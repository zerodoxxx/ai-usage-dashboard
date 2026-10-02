"""Tests for the Claude Code usage hook, backfill and its safe CLI protocol."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

import scripts.claude_usage_writer as writer
from src.usage_store import is_provider_capture_enabled, read_usage_sessions

PROVIDER = "claude-code"


def _assistant(session: str, message_id: str, ts: str, *, out: int, inp: int = 10, parent=None) -> dict:
    return {
        "type": "assistant",
        "uuid": f"u-{message_id}-{out}",
        "parentUuid": parent,
        "sessionId": session,
        "timestamp": ts,
        "cwd": "/work/project",
        "requestId": f"req-{message_id}",
        "message": {
            "id": message_id,
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "SECRET RESPONSE BODY"}],
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "output_tokens": out,
            },
        },
    }


def _user(session: str, ts: str, text: str = "hello there") -> dict:
    return {
        "type": "user", "uuid": f"user-{ts}", "sessionId": session, "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _write_transcript(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _main_records(session: str = "sess-1") -> list[dict]:
    return [
        _user(session, "2026-09-01T10:00:00Z"),
        # The same response persisted twice (streaming blocks): count once.
        _assistant(session, "msg_1", "2026-09-01T10:00:05Z", out=5),
        _assistant(session, "msg_1", "2026-09-01T10:00:06Z", out=50),
        _assistant(session, "msg_2", "2026-09-01T10:01:00Z", out=70),
    ]


# msg_1 (final, out=50): 10+100+20+50 = 180; msg_2: 10+100+20+70 = 200
MAIN_TOTAL = 380


def test_stop_hook_writes_deduplicated_snapshot_idempotently(tmp_path: Path) -> None:
    transcript = _write_transcript(tmp_path / "projects/p/sess-1.jsonl", _main_records())
    db = tmp_path / "usage.db"
    payload = {"hook_event_name": "Stop", "session_id": "sess-1", "transcript_path": str(transcript)}

    assert writer.process_hook_payload(payload, db_path=db) == MAIN_TOTAL
    assert writer.process_hook_payload(payload, db_path=db) == MAIN_TOTAL

    sessions = read_usage_sessions(PROVIDER, db)
    assert len(sessions) == 1
    assert sessions[0].id == "sess-1"
    assert sessions[0].usage.total_tokens == MAIN_TOTAL
    assert [e.event_id for e in sessions[0].events] == ["msg_1", "msg_2"]
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT COUNT(*) FROM token_events WHERE provider = ?", (PROVIDER,)).fetchone()[0] == 2
    # Only the session title (user-visible) may derive from text; never response bodies.
    dump = b"".join(line.encode() for line in connection.iterdump())
    assert b"SECRET RESPONSE BODY" not in dump


def test_growing_transcript_replaces_snapshot_without_double_counting(tmp_path: Path) -> None:
    records = _main_records()
    transcript = _write_transcript(tmp_path / "projects/p/sess-1.jsonl", records[:2])
    db = tmp_path / "usage.db"
    payload = {"hook_event_name": "Stop", "transcript_path": str(transcript)}
    writer.process_hook_payload(payload, db_path=db)
    _write_transcript(transcript, records)
    writer.process_hook_payload({"hook_event_name": "SessionEnd", "transcript_path": str(transcript)}, db_path=db)

    (session,) = read_usage_sessions(PROVIDER, db)
    assert session.usage.total_tokens == MAIN_TOTAL
    assert session.call_count == 2


def test_subagent_stop_captures_the_agent_transcript_as_its_own_session(tmp_path: Path) -> None:
    main = _write_transcript(tmp_path / "projects/p/sess-1.jsonl", _main_records())
    agent_records = [
        {**_assistant("sess-1", "msg_a1", "2026-09-01T10:00:30Z", out=30), "isSidechain": True},
    ]
    agent = _write_transcript(tmp_path / "projects/p/sess-1/subagents/agent-abc.jsonl", agent_records)
    db = tmp_path / "usage.db"

    total = writer.process_hook_payload(
        {"hook_event_name": "SubagentStop", "session_id": "sess-1",
         "transcript_path": str(main), "agent_transcript_path": str(agent)},
        db_path=db,
    )
    assert total == 160
    (session,) = read_usage_sessions(PROVIDER, db)
    assert session.id == "agent-abc"


def test_session_end_also_sweeps_subagent_transcripts(tmp_path: Path) -> None:
    main = _write_transcript(tmp_path / "projects/p/sess-1.jsonl", _main_records())
    _write_transcript(
        tmp_path / "projects/p/sess-1/subagents/agent-abc.jsonl",
        [{**_assistant("sess-1", "msg_a1", "2026-09-01T10:00:30Z", out=30), "isSidechain": True}],
    )
    db = tmp_path / "usage.db"
    writer.process_hook_payload({"hook_event_name": "SessionEnd", "transcript_path": str(main)}, db_path=db)
    assert {s.id for s in read_usage_sessions(PROVIDER, db)} == {"sess-1", "agent-abc"}


def test_resumed_transcript_does_not_recount_events_owned_by_another_session(tmp_path: Path) -> None:
    original = _write_transcript(tmp_path / "projects/p/sess-1.jsonl", _main_records("sess-1"))
    # A resumed session copies earlier responses, then adds its own.
    resumed = _write_transcript(
        tmp_path / "projects/p/sess-2.jsonl",
        _main_records("sess-2") + [_assistant("sess-2", "msg_3", "2026-09-02T09:00:00Z", out=1)],
    )
    db = tmp_path / "usage.db"
    for path in (original, resumed):
        writer.process_hook_payload({"hook_event_name": "Stop", "transcript_path": str(path)}, db_path=db)

    by_id = {s.id: s for s in read_usage_sessions(PROVIDER, db)}
    assert by_id["sess-1"].usage.total_tokens == MAIN_TOTAL
    assert by_id["sess-2"].usage.total_tokens == 131
    assert sum(s.usage.total_tokens for s in by_id.values()) == MAIN_TOTAL + 131


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"hook_event_name": "PreToolUse", "transcript_path": "x.jsonl"},
        {"hook_event_name": "Stop"},
        {"hook_event_name": "Stop", "transcript_path": "/definitely/missing.jsonl"},
        {"hook_event_name": "Stop", "transcript_path": "/etc/hosts"},
    ],
)
def test_irrelevant_or_unusable_payloads_are_skipped(payload, tmp_path: Path) -> None:
    db = tmp_path / "usage.db"
    assert writer.process_hook_payload(payload, db_path=db) is None
    assert not db.exists()


def test_backfill_imports_everything_dedupes_across_files_and_is_rerunnable(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    _write_transcript(claude / "projects/p/sess-1.jsonl", _main_records("sess-1"))
    _write_transcript(
        claude / "projects/p/sess-2.jsonl",
        _main_records("sess-2") + [_assistant("sess-2", "msg_3", "2026-09-02T09:00:00Z", out=1)],
    )
    _write_transcript(
        claude / "projects/p/sess-1/subagents/agent-abc.jsonl",
        [_assistant("sess-1", "msg_a1", "2026-09-01T10:00:30Z", out=30)],
    )
    db = tmp_path / "usage.db"

    first = writer.backfill_claude_usage(db_path=db, claude_dir=claude)
    snapshot = sorted((s.id, s.usage.total_tokens, s.call_count) for s in read_usage_sessions(PROVIDER, db))
    second = writer.backfill_claude_usage(db_path=db, claude_dir=claude)

    assert first == second
    assert sorted((s.id, s.usage.total_tokens, s.call_count) for s in read_usage_sessions(PROVIDER, db)) == snapshot
    assert {sid for sid, _, _ in snapshot} == {"sess-1", "sess-2", "agent-abc"}
    # msg_1/msg_2 exist in two transcripts but are counted once overall.
    assert sum(total for _, total, _ in snapshot) == MAIN_TOTAL + 131 + 160
    assert is_provider_capture_enabled(PROVIDER, db_path=db)


def test_backfill_then_hook_is_consistent(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    transcript = _write_transcript(claude / "projects/p/sess-1.jsonl", _main_records())
    db = tmp_path / "usage.db"
    writer.backfill_claude_usage(db_path=db, claude_dir=claude, enable_capture=False)
    writer.process_hook_payload({"hook_event_name": "Stop", "transcript_path": str(transcript)}, db_path=db)
    (session,) = read_usage_sessions(PROVIDER, db)
    assert session.usage.total_tokens == MAIN_TOTAL
    assert not is_provider_capture_enabled(PROVIDER, db_path=db)


def test_backfill_without_history_directory_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        writer.backfill_claude_usage(db_path=tmp_path / "usage.db", claude_dir=tmp_path / "nope")


def test_cli_hook_never_fails_or_writes_stdout(tmp_path: Path, monkeypatch, capsys) -> None:
    transcript = _write_transcript(tmp_path / "projects/p/sess-1.jsonl", _main_records())
    db = tmp_path / "usage.db"
    stdin = json.dumps({"hook_event_name": "Stop", "transcript_path": str(transcript)})
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert writer.main(["--db", str(db)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"{MAIN_TOTAL} tokens" in captured.err

    for bad in ("not json", ""):
        monkeypatch.setattr("sys.stdin", io.StringIO(bad))
        assert writer.main(["--db", str(db)]) == 0
        assert capsys.readouterr().out == ""


def test_cli_hook_swallows_store_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    transcript = _write_transcript(tmp_path / "projects/p/sess-1.jsonl", _main_records())

    def boom(*_a, **_k):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(writer, "write_owned_usage_sessions", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "Stop", "transcript_path": str(transcript)})))
    assert writer.main(["--db", str(tmp_path / "usage.db")]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "database exploded" in captured.err


def test_cli_backfill_and_status(tmp_path: Path, capsys) -> None:
    claude = tmp_path / "claude"
    _write_transcript(claude / "projects/p/sess-1.jsonl", _main_records())
    db = tmp_path / "usage.db"
    assert writer.main(["--backfill", "--db", str(db), "--claude-dir", str(claude)]) == 0
    assert "Backfilled 1 Claude Code sessions" in capsys.readouterr().err
    assert writer.main(["--status", "--db", str(db)]) == 0
    assert PROVIDER in capsys.readouterr().out
    assert writer.main(["--backfill", "--db", str(db), "--claude-dir", str(tmp_path / "nope")]) == 1


@pytest.mark.parametrize("hook_first", [False, True])
def test_same_id_backfill_and_hook_preserve_file_revisions(tmp_path, hook_first):
    claude = tmp_path / "claude"
    a = _write_transcript(claude / "projects/p/a.jsonl", [
        _assistant("resumed", "a-response", "2026-09-01T10:00:00Z", out=20),
    ])
    _write_transcript(claude / "projects/p/b.jsonl", [
        _assistant("resumed", "b-response", "2026-09-01T11:00:00Z", out=10),
    ])
    db = tmp_path / "usage.db"
    payload = {"hook_event_name": "Stop", "transcript_path": str(a)}
    if hook_first:
        writer.process_hook_payload(payload, db_path=db)
    writer.backfill_claude_usage(db_path=db, claude_dir=claude, enable_capture=False)
    writer.process_hook_payload(payload, db_path=db)
    (session,) = read_usage_sessions(PROVIDER, db)
    assert session.usage.total_tokens == 290
    assert len(session.metadata["capture_sources"]) == 2
    assert len({e.metadata["capture_source_hash"] for e in session.events}) == 2


def test_same_id_copied_response_backfill_then_hooks_preserve_provenance(tmp_path):
    claude = tmp_path / "claude"
    paths = [_write_transcript(claude / f"projects/p/{name}.jsonl", [
        _assistant("resumed", "shared", "2026-09-01T10:00:00Z", out=10),
    ]) for name in ("a", "b")]
    db = tmp_path / "usage.db"
    writer.backfill_claude_usage(db_path=db, claude_dir=claude, enable_capture=False)
    for path in paths:
        writer.process_hook_payload({"hook_event_name": "Stop", "transcript_path": str(path)}, db_path=db)
    (session,) = read_usage_sessions(PROVIDER, db)
    assert session.usage.total_tokens == 140
    assert len(session.metadata["capture_sources"]) == 2
    assert len(session.events[0].metadata["capture_source_hashes"]) == 2


def test_concurrent_hooks_claim_copied_response_atomically(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from src.usage_store import ensure_schema

    db = tmp_path / "usage.db"
    ensure_schema(db)
    paths = [_write_transcript(tmp_path / f"projects/p/{name}.jsonl", [
        _assistant(name, "shared", "2026-09-01T10:00:00Z", out=10),
    ]) for name in ("a", "b")]
    barrier = Barrier(2)
    original_parse = writer._parse_stable
    def parse(path):
        parsed = original_parse(path)
        barrier.wait(timeout=5)
        return parsed

    monkeypatch.setattr(writer, "_parse_stable", parse)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda path: writer.process_hook_payload(
            {"hook_event_name": "Stop", "transcript_path": str(path)}, db_path=db,
        ), paths))
    assert sum(s.usage.total_tokens for s in read_usage_sessions(PROVIDER, db)) == 140
    assert sorted(value or 0 for value in results) == [0, 140]
