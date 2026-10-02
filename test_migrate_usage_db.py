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

        src_summary, dst_summary = migrate_usage_db.migrate(source, target)

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
    assert migrate_usage_db.summarize(sqlite3.connect(target))["codex"]["sessions"] == 3


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
