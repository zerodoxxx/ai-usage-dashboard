"""Antigravity writer regressions, isolated from real hooks and databases."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import agy_usage_writer as writer
from src.parsers.contracts import TokenUsage, UsageSession
from src.usage_store import ensure_schema, read_usage_sessions, write_usage_sessions


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "_ENC", None)
    monkeypatch.setattr(writer, "_ENCODING_ATTEMPTED", True)
    base = tmp_path / "antigravity-cli"
    path = base / "brain" / "raw-conversation" / ".system_generated/logs/transcript.jsonl"
    path.parent.mkdir(parents=True)
    (base / "settings.json").write_text(json.dumps({"model": "gemini-2.5-flash"}))
    steps = [
        {"created_at": "2026-10-01T10:00:00Z", "source": "USER_EXPLICIT", "content": "one two"},
        {"created_at": "2026-10-01T10:01:00Z", "type": "PLANNER_RESPONSE", "content": "yes!", "thinking": "think"},
        {"created_at": "2026-10-01T10:02:00Z", "type": "GENERIC", "content": "more", "tool_calls": [{"name": "tool"}]},
        {"created_at": "2026-10-01T10:03:00Z", "source": "MODEL", "content": "done", "tool_calls": [{"result": "ok"}]},
    ]
    path.write_text("\n".join([json.dumps(steps[0]), "bad json", "[]", "", *map(json.dumps, steps[1:])]) + "\n")
    return base, path, steps


def _expected(steps):
    first = writer.count_tokens(steps[0]["content"])
    added = writer.count_tokens(steps[2]["content"] + json.dumps(steps[2]["tool_calls"]))
    output = writer.count_tokens("yes!") + writer.count_tokens("think")
    output += writer.count_tokens("done" + json.dumps(steps[3]["tool_calls"]))
    return first, first + added, output


def test_parse_legacy_estimation_and_cache_semantics(history):
    base, path, steps = history
    session = writer.parse_transcript(path, "raw-conversation", base)
    first, second, output = _expected(steps)
    assert session.id == "raw-conversation"
    assert session.call_count == 2 and session.metadata["step_count"] == 4
    assert session.usage.input_tokens == first + second
    assert session.usage.output_tokens == output
    assert session.usage.reasoning_output_tokens == 1
    assert session.usage.cached_input_tokens == int(second * 0.45)
    assert session.usage.total_tokens == first + second + output
    assert session.usage.cache_write_tokens == 0
    assert session.events[0].usage.cached_input_tokens == 0
    assert session.events[1].event_id == "raw-conversation:step:6"
    assert session.metadata["estimated"] is True
    assert session.metadata["token_source"] == "estimated"
    assert session.cost is not None and session.cost.reported_usd is None


def test_token_encoder_and_exact_fallback(monkeypatch):
    monkeypatch.setattr(writer, "_ENCODING_ATTEMPTED", True)
    monkeypatch.setattr(writer, "_ENC", None)
    assert writer.count_tokens("") == 0
    assert writer.count_tokens("hello  world!") == 4
    assert writer.count_tokens("   ") == 1

    class Encoder:
        def encode(self, text):
            if text == "fail":
                raise ValueError("special token")
            return [1, 2]

    monkeypatch.setattr(writer, "_ENC", Encoder())
    assert writer.count_tokens("hello") == 2
    assert writer.count_tokens("fail") == 1


def test_hook_replay_and_shorter_snapshot_replace_events(history, tmp_path):
    base, path, _ = history
    db = tmp_path / "capture.db"
    payload = {"tool_input": {"conversationId": "raw-conversation"}, "transcriptPath": str(path)}
    total = writer.process_hook_payload(payload, db_path=db, agy_dir=base)
    assert writer.process_hook_payload(payload, db_path=db, agy_dir=base) == total
    sessions = read_usage_sessions("antigravity", db_path=db)
    assert len(sessions) == 1 and len(sessions[0].events) == 2
    assert sessions[0].usage.total_tokens == total
    assert sessions[0].metadata["estimated"] is True
    assert writer.backfill_agy_usage(db_path=db, agy_dir=base) == (1, 2)
    after_backfill = read_usage_sessions("antigravity", db_path=db)[0]
    assert len(after_backfill.events) == 2 and after_backfill.usage.total_tokens == total
    assert after_backfill.metadata["capture_source_hash"] == sessions[0].metadata["capture_source_hash"]
    path.write_text('\n'.join(path.read_text().splitlines()[:5]) + '\n')
    writer.process_hook_payload(payload, db_path=db, agy_dir=base)
    assert len(read_usage_sessions("antigravity", db_path=db)[0].events) == 1


def test_delayed_backfill_snapshot_cannot_replace_newer_hook_write(history, tmp_path):
    base, path, _ = history
    path.write_text(json.dumps({
        "created_at": "2026-10-01T10:00:00Z", "source": "MODEL", "content": "one",
    }) + "\n")
    v1_stat = path.stat()
    stale_snapshot = writer.parse_transcript(path, "raw-conversation", base)
    assert stale_snapshot is not None and stale_snapshot.call_count == 1

    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "created_at": "2026-10-01T10:01:00Z", "source": "MODEL", "content": "two three",
        }) + "\n")
    os.utime(path, ns=(v1_stat.st_atime_ns, v1_stat.st_mtime_ns + 2_000_000_000))
    v2_stat = path.stat()
    assert v2_stat.st_size != v1_stat.st_size
    assert v2_stat.st_mtime_ns > stale_snapshot.metadata["capture_mtime_ns"]

    db = tmp_path / "stale-backfill.db"
    payload = {"conversationId": "raw-conversation", "transcriptPath": str(path)}
    v2_total = writer.process_hook_payload(payload, db_path=db, agy_dir=base)
    assert v2_total is not None and v2_total > stale_snapshot.usage.total_tokens
    assert writer._write_sessions([stale_snapshot], db) == 0
    stored = read_usage_sessions("antigravity", db_path=db)[0]
    assert len(stored.events) == 2
    assert stored.usage.total_tokens == v2_total


def test_hook_and_backfill_stamp_same_transcript_source(history, tmp_path):
    base, path, _ = history
    hook_db = tmp_path / "hook-source.db"
    backfill_db = tmp_path / "backfill-source.db"
    payload = {"conversationId": "raw-conversation", "transcriptPath": str(path)}

    assert writer.process_hook_payload(payload, db_path=hook_db, agy_dir=base) is not None
    assert writer.backfill_agy_usage(db_path=backfill_db, agy_dir=base) == (1, 2)
    hook = read_usage_sessions("antigravity", db_path=hook_db)[0]
    backfill = read_usage_sessions("antigravity", db_path=backfill_db)[0]
    assert hook.metadata["capture_source_hash"] == backfill.metadata["capture_source_hash"]


def test_backfill_prefers_canonical_transcript_once(history, tmp_path):
    base, nested_path, _ = history
    canonical_path = base / "brain" / "raw-conversation" / "transcript.jsonl"
    canonical_path.write_text(json.dumps({
        "created_at": "2026-10-01T10:00:00Z", "source": "MODEL", "content": "canonical",
    }) + "\n")
    db = tmp_path / "canonical-backfill.db"

    assert writer.backfill_agy_usage(db_path=db, agy_dir=base) == (1, 1)
    sessions = read_usage_sessions("antigravity", db_path=db)
    assert len(sessions) == 1 and len(sessions[0].events) == 1
    assert sessions[0].usage.total_tokens == writer.count_tokens("canonical")
    assert sessions[0].metadata["capture_source_hash"] == writer._capture_file_metadata(canonical_path)[
        "capture_source_hash"
    ]
    assert nested_path.exists()


def test_hook_fallback_paths_and_skips(history, tmp_path):
    base, path, _ = history
    db = tmp_path / "capture.db"
    assert writer.process_hook_payload({"conversationId": "raw-conversation"}, db_path=db, agy_dir=base) > 0
    assert writer.process_hook_payload({"conversationId": "missing"}, db_path=db, agy_dir=base) is None
    for value in (None, [], {"tool_input": []}, {"conversationId": 123}):
        assert writer.process_hook_payload(value, db_path=db, agy_dir=base) is None
    direct = path.parents[2] / "transcript.jsonl"
    direct.write_bytes(path.read_bytes())
    assert writer.process_hook_payload({"conversationId": "raw-conversation", "transcriptPath": "missing"}, db_path=db, agy_dir=base) > 0


def test_existing_raw_session_is_never_duplicated(history, tmp_path):
    base, path, _ = history
    db = tmp_path / "legacy.db"
    ensure_schema(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO sessions(session_id,timestamp,date,model,updated_at,total_tokens) VALUES(?,?,?,?,?,?)",
            ("raw-conversation", "2026-10-01T10:00:00Z", "2026-10-01", "gemini", "2026-10-01", 123),
        )
    expected = writer.parse_transcript(path, "raw-conversation", base)
    payload = {"conversationId": "raw-conversation", "transcriptPath": str(path)}
    assert writer.process_hook_payload(payload, db_path=db, agy_dir=base) == expected.usage.total_tokens
    assert writer.backfill_agy_usage(db_path=db, agy_dir=base) == (1, len(expected.events))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT session_id,total_tokens FROM sessions").fetchall() == [
            ("raw-conversation", expected.usage.total_tokens),
        ]
        assert conn.execute("SELECT DISTINCT session_id FROM token_events").fetchall() == [("raw-conversation",)]
    assert read_usage_sessions("antigravity", db)[0].id == "raw-conversation"


def test_hook_folds_prefixed_session_into_raw_id(history, tmp_path):
    base, path, _ = history
    db = tmp_path / "prefixed.db"
    old = UsageSession(
        id="raw-conversation", tool="antigravity", provider="antigravity",
        model="gemini", usage=TokenUsage(input_tokens=123),
    )
    write_usage_sessions("antigravity", [old], db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET session_id = 'antigravity:raw-conversation'")
    assert read_usage_sessions("antigravity", db)[0].id == "raw-conversation"
    expected = writer.parse_transcript(path, "raw-conversation", base)
    assert writer.process_hook_payload(
        {"conversationId": "raw-conversation", "transcriptPath": str(path)},
        db_path=db, agy_dir=base,
    ) == expected.usage.total_tokens
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT session_id,total_tokens FROM sessions").fetchall() == [
            ("raw-conversation", expected.usage.total_tokens),
        ]
        assert conn.execute("SELECT DISTINCT session_id FROM token_events").fetchall() == [("raw-conversation",)]


def test_backfill_recursive_and_cli_mode_with_piped_stdin(history, tmp_path):
    base, path, _ = history
    db = tmp_path / "backfill.db"
    assert writer.backfill_agy_usage(db_path=db, agy_dir=base) == (1, 2)
    assert writer.backfill_agy_usage(db_path=db, agy_dir=base) == (1, 2)
    result = subprocess.run(
        [sys.executable, str(Path(writer.__file__)), "--backfill", "--agy-dir", str(base), "--db", str(db)],
        input="", capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0 and "Backfilled 1" in result.stderr
    assert len(read_usage_sessions("antigravity", db_path=db)) == 1


@pytest.mark.parametrize("payload", ["", "bad json", "[]", '{"tool_input": []}'])
def test_cli_hook_errors_never_fail_agy(payload, tmp_path):
    db = tmp_path / "capture.db"
    result = subprocess.run(
        [sys.executable, str(Path(writer.__file__)), "--db", str(db)],
        input=payload, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert result.stderr and not db.exists()


def test_status_and_cli_failures_are_nonfatal(tmp_path, capsys):
    db = tmp_path / "missing.db"
    assert writer.main(["--status", "--db", str(db)]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)
    assert not db.exists()
    assert writer.main(["--backfill", "--agy-dir", str(tmp_path / "missing")]) == 0
    assert "skipped" in capsys.readouterr().err
    assert writer.main(["--invalid"]) == 0


def test_cli_capture_then_agy_usage_api(history, tmp_path, monkeypatch):
    import src.app as app_module
    from test_store_read_path import TestClient

    base, path, steps = history
    db = tmp_path / "e2e.db"
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(db))
    monkeypatch.setattr(app_module, "refresh_pricing", lambda: None)
    command = [sys.executable, str(Path(writer.__file__)), "--agy-dir", str(base)]
    payload = json.dumps({"conversationId": "raw-conversation", "transcriptPath": str(path)})
    # Capture through a real subprocess, then exercise the dashboard read path.
    for _ in range(2):
        result = subprocess.run(command, input=payload, capture_output=True, text=True, check=False, env=os.environ.copy())
        assert result.returncode == 0, result.stderr
    parsed = writer.parse_transcript(path, "raw-conversation", base)
    # Use persisted totals so this also works when the test interpreter has
    # tiktoken installed (the parent fixture deliberately tests the fallback).
    stored = read_usage_sessions("antigravity", db_path=db)[0]
    with TestClient(app_module.app) as client:
        response = client.get("/api/usage?tool=agy")
    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["session_count"] == 1
    assert data["summary"]["call_count"] == parsed.call_count
    assert data["summary"]["total_tokens"] == stored.usage.total_tokens > 0
    assert data["summary"]["total_input"] == stored.usage.input_tokens
    assert data["summary"]["output"] == stored.usage.output_tokens
    assert data["summary"]["total_input"] + data["summary"]["output"] == data["summary"]["total_tokens"]
