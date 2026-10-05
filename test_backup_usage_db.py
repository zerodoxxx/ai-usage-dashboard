"""Backups exercise temporary databases, including committed WAL contents."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import backup_usage_db


def _database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, provider TEXT, total_tokens INTEGER);
        CREATE TABLE token_events (session_id TEXT, provider TEXT, total_tokens INTEGER);
        INSERT INTO sessions VALUES ('codex:one', 'codex', 28), ('agy:one', 'antigravity', 40);
        INSERT INTO token_events VALUES ('codex:one', 'codex', 14), ('codex:one', 'codex', 14),
                                        ('agy:one', 'antigravity', 40);
    """)
    connection.commit()
    return connection


def test_backup_reads_wal_and_verifies_provider_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "usage.db"
    connection = _database(db)
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(db))
    before = db.read_bytes()
    try:
        result = backup_usage_db.backup_usage_db()
        path = Path(result["path"])
        assert path.parent == db.parent / "backups"
        assert path.name.startswith("usage-") and path.suffix == ".db"
        assert result["providers"] == {
            "codex": {"sessions": 1, "events": 2, "session_tokens": 28, "event_tokens": 28},
            "antigravity": {"sessions": 1, "events": 1, "session_tokens": 40, "event_tokens": 40},
        }
        with sqlite3.connect(path) as saved:
            assert saved.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert saved.execute("SELECT SUM(total_tokens) FROM token_events").fetchone() == (68,)
        assert db.read_bytes() == before
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        connection.close()


def test_backup_rotates_only_old_matching_backups(tmp_path: Path) -> None:
    db = tmp_path / "usage.db"
    _database(db).close()
    dest = tmp_path / "saved"
    for _ in range(16):
        backup_usage_db.backup_usage_db(db, dest_dir=dest)
    backups = sorted(dest.glob("usage-*.db"))
    assert len(backups) == 14
    sentinel = dest / "important.db"
    sentinel.write_text("keep")
    result = backup_usage_db.backup_usage_db(db, dest_dir=dest, keep=2)
    assert len(list(dest.glob("usage-*.db"))) == 2
    assert Path(result["path"]).exists()
    assert sentinel.read_text() == "keep"


def test_backup_missing_source_never_creates_database(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    with pytest.raises(backup_usage_db.BackupError):
        backup_usage_db.backup_usage_db(db)
    assert not db.exists()
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("failure", ["integrity", "counts"])
def test_failed_verification_preserves_existing_backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    db = tmp_path / "usage.db"
    _database(db).close()
    dest = tmp_path / "saved"
    old = Path(backup_usage_db.backup_usage_db(db, dest_dir=dest)["path"])
    old_contents = old.read_bytes()
    original = backup_usage_db._provider_counts
    if failure == "integrity":
        monkeypatch.setattr(backup_usage_db, "_integrity_check", lambda _connection: (_ for _ in ()).throw(ValueError("bad integrity")))
    else:
        calls = 0

        def counts(connection: sqlite3.Connection) -> dict:
            nonlocal calls
            calls += 1
            result = original(connection)
            if calls == 2:
                result["codex"]["event_tokens"] += 1
            return result

        monkeypatch.setattr(backup_usage_db, "_provider_counts", counts)
    with pytest.raises(backup_usage_db.BackupError):
        backup_usage_db.backup_usage_db(db, dest_dir=dest, keep=1)
    assert list(dest.iterdir()) == [old]
    assert old.read_bytes() == old_contents


def test_backup_json_cli_and_invalid_keep(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "usage.db"
    _database(db).close()
    dest = tmp_path / "saved"
    assert backup_usage_db.main(["--db", str(db), "--dest-dir", str(dest), "--keep", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"
    assert backup_usage_db.main(["--db", str(db), "--keep", "0", "--json"]) != 0
    assert json.loads(capsys.readouterr().out)["status"] == "backup_failed"


def test_backup_counts_and_copy_share_snapshot_during_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "usage.db"
    writer = _database(db)
    original = backup_usage_db._provider_counts
    calls = 0
    def counts(connection: sqlite3.Connection) -> dict:
        nonlocal calls
        calls += 1
        result = original(connection)
        if calls == 1:
            writer.execute("INSERT INTO sessions VALUES ('codex:two', 'codex', 99)")
            writer.commit()
        return result
    monkeypatch.setattr(backup_usage_db, "_provider_counts", counts)
    try:
        result = backup_usage_db.backup_usage_db(db)
        assert result["providers"]["codex"]["sessions"] == 1
        with sqlite3.connect(result["path"]) as saved:
            assert saved.execute("SELECT COUNT(*) FROM sessions").fetchone() == (2,)
        assert writer.execute("SELECT COUNT(*) FROM sessions").fetchone() == (3,)
    finally:
        writer.close()


def test_backup_corrupt_source_does_not_publish_or_rotate(tmp_path: Path) -> None:
    db = tmp_path / "usage.db"
    db.write_bytes(b"corrupt database")
    with pytest.raises(backup_usage_db.BackupError):
        backup_usage_db.backup_usage_db(db)
    assert not (tmp_path / "backups").exists()
