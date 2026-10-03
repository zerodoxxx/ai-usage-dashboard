"""Read-only doctor checks using installed-looking fixtures, never real HOME."""
from datetime import datetime, timedelta, timezone
import json
import os
import plistlib
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from scripts import doctor
from src.usage_store import ensure_schema


@pytest.fixture
def healthy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("AI_USAGE_DB_PATH", raising=False)
    db = home / ".local/share/ai-usage/usage.db"
    ensure_schema(db)
    with sqlite3.connect(db) as connection:
        for provider in ("codex", "claude-code", "antigravity"):
            connection.execute(
                "INSERT INTO sessions (session_id, provider, timestamp, date, model, updated_at) "
                "VALUES (?, ?, 't', 'd', 'm', ?)",
                (provider + ":s", provider, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                "INSERT INTO token_events (session_id, provider, step_index, timestamp, model) "
                "VALUES (?, ?, 0, 't', 'm')", (provider + ":s", provider),
            )
    connection.close()
    # Closing the final SQLite connection checkpoints the WAL.
    for name, installer, filename in (
        ("codex", doctor.codex_installer, "hooks.json"),
        ("claude", doctor.claude_installer, "settings.json"),
    ):
        stage = tmp_path / (name + "-stage")
        installer._copy_payload(doctor.PROJECT_ROOT, stage)
        digest = doctor.codex_installer._artifact_digest(stage)
        assert doctor.expected_digest(installer) == digest
        release = home / f".{name}/usage-publisher/releases" / digest
        release.parent.mkdir(parents=True)
        stage.rename(release)
        script = release / installer.PUBLISHER_RELATIVE_PATH
        if name == "codex":
            command = installer._hook_command(Path(sys.executable), script, db, digest)
        else:
            command = installer._hook_command(Path(sys.executable), script, db)
        document = {"hooks": {event: [{"hooks": [{"type": "command", "command": command,
                                                   "async": False}]}]
                               for event in installer.HOOK_EVENTS}}
        (home / f".{name}" / filename).write_text(json.dumps(document))
    agy = home / ".gemini/config/hooks.json"
    agy.parent.mkdir(parents=True)
    agy.write_text(json.dumps({"agy-token-tracker": {"enabled": True, "Stop": [
        {"type": "command", "command": f"{sys.executable} {home}/track_usage.py"}
    ]}}))
    plist = home / "Library/LaunchAgents" / f"{doctor.LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    log = home / ".codex/usage-retention/retention.log"
    log.parent.mkdir(parents=True)
    log.write_text("previous run\nretention completed\n")
    backups = db.parent / "backups"
    backups.mkdir()
    (backups / "backup.db").write_bytes(b"backup")

    def launchctl(command, **kwargs):
        assert command == ["launchctl", "print", f"gui/{os.getuid()}/{doctor.LABEL}"]
        return subprocess.CompletedProcess(command, 0, "loaded", "")

    monkeypatch.setattr(doctor.subprocess, "run", launchctl)
    return home, db


def _rows(report):
    return {row["check"]: row for row in report["checks"]}


def _change_hook(home, provider, event, field, value):
    path = home / (".codex/hooks.json" if provider == "codex" else ".claude/settings.json")
    config = json.loads(path.read_text())
    config["hooks"][event][0]["hooks"][0][field] = value
    path.write_text(json.dumps(config))


def test_doctor_ok_table_json_and_read_only(healthy, capsys):
    home, db = healthy
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
              for path in home.rglob("*") if path.is_file()}
    assert doctor.main(["--home", str(home)]) == 0
    table = capsys.readouterr().out
    assert "CHECK" in table and str(db) in table and "sessions=1 events=1" in table
    assert doctor.main(["--home", str(home), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "OK"
    assert _rows(report)["retention.log"]["last_line"] == "retention completed"
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns)
             for path in home.rglob("*") if path.is_file()}
    assert before == after


@pytest.mark.parametrize("provider,event", [("codex", "Interrupt"), ("claude", "SessionEnd")])
def test_doctor_missing_hooks(healthy, provider, event):
    home, _ = healthy
    path = home / (".codex/hooks.json" if provider == "codex" else ".claude/settings.json")
    document = json.loads(path.read_text())
    document["hooks"].pop(event)
    path.write_text(json.dumps(document))
    report = doctor.diagnose(home)
    assert report["exit_code"] == 1
    assert _rows(report)[f"{provider}.{event}.present"]["status"] == "FAIL"


def test_doctor_async_hook(healthy):
    home, _ = healthy
    _change_hook(home, "codex", "Stop", "async", True)
    report = doctor.diagnose(home)
    assert report["exit_code"] == 1
    assert _rows(report)["codex.Stop.sync"]["status"] == "FAIL"


def test_doctor_async_claude_hook_is_ok(healthy):
    home, _ = healthy
    _change_hook(home, "claude", "Stop", "async", True)
    report = doctor.diagnose(home)
    assert _rows(report)["claude.Stop.sync"]["status"] == "OK"
    assert report["exit_code"] == 0


