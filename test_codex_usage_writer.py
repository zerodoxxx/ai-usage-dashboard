"""Tests for Codex completion-hook routing and its safe CLI protocol."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.parsers.codex as codex_parser
import scripts.codex_usage_writer as writer
from src.parsers.contracts import TokenUsage, UsageEvent, UsageSession
from src.usage_store import is_provider_capture_enabled, read_usage_sessions


def _session(session_id: str = "thread-1") -> UsageSession:
    return UsageSession(
        id=session_id,
        tool="codex",
        provider="codex",
        model="gpt-5-codex",
        start_time=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
        usage=TokenUsage(input_tokens=80, cached_input_tokens=20, output_tokens=30, total_tokens=110),
        events=[
            UsageEvent(
                timestamp=datetime(2026, 9, 23, 12, 1, tzinfo=timezone.utc),
                model="gpt-5-codex",
                usage=TokenUsage(input_tokens=80, cached_input_tokens=20, output_tokens=30, total_tokens=110),
            )
        ],
    )


def test_stop_uses_main_session_transcript_and_writes_compact_usage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    def extract(**kwargs):
        calls.append(kwargs)
        return _session(kwargs["session_id"])

    monkeypatch.setattr(codex_parser, "extract_codex_session_for_capture", extract)
    db_path = tmp_path / "usage.db"
    result = writer.process_hook_payload(
        {
            "hook_event_name": "Stop",
            "session_id": "main-thread",
            "transcript_path": "/private/codex/rollout-main.jsonl",
            "model": "gpt-5-codex",
        },
        db_path=db_path,
        codex_dir=tmp_path / "codex",
    )

    assert result == 110
    assert calls == [{
        "transcript_path": "/private/codex/rollout-main.jsonl",
        "session_id": "main-thread",
        "model": "gpt-5-codex",
        "codex_dir": tmp_path / "codex",
    }]
    loaded = read_usage_sessions("codex", db_path)
    assert len(loaded) == 1
    assert loaded[0].id == "main-thread"
    assert loaded[0].usage.total_tokens == 110


def test_subagent_stop_targets_agent_transcript_and_never_parent_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    def extract(**kwargs):
        calls.append(kwargs)
        return _session(kwargs["session_id"])

    monkeypatch.setattr(codex_parser, "extract_codex_session_for_capture", extract)
    db_path = tmp_path / "usage.db"
    result = writer.process_hook_payload(
        {
            "hook_event_name": "SubagentStop",
            "session_id": "parent-thread",
            "agent_id": "child-agent-id",
            "agent_transcript_path": "/private/codex/agent-child.jsonl",
            "model": "parent-model-must-not-be-used",
        },
        db_path=db_path,
    )

    assert result == 110
    assert calls == [{
        "transcript_path": "/private/codex/agent-child.jsonl",
        "session_id": "child-agent-id",
        "model": None,
        "codex_dir": None,
    }]
    assert [session.id for session in read_usage_sessions("codex", db_path)] == [
        "child-agent-id"
    ]


def test_no_usage_is_a_successful_skip_and_does_not_create_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(codex_parser, "extract_codex_session_for_capture", lambda **_: None)
    db_path = tmp_path / "usage.db"

    result = writer.process_hook_payload(
        {
            "hook_event_name": "SessionEnd",
            "session_id": "empty-thread",
            "transcript_path": "/private/codex/empty.jsonl",
        },
        db_path=db_path,
    )

    assert result is None
    assert not db_path.exists()
    assert writer.process_hook_payload({}, db_path=db_path) is None


def test_hook_errors_are_nonblocking_json_and_do_not_echo_private_payload(
    monkeypatch,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(writer.sys, "stdin", io.StringIO('{"secret":"private text"'))
    monkeypatch.setattr(writer.sys, "stdout", stdout)
    monkeypatch.setattr(writer.sys, "stderr", stderr)

    result = writer.main([])

    assert result == 0
    assert stdout.getvalue() == "{}\n"
    assert "JSONDecodeError" in stderr.getvalue()
    assert "private text" not in stderr.getvalue()


def test_backfill_enables_database_mode_only_after_import_succeeds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    db_path = tmp_path / "usage.db"
    monkeypatch.setattr(
        codex_parser,
        "extract_all_codex_sessions_for_capture",
        lambda **_: [_session("backfilled-thread")],
    )

    assert writer.backfill_codex_usage(db_path=db_path, codex_dir=codex_dir) == 1
    assert is_provider_capture_enabled("codex", db_path)
    assert [session.id for session in read_usage_sessions("codex", db_path)] == [
        "backfilled-thread"
    ]


def test_backfill_failure_does_not_activate_database_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    db_path = tmp_path / "usage.db"
    monkeypatch.setattr(
        codex_parser,
        "extract_all_codex_sessions_for_capture",
        lambda **_: [_session("backfilled-thread")],
    )
    monkeypatch.setattr(
        writer,
        "write_usage_sessions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    try:
        writer.backfill_codex_usage(db_path=db_path, codex_dir=codex_dir)
    except RuntimeError:
        pass
    else:
        raise AssertionError("backfill write failure should propagate")

    assert not is_provider_capture_enabled("codex", db_path)


def test_backfill_is_a_noop_after_capture_has_been_activated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    db_path = tmp_path / "usage.db"
    monkeypatch.setattr(
        codex_parser,
        "extract_all_codex_sessions_for_capture",
        lambda **_: [_session("backfilled-thread")],
    )
    writer.backfill_codex_usage(db_path=db_path, codex_dir=codex_dir)

    assert writer.backfill_codex_usage(db_path=db_path, codex_dir=codex_dir) is None
    assert [session.id for session in read_usage_sessions("codex", db_path)] == [
        "backfilled-thread"
    ]


def test_status_output_has_only_aggregate_data(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "usage.db"
    writer.write_usage_sessions("codex", [_session("private-thread-id")], db_path)
    stdout = io.StringIO()
    monkeypatch.setattr(writer.sys, "stdout", stdout)

    assert writer.main(["--status", "--db", str(db_path)]) == 0
    status = json.loads(stdout.getvalue())
    assert status["database_exists"] is True
    assert status["providers"]["codex"]["sessions"] == 1
    assert status["providers"]["codex"]["events"] == 1
    assert "private-thread-id" not in stdout.getvalue()
    assert "usage test" not in stdout.getvalue()


def test_cli_stop_with_real_codex_payload_shape_writes_row(tmp_path: Path) -> None:
    """Regression: the exact Stop payload Codex 0.159 sends, over a real pipe.

    The writer is run as the hook runs it (subprocess, JSON on stdin, cwd
    elsewhere, stdout/stderr piped) against a rollout shaped like a real
    ``codex exec`` transcript, and must persist the row.
    """
    import subprocess
    import sys

    thread = "01a0fcd9-a16d-7db2-88f0-b31464da79ff"
    turn = "01a0fcd9-a216-7331-8ab6-dc9254da1560"
    usage = {
        "input_tokens": 22276, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
        "output_tokens": 5, "reasoning_output_tokens": 0, "total_tokens": 22281,
    }
    lines = [
        {"timestamp": "2026-10-02T13:41:57.180Z", "ordinal": 0, "type": "session_meta",
         "payload": {"id": thread, "session_id": thread, "timestamp": "2026-10-02T13:41:57.100Z",
                     "cwd": "/private/tmp", "originator": "codex_exec", "source": "exec"}},
        {"timestamp": "2026-10-02T13:41:57.181Z", "ordinal": 1, "type": "turn_context",
         "payload": {"turn_id": turn, "model": "gpt-6.1-sol"}},
        {"timestamp": "2026-10-02T13:42:05.589Z", "ordinal": 2, "type": "token_usage_record",
         "payload": {"thread_id": thread, "turn_id": turn, "session_id": thread,
                     "response_id": "resp_1", "usage": usage}},
        {"timestamp": "2026-10-02T13:42:05.590Z", "ordinal": 3, "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": usage, "last_token_usage": usage}}},
        {"timestamp": "2026-10-02T13:42:05.654Z", "ordinal": 4, "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": turn, "last_agent_message": "ok"}},
    ]
    rollout = tmp_path / f"rollout-2026-10-02T19-11-57-{thread}.jsonl"
    rollout.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    payload = {
        "hook_event_name": "Stop",
        "session_id": thread,
        "turn_id": turn,
        "transcript_path": str(rollout),
        "cwd": "/private/tmp",
        "model": "gpt-6.1-sol",
        "permission_mode": "default",
        "stop_hook_active": False,
        "last_assistant_message": "ok",
    }
    db_path = tmp_path / "usage.db"
    script = Path(writer.__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--codex-dir", str(tmp_path / "home"),
         "--deadline-seconds", "25"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}
    assert "captured (22281 tokens)" in result.stderr
    loaded = read_usage_sessions("codex", db_path)
    assert [s.id for s in loaded] == [thread]
    assert loaded[0].usage.total_tokens == 22281


@pytest.mark.parametrize("existing_schema", [True, False])
def test_locked_interrupt_returns_json_before_hook_timeout(
    tmp_path: Path, existing_schema: bool,
) -> None:
    import sqlite3
    import subprocess
    import sys

    db_path = tmp_path / "usage.db"
    if existing_schema:
        writer.write_usage_sessions("codex", [_session()], db_path)
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({
        "timestamp": "2026-10-02T13:42:05Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 80, "output_tokens": 30, "total_tokens": 110,
        }}},
    }) + "\n", encoding="utf-8")
    payload = {
        "hook_event_name": "Interrupt",
        "session_id": "locked-thread",
        "transcript_path": str(rollout),
    }
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = subprocess.run(
            [sys.executable, str(Path(writer.__file__).resolve()),
             "--db", str(db_path), "--codex-dir", str(tmp_path),
             "--deadline-seconds", "2.5"],
            input=json.dumps(payload), capture_output=True, text=True,
            cwd=tmp_path, timeout=3, check=False,
        )
    finally:
        connection.rollback()
        connection.close()

    assert result.returncode == 0
    assert result.stdout == "{}\n"
    if existing_schema:
        assert "deadline" in result.stderr.casefold()
    else:
        # Journal-mode migration can reject a lock immediately and exhaust
        # its retry count before the deadline; that is also a safe skip.
        assert "capture skipped" in result.stderr.casefold()
    assert [session.id for session in read_usage_sessions("codex", db_path)] == (
        ["thread-1"] if existing_schema else []
    )


def test_hook_passes_monotonic_deadline_to_store(tmp_path: Path, monkeypatch) -> None:
    seen = {}

    def fake_write(provider, sessions, db_path=None, *, deadline=None):
        seen["deadline"] = deadline
        return 1

    monkeypatch.setattr(writer, "write_usage_sessions", fake_write)
    monkeypatch.setattr(
        codex_parser, "extract_codex_session_for_capture", lambda **_kw: _session(),
    )
    monkeypatch.setattr(writer.sys, "stdin", io.StringIO(json.dumps({
        "hook_event_name": "Stop", "session_id": "t",
        "transcript_path": str(tmp_path / "rollout.jsonl"),
    })))
    before = writer.time.monotonic()
    assert writer.main(["--db", str(tmp_path / "u.db"), "--deadline-seconds", "5"]) == 0
    assert before + 4.9 <= seen["deadline"] <= writer.time.monotonic() + 5.1


def test_deadline_includes_transcript_parsing(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    def extract(**_kwargs):
        writer.time.sleep(0.2)
        return _session()

    monkeypatch.setattr(codex_parser, "extract_codex_session_for_capture", extract)
    monkeypatch.setattr(writer.sys, "stdin", io.StringIO(json.dumps({
        "hook_event_name": "Interrupt", "session_id": "slow-thread",
        "transcript_path": str(tmp_path / "rollout.jsonl"),
    })))
    db_path = tmp_path / "usage.db"
    assert writer.main(["--db", str(db_path), "--deadline-seconds", "0.02"]) == 0
    output = capsys.readouterr()
    assert output.out == "{}\n"
    assert "deadline expired" in output.err
    assert not db_path.exists()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
def test_deadline_rejects_nonpositive_or_nonfinite_values(value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        writer._arguments(["--deadline-seconds", value])
    assert exc.value.code == 2
