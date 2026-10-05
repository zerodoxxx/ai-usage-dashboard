"""Tests for scripts/migrate_usage_db.py."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "scripts" / "migrate_usage_db.py"
spec = importlib.util.spec_from_file_location("migrate_usage_db", SCRIPT)
migrate_usage_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate_usage_db)


def _build_source(path: Path) -> sqlite3.Connection:
    """A WAL-mode DB with two providers; the connection stays open so WAL is live."""
    from src.usage_store import ensure_schema

    ensure_schema(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    session_columns = [r[1] for r in connection.execute("PRAGMA table_info(sessions)")]
    event_columns = [r[1] for r in connection.execute("PRAGMA table_info(token_events)")]
    assert "provider" in session_columns and "provider" in event_columns
    for provider, sessions in (("codex", 3), ("antigravity", 2)):
        for index in range(sessions):
            sid = f"{provider}:s{index}"
            connection.execute(
                "INSERT INTO sessions (session_id, provider, timestamp, date, model, "
                "total_tokens, updated_at) VALUES (?, ?, 't', 'd', 'm', ?, 't')",
                (sid, provider, 100 + index),
            )
            for step in range(2):
                connection.execute(
                    "INSERT INTO token_events (session_id, provider, step_index, timestamp, "
                    "model, total_tokens) VALUES (?, ?, ?, 't', 'm', ?)",
                    (sid, provider, step, 10 + step),
                )
    connection.commit()
    return connection


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_copy_matches_including_wal_and_leaves_source(tmp_path: Path) -> None:
    source = tmp_path / "old" / "token_usage.db"
    source.parent.mkdir()
    live = _build_source(source)
    try:
        assert Path(str(source) + "-wal").exists()  # content lives in the WAL
        before = (_digest(source), _digest(Path(str(source) + "-wal")))
        target = tmp_path / "new" / "dir" / "usage.db"

        src_summary, dst_summary = migrate_usage_db.migrate(source, target, keep_source=True)

        assert src_summary == dst_summary
        assert src_summary["codex"] == {"sessions": 3, "events": 6, "total_tokens": 303}
        assert src_summary["antigravity"]["sessions"] == 2
        with sqlite3.connect(target) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 5
        after = (_digest(source), _digest(Path(str(source) + "-wal")))
        assert before == after
        assert not list(target.parent.glob(".*migrating*"))
    finally:
        live.close()


def test_refuses_existing_target_unless_forced(tmp_path: Path) -> None:
    source = tmp_path / "token_usage.db"
    _build_source(source).close()
    target = tmp_path / "usage.db"
    target.write_bytes(b"precious")

    with pytest.raises(migrate_usage_db.MigrationError, match="already exists"):
        migrate_usage_db.migrate(source, target)
    assert target.read_bytes() == b"precious"

    migrate_usage_db.migrate(source, target, force=True)
    with sqlite3.connect(target) as connection:
        assert migrate_usage_db.summarize(connection)["codex"]["sessions"] == 3
    backups = list(tmp_path.glob("usage.db.*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"precious"
    assert not source.exists()
    assert len([path for path in tmp_path.glob("token_usage.db.migrated-*")
                if not path.name.endswith(("-wal", "-shm"))]) == 1


def test_dry_run_writes_nothing_and_cli_prints_table(tmp_path: Path, capsys) -> None:
    source = tmp_path / "token_usage.db"
    _build_source(source).close()
    target = tmp_path / "new" / "usage.db"

    code = migrate_usage_db.main(
        ["--source", str(source), "--target", str(target), "--dry-run"]
    )
    out = capsys.readouterr().out
    assert code == 0 and "codex" in out and "antigravity" in out
    assert not target.parent.exists()

    assert migrate_usage_db.main(["--source", str(source), "--target", str(target)]) == 0
    assert "match" in capsys.readouterr().out and target.exists()
    assert migrate_usage_db.main(["--source", str(source), "--target", str(target)]) == 1


def test_missing_source_fails(tmp_path: Path) -> None:
    with pytest.raises(migrate_usage_db.MigrationError, match="not found"):
        migrate_usage_db.migrate(tmp_path / "nope.db", tmp_path / "usage.db")
    assert not (tmp_path / "usage.db").exists()


def test_default_target_honors_environment_and_archives_source(tmp_path, monkeypatch, capsys):
    source = tmp_path / "token_usage.db"
    _build_source(source).close()
    target = tmp_path / "override" / "usage.db"
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(target))
    assert migrate_usage_db.main(["--source", str(source)]) == 0
    assert target.exists() and not source.exists()
    archived = [path for path in tmp_path.glob("token_usage.db.migrated-*")
                if not path.name.endswith(("-wal", "-shm"))]
    assert len(archived) == 1
    assert migrate_usage_db._summarize_path(archived[0]) == migrate_usage_db._summarize_path(target)
    assert "source archived" in capsys.readouterr().out


def test_keep_source_cli(tmp_path, capsys):
    source = tmp_path / "token_usage.db"
    _build_source(source).close()
    before = _digest(source)
    target = tmp_path / "usage.db"
    assert migrate_usage_db.main(["--source", str(source), "--target", str(target), "--keep-source"]) == 0
    assert _digest(source) == before
    assert not list(tmp_path.glob("*.migrated-*"))
    assert "source left untouched" in capsys.readouterr().out


def test_force_backup_preserves_target_wal(tmp_path):
    source = tmp_path / "token_usage.db"
    _build_source(source).close()
    target = tmp_path / "usage.db"
    live = _build_source(target)
    try:
        live.execute("UPDATE sessions SET total_tokens=999 WHERE provider='codex'")
        live.commit()
        expected = migrate_usage_db.summarize(live)
        migrate_usage_db.migrate(source, target, force=True)
        backups = list(tmp_path.glob("usage.db.*.bak"))
        assert len(backups) == 1
        assert migrate_usage_db._integrity_ok(backups[0])
        assert migrate_usage_db._summarize_path(backups[0]) == expected
    finally:
        live.close()


def test_verification_failure_preserves_source_and_target(tmp_path, monkeypatch):
    source = tmp_path / "token_usage.db"
    _build_source(source).close()
    target = tmp_path / "usage.db"
    target.write_bytes(b"precious")
    monkeypatch.setattr(migrate_usage_db, "_integrity_ok", lambda path: False)
    with pytest.raises(migrate_usage_db.MigrationError, match="integrity_check"):
        migrate_usage_db.migrate(source, target, force=True)
    assert source.exists() and target.read_bytes() == b"precious"
    assert not list(tmp_path.glob("*.bak"))
    assert not list(tmp_path.glob("*.migrated-*"))
    assert not list(tmp_path.glob(".*migrating*"))


def test_archive_collision_refused_without_replacing_target(tmp_path):
    from datetime import datetime, timezone

    source = tmp_path / "token_usage.db"
    _build_source(source).close()
    archive = source.with_name(f"{source.name}.migrated-{datetime.now(timezone.utc):%Y-%m-%d}")
    archive.write_bytes(b"older archive")
    target = tmp_path / "usage.db"
    with pytest.raises(migrate_usage_db.MigrationError, match="archive already exists"):
        migrate_usage_db.migrate(source, target)
    assert source.exists() and not target.exists()
    assert archive.read_bytes() == b"older archive"


def test_target_appearing_during_copy_is_not_overwritten(tmp_path, monkeypatch):
    source = tmp_path / "token_usage.db"
    _build_source(source).close()
    target = tmp_path / "usage.db"

    def verify(path):
        target.write_bytes(b"new target")
        return True

    monkeypatch.setattr(migrate_usage_db, "_integrity_ok", verify)
    with pytest.raises(migrate_usage_db.MigrationError, match="target appeared"):
        migrate_usage_db.migrate(source, target)
    assert source.exists() and target.read_bytes() == b"new target"