def test_doctor_stale_provider(healthy):
    home, db = healthy
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE sessions SET updated_at=? WHERE provider='codex'", (stale,))
    connection.close()
    report = doctor.diagnose(home)
    assert report["exit_code"] == 2
    row = _rows(report)["provider.codex"]
    assert row["status"] == "WARN" and "STALE" in row["detail"]
    assert row["last_write"] == stale


@pytest.mark.parametrize("entry", [{"type": "command"}, {"name": "broken"}, {}, "bad"])
def test_doctor_malformed_agy_hooks(healthy, entry):
    home, _ = healthy
    path = home / ".gemini/config/hooks.json"
    config = json.loads(path.read_text())
    config["agy-token-tracker"]["Stop"].append(entry)
    path.write_text(json.dumps(config))
    report = doctor.diagnose(home)
    assert report["exit_code"] == 1
    assert _rows(report)["agy.entries"]["status"] == "FAIL"


def test_doctor_bad_db_pin_and_missing_interpreter(healthy):
    home, db = healthy
    path = home / ".codex/hooks.json"
    config = json.loads(path.read_text())
    handler = config["hooks"]["Stop"][0]["hooks"][0]
    handler["command"] = handler["command"].replace(str(db), str(db.with_name("wrong.db"))).replace(
        sys.executable, str(home / "missing-python")
    )
    path.write_text(json.dumps(config))
    rows = _rows(doctor.diagnose(home))
    assert rows["codex.Stop.db"]["status"] == "FAIL"
    assert rows["codex.Stop.interpreter"]["status"] == "FAIL"


def test_doctor_outdated_release(healthy, monkeypatch):
    home, _ = healthy
    monkeypatch.setattr(doctor, "expected_digest", lambda installer: "new-digest")
    report = doctor.diagnose(home)
    assert report["exit_code"] == 2
    assert _rows(report)["codex.Stop.digest"]["status"] == "WARN"


def test_doctor_agy_environment_override(healthy, monkeypatch):
    home, db = healthy
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(db))
    assert doctor.diagnose(home)["exit_code"] == 0
    agy = home / ".gemini/config/hooks.json"
    agy.write_text(json.dumps({"usage-writer": {"Stop": [{"command":
        f"env AI_USAGE_DB_PATH={home}/wrong.db {sys.executable} track_usage.py"}]}}))
    assert _rows(doctor.diagnose(home))["agy.db.1"]["status"] == "FAIL"


def test_doctor_missing_db_never_creates_it(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_USAGE_DB_PATH", raising=False)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1))
    before = set(tmp_path.rglob("*"))
    report = doctor.diagnose(tmp_path)
    assert report["exit_code"] == 1
    assert set(tmp_path.rglob("*")) == before
    assert _rows(report)["retention.loaded"]["status"] == "WARN"


def test_doctor_live_wal_never_writes_sidecars(healthy):
    home, db = healthy
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE sessions SET title='uncheckpointed'")
        connection.commit()
        files = [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]
        before = {path: path.read_bytes() for path in files}
        report = doctor.diagnose(home)
        assert _rows(report)["db.wal"]["status"] == "WARN"
        assert before == {path: path.read_bytes() for path in files}
    finally:
        connection.close()


def test_doctor_invalid_agy_json(healthy):
    home, _ = healthy
    (home / ".gemini/config/hooks.json").write_text("{broken")
    report = doctor.diagnose(home)
    assert report["exit_code"] == 1
    assert _rows(report)["agy.config"]["status"] == "FAIL"


def test_doctor_newest_backup_and_age(healthy):
    home, db = healthy
    backup = db.parent / "backups/backup.db"
    old_time = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(backup, (old_time, old_time))
    report = doctor.diagnose(home)
    assert report["exit_code"] == 2
    row = _rows(report)["backups.newest"]
    assert row["status"] == "WARN" and row["age_seconds"] > doctor.STALE_SECONDS
    latest = backup.with_name("latest.db")
    latest.write_bytes(b"latest")
    report = doctor.diagnose(home)
    assert report["exit_code"] == 0
    assert _rows(report)["backups.newest"]["path"] == str(latest)


def test_doctor_accepts_missing_backups_when_retention_disables_them(healthy):
    home, db = healthy
    for backup in (db.parent / "backups").iterdir():
        backup.unlink()
    assert _rows(doctor.diagnose(home))["backups.newest"]["status"] == "WARN"
    plist = home / "Library/LaunchAgents" / f"{doctor.LABEL}.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["python", "codex_retention.py", "--apply", "--no-backup"]}))
    report = doctor.diagnose(home)
    assert _rows(report)["backups.newest"]["status"] == "OK"
    assert report["exit_code"] == 0

def test_doctor_nonzero_launchctl_means_not_loaded(healthy, monkeypatch):
    home, _ = healthy
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 1, "", "not found"))
    report = doctor.diagnose(home)
    assert report["exit_code"] == 2
    assert _rows(report)["retention.loaded"]["detail"] == "not loaded"
