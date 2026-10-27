"""Antigravity writer regressions, isolated from real hooks and databases."""
from __future__ import annotations

import builtins
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import agy_usage_writer as writer
from src.agy_conversation_usage import UNPAIRED_STEP_BASE, read_conversation_calls
from src.parsers.contracts import TokenUsage, UsageSession
from src.pricing import calculate_cost_strict
from src.usage_store import ensure_schema, read_usage_sessions, write_usage_sessions
from tests_agy_support import create_conversation_db, gen_row, usage_message, write_conversation


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


# --- Real token usage: conversation databases (tier 1), transcript JSON (tier 2) ---

REAL_ID = "11111111-aaaa-bbbb-cccc-222222222222"
START = 1_790_000_000
MIXED_CALLS = [
    {"uncached": 100, "cache_read": 900, "output": 50, "reasoning": 20, "model": "gemini-3.8-flash"},
    {"uncached": 10, "output": 5, "model": "claude-sonnet-5-5"},
    {"uncached": 7, "cache_read": 3, "output": 2, "reasoning": 1, "model": "totally-unknown-model"},
]
_SUM_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


@pytest.fixture
def agy(tmp_path, monkeypatch):
    """An agy directory with a settings label; tokenizing is switched to the offline fallback."""
    monkeypatch.setattr(writer, "_ENC", None)
    monkeypatch.setattr(writer, "_ENCODING_ATTEMPTED", True)
    base = tmp_path / "agy"
    base.mkdir()
    (base / "settings.json").write_text(json.dumps({"model": "gemini-2.5-flash"}))
    return base


def _write_transcript(base, session_id, steps):
    path = base / "brain" / session_id / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(step) for step in steps) + "\n")
    return path


def _plain_steps():
    """A transcript with no recorded counts: one estimated call."""
    return [
        {"created_at": "2026-10-01T10:00:00Z", "source": "USER_EXPLICIT", "content": "one two"},
        {"created_at": "2026-10-01T10:01:00Z", "type": "PLANNER_RESPONSE", "content": "yes!"},
    ]


def _keyed_steps(counts):
    """A transcript whose PLANNER_RESPONSE lines carry (uncached, cache_read, output) counts."""
    steps = [{"created_at": "2026-10-02T09:59:00Z", "source": "USER_EXPLICIT", "content": "hello"}]
    for index, (uncached, cache_read, output) in enumerate(counts):
        steps.append({
            "type": "PLANNER_RESPONSE", "step_index": 10 + index, "content": "reply",
            "created_at": f"2026-10-02T10:0{index}:00Z",
            "input_tokens": uncached, "cache_read_tokens": cache_read, "output_tokens": output,
        })
    return steps


def _hook(base, db, session_id=REAL_ID, **payload):
    return writer.process_hook_payload({"conversationId": session_id, **payload}, db_path=db, agy_dir=base)


def _stored(db):
    sessions = read_usage_sessions("antigravity", db_path=db)
    assert len(sessions) == 1
    return sessions[0]


def _utc(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


@pytest.mark.parametrize("session_id", ["abc-123_x.y", REAL_ID, "a..b", "UPPER.lower-9"])
def test_conversation_db_path_accepts_plain_ids(tmp_path, session_id):
    assert writer.conversation_db_path(tmp_path, session_id) == tmp_path / "conversations" / f"{session_id}.db"


@pytest.mark.parametrize(
    "session_id",
    ["../x", "a/b", "..", ".hidden", "", "a b", "x\n", "a\\b", "../conversations/x", "/etc/passwd", None, 123],
)
def test_conversation_db_path_refuses_unsafe_ids(tmp_path, session_id):
    assert writer.conversation_db_path(tmp_path, session_id) is None


def test_tier1_reads_recorded_counts_per_call(agy):
    write_conversation(agy, REAL_ID, MIXED_CALLS, start=START)
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier1"
    first, second, third = session.events
    assert (first.usage.input_tokens, first.usage.cached_input_tokens) == (1000, 900)
    assert (first.usage.output_tokens, first.usage.reasoning_output_tokens, first.usage.total_tokens) == (50, 20, 1050)
    assert (second.usage.input_tokens, second.usage.cached_input_tokens, second.usage.total_tokens) == (10, 0, 15)
    assert (third.usage.input_tokens, third.usage.cached_input_tokens, third.usage.total_tokens) == (10, 3, 12)
    assert all(event.usage.cache_write_tokens == 0 for event in session.events)
    assert [event.event_id for event in session.events] == [f"{REAL_ID}:call:{step}" for step in (1, 3, 5)]
    assert [event.model for event in session.events] == ["gemini-3.8-flash", "claude-sonnet-5-5", "totally-unknown-model"]
    assert [event.timestamp for event in session.events] == [_utc(START + 60 * index) for index in range(3)]

    assert session.model == "mixed"
    assert session.call_count == len(session.events) == 3
    for key in _SUM_KEYS:
        assert getattr(session.usage, key) == sum(getattr(event.usage, key) for event in session.events)
    assert session.usage.input_tokens == 1020 and session.usage.cached_input_tokens == 903
    assert session.usage.output_tokens == 57 and session.usage.reasoning_output_tokens == 21
    assert session.usage.total_tokens == 1077 and session.usage.cache_write_tokens == 0
    assert session.created_at == session.start_time == _utc(START)
    assert session.activity_at == session.end_time == _utc(START + 120)
    assert session.title == f"Session {REAL_ID[:8]}"
    # Exactly these keys: no capture_* revision fields on a real snapshot.
    assert session.metadata == {
        "estimated": False, "token_source": "reported", "token_origin": "agy-conversation-db",
    }


def test_tier1_prices_each_call_with_a_provider_inferred_from_its_model(agy):
    write_conversation(agy, REAL_ID, MIXED_CALLS, start=START)
    session, _tier = writer.capture_session(agy, REAL_ID)
    first, second, third = session.events
    gemini = calculate_cost_strict("gemini-3.8-flash", 100, 900, 50, provider="antigravity")
    claude = calculate_cost_strict("claude-sonnet-5-5", 10, 0, 5, provider="claude")
    assert gemini["cost_cached_usd"] is not None and claude["cost_cached_usd"] is not None
    # The premise: a Claude id is unknown under the antigravity provider.
    assert calculate_cost_strict("claude-sonnet-5-5", 10, 0, 5, provider="antigravity")["cost_cached_usd"] is None
    assert float(first.cost.cached_usd) == gemini["cost_cached_usd"]
    assert float(second.cost.cached_usd) == claude["cost_cached_usd"]
    assert third.cost is None
    assert session.cost.source == "estimated" and session.cost.reported_usd is None
    assert float(session.cost.cached_usd) == pytest.approx(gemini["cost_cached_usd"] + claude["cost_cached_usd"])
    assert float(session.cost.uncached_usd) == pytest.approx(gemini["cost_uncached_usd"] + claude["cost_uncached_usd"])
    assert float(session.cost.savings_usd) == pytest.approx(gemini["savings_usd"] + claude["savings_usd"])


def test_tier1_session_with_no_priced_call_has_no_cost(agy):
    write_conversation(agy, REAL_ID, [{"uncached": 5, "output": 5, "model": "totally-unknown-model"}])
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier1" and session.model == "totally-unknown-model"
    assert session.events[0].cost is None and session.cost is None


@pytest.mark.parametrize("model, provider", [
    ("deepseek-v4", "deepseek"), ("DeepSeek-Chat", "deepseek"),
    ("gemini-3.8-flash", "antigravity"), ("  Gemini-3.8-Flash-Tiered ", "antigravity"),
    ("claude-sonnet-5-5", "claude"), ("Claude-Opus-5", "claude"), ("sonnet-4", "claude"),
    ("OPUS-4", "claude"), ("haiku-4", "claude"),
    ("gpt-5", "codex"), ("GPT-oss-120b", "codex"), ("o1-mini", "codex"), ("o3", "codex"), ("o4-mini", "codex"),
    # Prefix rules, not a substring search: the read path answers the same way.
    ("anthropic/sonnet-x", "antigravity"), ("models/claude-3", "antigravity"),
    ("totally-unknown-model", "antigravity"), ("oasis-1", "antigravity"), ("", "antigravity"),
])
def test_model_provider_follows_the_aggregator_rules(model, provider):
    from src.parsers import aggregator

    assert writer._model_provider(model) == provider == aggregator._model_provider(model, "antigravity")


def test_tier1_call_without_a_model_takes_the_most_common_id(agy):
    flash = "gemini-3.8-flash"
    calls = [
        {"uncached": 1, "output": 1, "model": flash}, {"uncached": 1, "output": 1, "model": flash},
        {"uncached": 1, "output": 1}, {"uncached": 1, "output": 1, "model": "claude-sonnet-5-5"},
    ]
    write_conversation(agy, REAL_ID, calls)
    session, _tier = writer.capture_session(agy, REAL_ID)
    assert [event.model for event in session.events] == [flash, flash, flash, "claude-sonnet-5-5"]
    assert session.model == "mixed"


def test_tier1_without_any_model_uses_the_settings_label(agy):
    write_conversation(agy, REAL_ID, [{"uncached": 1, "output": 1}, {"uncached": 2, "output": 2}])
    session, _tier = writer.capture_session(agy, REAL_ID)
    assert [event.model for event in session.events] == ["gemini-2.5-flash"] * 2
    assert session.model == "gemini-2.5-flash"


def test_tier1_single_distinct_model_is_the_session_model(agy):
    calls = [{"uncached": 1, "output": 1, "model": "gemini-3.8-flash"}, {"uncached": 2, "output": 2}]
    write_conversation(agy, REAL_ID, calls)
    session, _tier = writer.capture_session(agy, REAL_ID)
    assert session.model == "gemini-3.8-flash"


def _untimed_conversation(agy):
    """A conversation database whose one call has no planner step, hence no timestamp."""
    path = agy / "conversations" / f"{REAL_ID}.db"
    path.parent.mkdir()
    create_conversation_db(path, [(4, gen_row(usage_message(uncached=3, output=2, cache_read=1)))])
    os.utime(path, (START, START))
    return path


def _stored_dates(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT timestamp, date, created_at, start_time, end_time, activity_at FROM sessions"
        ).fetchall()


def test_tier1_untimed_calls_date_the_session_from_the_database_file(agy):
    _untimed_conversation(agy)
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier1" and session.events[0].event_id == f"{REAL_ID}:call:{UNPAIRED_STEP_BASE + 4}"
    assert session.events[0].timestamp is None
    assert session.created_at == session.start_time == session.end_time == session.activity_at == _utc(START)
    assert _hook(agy, agy.parent / "untimed.db") == 6


def test_tier1_untimed_session_keeps_its_stored_date_across_replays(agy, tmp_path):
    _untimed_conversation(agy)
    db = tmp_path / "untimed-replay.db"
    assert _hook(agy, db) == 6
    first = _stored_dates(db)
    time.sleep(0.05)  # a "now" fallback would move the stored timestamp
    assert _hook(agy, db) == 6
    assert _stored_dates(db) == first
    stamp = _utc(START).isoformat()
    assert first == [(stamp, stamp[:10], stamp, stamp, stamp, stamp)]


def test_tier2_untimed_session_keeps_its_stored_date_across_replays(agy, tmp_path):
    steps = _keyed_steps([(100, 900, 50), (20, 0, 5)])
    for step in steps:
        step.pop("created_at", None)
    transcript = _write_transcript(agy, REAL_ID, steps)
    os.utime(transcript, (START, START))
    session = writer.parse_transcript_json_usage(transcript, REAL_ID, agy)
    assert all(event.timestamp is None for event in session.events)
    assert session.created_at == session.activity_at == _utc(START)
    db = tmp_path / "untimed-tier2.db"
    assert _hook(agy, db) == 1075
    first = _stored_dates(db)
    time.sleep(0.05)
    assert _hook(agy, db) == 1075
    assert _stored_dates(db) == first
    stamp = _utc(START).isoformat()
    assert first == [(stamp, stamp[:10], stamp, stamp, stamp, stamp)]


def test_tier1_never_tokenizes(agy, monkeypatch):
    monkeypatch.setattr(writer, "_ENCODING_ATTEMPTED", False)
    monkeypatch.setattr(writer, "count_tokens", lambda text: pytest.fail("tier 1 must not tokenize"))
    write_conversation(agy, REAL_ID, MIXED_CALLS)
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier1" and session.usage.total_tokens == 1077
    assert writer._ENCODING_ATTEMPTED is False


def test_tier1_never_imports_tiktoken(agy, tmp_path, monkeypatch):
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] == "tiktoken":
            pytest.fail("tier 1 must not import tiktoken")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(writer, "_ENCODING_ATTEMPTED", False)
    write_conversation(agy, REAL_ID, MIXED_CALLS)
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", guarded)
        assert _hook(agy, tmp_path / "no-tiktoken.db") == 1077
    assert _stored(tmp_path / "no-tiktoken.db").metadata["token_origin"] == "agy-conversation-db"


def test_tier1_replay_is_idempotent(agy, tmp_path):
    db = tmp_path / "replay.db"
    write_conversation(agy, REAL_ID, MIXED_CALLS, start=START)
    total = _hook(agy, db)
    assert total == 1077 and _hook(agy, db) == total
    stored = _stored(db)
    assert len(stored.events) == stored.call_count == 3
    assert stored.usage.total_tokens == 1077 and stored.usage.cached_input_tokens == 903
    assert stored.metadata["token_origin"] == "agy-conversation-db"
    assert stored.metadata["estimated"] is False and stored.metadata["token_source"] == "reported"


def test_real_snapshot_replaces_an_earlier_estimate_and_estimates_cannot_replace_it(agy, tmp_path):
    db = tmp_path / "precedence.db"
    transcript = _write_transcript(agy, REAL_ID, _plain_steps())
    assert _hook(agy, db) is not None
    assert _stored(db).metadata["estimated"] is True

    write_conversation(agy, REAL_ID, MIXED_CALLS)
    assert _hook(agy, db) == 1077
    stored = _stored(db)
    assert stored.metadata["estimated"] is False and stored.metadata["token_origin"] == "agy-conversation-db"
    assert len(stored.events) == 3 and stored.usage.total_tokens == 1077

    # A later, larger estimate (the database is gone) is rejected by the store.
    (agy / "conversations" / f"{REAL_ID}.db").unlink()
    with transcript.open("a") as stream:
        stream.write(json.dumps({"source": "MODEL", "content": "a much longer reply " * 50}) + "\n")
    assert _hook(agy, db) is None
    stored = _stored(db)
    assert stored.usage.total_tokens == 1077 and stored.metadata["estimated"] is False


def test_snapshot_with_fewer_calls_is_rejected_and_a_longer_one_accepted(agy, tmp_path, monkeypatch):
    db = tmp_path / "fewer.db"
    _write_transcript(agy, REAL_ID, _plain_steps())
    write_conversation(agy, REAL_ID, MIXED_CALLS)
    assert _hook(agy, db) == 1077

    attempts = []
    original = writer._write_sessions
    monkeypatch.setattr(writer, "_write_sessions", lambda sessions, db_path=None: (
        attempts.append(sessions[0].metadata.get("token_origin")) or original(sessions, db_path)
    ))
    write_conversation(agy, REAL_ID, MIXED_CALLS[:2])
    assert _hook(agy, db) is None
    # The rejected tier-1 snapshot is the only attempt: nothing lower is tried.
    assert attempts == ["agy-conversation-db"]
    assert _stored(db).call_count == 3 and _stored(db).usage.total_tokens == 1077

    write_conversation(agy, REAL_ID, [*MIXED_CALLS, {"uncached": 4, "output": 1, "model": "gemini-3.8-flash"}])
    assert _hook(agy, db) == 1082
    assert _stored(db).call_count == 4


def test_corrupt_conversation_db_never_blocks_agy_and_falls_to_the_estimate(agy, tmp_path):
    (agy / "conversations").mkdir()
    (agy / "conversations" / f"{REAL_ID}.db").write_bytes(b"this is not a database" * 200)
    _write_transcript(agy, REAL_ID, _plain_steps())
    db = tmp_path / "corrupt.db"
    result = subprocess.run(
        [sys.executable, str(Path(writer.__file__)), "--agy-dir", str(agy), "--db", str(db)],
        input=json.dumps({"conversationId": REAL_ID}), capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    stored = _stored(db)
    assert stored.metadata["estimated"] is True and "token_origin" not in stored.metadata


def test_tier1_failure_degrades_to_the_next_tier(agy, monkeypatch, capsys):
    write_conversation(agy, REAL_ID, MIXED_CALLS)
    _write_transcript(agy, REAL_ID, _plain_steps())

    def explode(*args, **kwargs):
        raise RuntimeError("private detail")

    monkeypatch.setattr(writer, "build_real_session", explode)
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier3" and session.metadata["estimated"] is True
    assert "private detail" not in capsys.readouterr().err


def test_traversal_id_skips_tier1(agy, tmp_path):
    write_conversation(agy, REAL_ID, MIXED_CALLS)
    traversal = f"../conversations/{REAL_ID}"
    # Resolved naively, this id would land on the real database above.
    assert writer.conversation_db_path(agy, traversal) is None
    assert writer.capture_session(agy, traversal) == (None, "skipped")
    db = tmp_path / "traversal.db"
    assert _hook(agy, db, traversal) is None and not db.exists()

    # With a transcript reachable through the same id, the estimate (tier 3) is used.
    (agy / "brain").mkdir()
    transcript = agy / "conversations" / REAL_ID / "transcript.jsonl"
    transcript.parent.mkdir()
    transcript.write_text("\n".join(json.dumps(step) for step in _plain_steps()) + "\n")
    session, tier = writer.capture_session(agy, traversal)
    assert tier == "tier3" and session.metadata["estimated"] is True


def test_zero_call_database_falls_through_to_the_estimate(agy):
    write_conversation(agy, REAL_ID, [{"uncached": 0, "output": 0}])
    assert read_conversation_calls(agy / "conversations" / f"{REAL_ID}.db") == []
    assert writer.capture_session(agy, REAL_ID) == (None, "skipped")
    _write_transcript(agy, REAL_ID, _plain_steps())
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier3" and session.metadata["estimated"] is True


def test_payload_transcript_path_never_locates_the_database(agy, tmp_path):
    other = tmp_path / "other"
    write_conversation(other, REAL_ID, MIXED_CALLS)
    transcript = _write_transcript(other, REAL_ID, _plain_steps())
    db = tmp_path / "hint.db"
    assert agy / "conversations" not in transcript.parents
    assert _hook(agy, db, transcriptPath=str(transcript)) is not None
    stored = _stored(db)
    assert stored.metadata["estimated"] is True and "token_origin" not in stored.metadata


def test_tier3_estimate_is_unchanged_and_has_no_origin(agy):
    _write_transcript(agy, REAL_ID, _plain_steps())
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier3" and session.call_count == 1
    assert session.metadata["estimated"] is True and session.metadata["token_source"] == "estimated"
    assert "token_origin" not in session.metadata and "capture_source_hash" in session.metadata
    assert session.events[0].event_id == f"{REAL_ID}:step:1"


def test_tier2_reads_planner_response_counts(agy):
    path = _write_transcript(agy, REAL_ID, _keyed_steps([(100, 900, 50), (20, 0, 5)]))
    session = writer.parse_transcript_json_usage(path, REAL_ID, agy)
    first, second = session.events
    assert (first.usage.input_tokens, first.usage.cached_input_tokens, first.usage.output_tokens) == (1000, 900, 50)
    assert (second.usage.input_tokens, second.usage.cached_input_tokens, second.usage.output_tokens) == (20, 0, 5)
    assert first.usage.total_tokens == 1050 and second.usage.total_tokens == 25
    assert first.usage.reasoning_output_tokens == 0 and first.usage.cache_write_tokens == 0
    assert [event.event_id for event in session.events] == [f"{REAL_ID}:call:10", f"{REAL_ID}:call:11"]
    assert first.timestamp == datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    assert [event.model for event in session.events] == ["gemini-2.5-flash"] * 2
    assert session.model == "gemini-2.5-flash" and first.cost is not None
    assert session.call_count == len(session.events) == 2
    for key in _SUM_KEYS:
        assert getattr(session.usage, key) == sum(getattr(event.usage, key) for event in session.events)
    assert session.usage.total_tokens == 1075
    assert session.created_at == first.timestamp and session.activity_at == second.timestamp
    assert session.metadata == {
        "estimated": False, "token_source": "reported", "token_origin": "agy-transcript-json",
    }


def test_tier2_is_used_through_the_hook_and_reaches_the_store(agy, tmp_path):
    _write_transcript(agy, REAL_ID, _keyed_steps([(100, 900, 50), (20, 0, 5)]))
    db = tmp_path / "tier2.db"
    assert _hook(agy, db) == 1075
    stored = _stored(db)
    assert stored.metadata["token_origin"] == "agy-transcript-json" and stored.call_count == 2
    assert stored.metadata["estimated"] is False


@pytest.mark.parametrize("key, value", [
    ("cache_read_tokens", "<missing>"), ("input_tokens", "<missing>"), ("output_tokens", "<missing>"),
    ("cache_read_tokens", None), ("input_tokens", "12"), ("output_tokens", 1.5),
    ("input_tokens", True), ("output_tokens", -3),
])
def test_tier2_is_all_or_nothing(agy, key, value):
    steps = _keyed_steps([(100, 900, 50), (20, 0, 5), (7, 3, 2)])
    if value == "<missing>":
        del steps[2][key]
    else:
        steps[2][key] = value
    path = _write_transcript(agy, REAL_ID, steps)
    assert writer.parse_transcript_json_usage(path, REAL_ID, agy) is None
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier3" and session.metadata["estimated"] is True


def test_tier2_event_timestamps_accept_only_nonempty_strings(agy):
    steps = _keyed_steps([(100, 900, 50), (20, 0, 5), (7, 3, 2), (1, 1, 1)])
    steps[2]["created_at"] = START  # numeric: not a usable timestamp
    steps[4]["created_at"] = ""
    _write_transcript(agy, REAL_ID, steps)
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier2" and session.call_count == 4
    first, last = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 2, 10, 2, tzinfo=timezone.utc)
    assert [event.timestamp for event in session.events] == [first, None, last, None]
    assert session.created_at == first and session.activity_at == last


def test_tier2_needs_at_least_one_nonzero_call(agy):
    path = _write_transcript(agy, REAL_ID, _plain_steps()[:1])
    assert writer.parse_transcript_json_usage(path, REAL_ID, agy) is None
    path = _write_transcript(agy, REAL_ID, _keyed_steps([(0, 0, 0)]))
    assert writer.parse_transcript_json_usage(path, REAL_ID, agy) is None


def test_tier2_event_ids_stay_unique_without_usable_step_indexes(agy):
    steps = _keyed_steps([(1, 0, 1), (2, 0, 2), (3, 0, 3)])
    del steps[1]["step_index"]
    steps[3]["step_index"] = steps[2]["step_index"]
    path = _write_transcript(agy, REAL_ID, steps)
    session = writer.parse_transcript_json_usage(path, REAL_ID, agy)
    ids = [event.event_id for event in session.events]
    assert len(set(ids)) == 3 and ids[1] == f"{REAL_ID}:call:11"
    assert ids[0] == f"{REAL_ID}:call:{UNPAIRED_STEP_BASE + 1}"


def test_tier1_wins_over_tier2(agy):
    write_conversation(agy, REAL_ID, MIXED_CALLS)
    _write_transcript(agy, REAL_ID, _keyed_steps([(1, 0, 1)]))
    session, tier = writer.capture_session(agy, REAL_ID)
    assert tier == "tier1" and session.metadata["token_origin"] == "agy-conversation-db"


def _populate_backfill(agy):
    """Two tier-1, one tier-2, two tier-3 and two skipped sessions."""
    write_conversation(agy, "aaaaaaaa-1", MIXED_CALLS)  # tier 1, transcript ignored
    _write_transcript(agy, "aaaaaaaa-1", _plain_steps())
    write_conversation(agy, "bbbbbbbb-2", MIXED_CALLS[:2])  # tier 1, no transcript
    write_conversation(agy, "cccccccc-3", [{"uncached": 0, "output": 0}])  # zero calls -> tier 3
    _write_transcript(agy, "cccccccc-3", _plain_steps())
    _write_transcript(agy, "dddddddd-4", _keyed_steps([(5, 5, 5), (6, 0, 1)]))  # tier 2
    _write_transcript(agy, "eeeeeeee-5", _plain_steps())  # tier 3
    (agy / "conversations" / ".hidden.db").write_bytes(b"x")  # unsafe id -> skipped
    (agy / "conversations" / "gggggggg-7.db").write_bytes(b"not a database")  # no tier has data


def _snapshot(db):
    return {
        session.id: (
            tuple(getattr(session.usage, key) for key in _SUM_KEYS), len(session.events),
            session.metadata.get("token_origin"), session.metadata["estimated"],
        )
        for session in read_usage_sessions("antigravity", db_path=db)
    }


def test_backfill_is_conversations_first_idempotent_and_reports_tiers(agy, tmp_path, monkeypatch, capsys):
    _populate_backfill(agy)
    db = tmp_path / "backfill-tiers.db"
    seen = []
    original = writer.capture_session
    monkeypatch.setattr(writer, "capture_session", lambda base, sid, hint=None: (
        seen.append(sid) or original(base, sid, hint)
    ))

    sessions, events = writer.backfill_agy_usage(db_path=db, agy_dir=agy)
    conversations = ["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3", "gggggggg-7"]
    assert seen[:4] == conversations and sorted(seen[4:]) == ["dddddddd-4", "eeeeeeee-5"]
    assert (sessions, events) == (5, 3 + 2 + 1 + 2 + 1)
    assert "tier1=2 tier2=1 tier3=2 skipped=2" in capsys.readouterr().err

    first = _snapshot(db)
    assert set(first) == {"aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3", "dddddddd-4", "eeeeeeee-5"}
    assert first["aaaaaaaa-1"] == ((1020, 903, 57, 21, 1077), 3, "agy-conversation-db", False)
    assert first["bbbbbbbb-2"][2:] == ("agy-conversation-db", False)
    assert first["cccccccc-3"][2:] == (None, True) and first["eeeeeeee-5"][2:] == (None, True)
    assert first["dddddddd-4"] == ((16, 5, 6, 0, 22), 2, "agy-transcript-json", False)

    assert writer.backfill_agy_usage(db_path=db, agy_dir=agy) == (5, 9)
    assert "tier1=2 tier2=1 tier3=2 skipped=2" in capsys.readouterr().err
    assert _snapshot(db) == first


def test_backfill_counts_a_rejected_write_as_skipped_not_captured(agy, tmp_path, capsys):
    db = tmp_path / "backfill-rejected.db"
    write_conversation(agy, "aaaaaaaa-1", MIXED_CALLS)
    write_conversation(agy, "cccccccc-3", MIXED_CALLS[:1])
    assert writer.backfill_agy_usage(db_path=db, agy_dir=agy) == (2, 4)
    assert "tier1=2 tier2=0 tier3=0 skipped=0" in capsys.readouterr().err

    # Smaller real snapshot (agy pruned calls): the store keeps the earlier one.
    write_conversation(agy, "aaaaaaaa-1", MIXED_CALLS[:2])
    # Lower-quality snapshot: only an estimate remains for a session already stored as real.
    (agy / "conversations" / "cccccccc-3.db").unlink()
    _write_transcript(agy, "cccccccc-3", _plain_steps())
    # A new session is still captured.
    write_conversation(agy, "bbbbbbbb-2", MIXED_CALLS[:1])
    assert writer.backfill_agy_usage(db_path=db, agy_dir=agy) == (1, 1)
    assert "tier1=1 tier2=0 tier3=0 skipped=2" in capsys.readouterr().err

    stored = {session.id: session for session in read_usage_sessions("antigravity", db_path=db)}
    assert stored["aaaaaaaa-1"].call_count == 3 and stored["aaaaaaaa-1"].usage.total_tokens == 1077
    assert stored["cccccccc-3"].metadata["estimated"] is False and stored["cccccccc-3"].call_count == 1
    assert stored["bbbbbbbb-2"].call_count == 1


@pytest.mark.parametrize("keyed, counts", [
    (False, "tier1=0 tier2=0 tier3=1 skipped=0"), (True, "tier1=0 tier2=1 tier3=0 skipped=0"),
])
def test_backfill_captures_a_transcript_only_at_a_nested_non_canonical_path(agy, tmp_path, capsys, keyed, counts):
    nested = agy / "brain" / "sess-nested-1" / "some" / "deeper" / "transcript.jsonl"
    nested.parent.mkdir(parents=True)
    steps = _keyed_steps([(100, 900, 50)]) if keyed else _plain_steps()
    nested.write_text("\n".join(json.dumps(step) for step in steps) + "\n")
    db = tmp_path / "nested.db"
    assert writer.backfill_agy_usage(db_path=db, agy_dir=agy) == (1, 1)
    assert counts in capsys.readouterr().err
    assert [session.id for session in read_usage_sessions("antigravity", db_path=db)] == ["sess-nested-1"]
    # Idempotent: a replay captures it again with the same totals.
    first = _snapshot(db)
    assert writer.backfill_agy_usage(db_path=db, agy_dir=agy) == (1, 1)
    assert _snapshot(db) == first


def test_transcript_hint_accepts_strings_and_paths_but_only_locates_transcripts(agy):
    nested = agy / "brain" / "sess" / "some" / "transcript.jsonl"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}\n")
    assert writer._resolve_transcript(agy, "sess", None) is None
    assert writer._resolve_transcript(agy, "sess", str(nested)) == nested
    assert writer._resolve_transcript(agy, "sess", nested) == nested
    for hint in (123, ["x"], {"p": 1}, "", "missing"):
        assert writer._resolve_transcript(agy, "sess", hint) is None
    # The hook payload's transcriptPath (a JSON string) keeps working.
    nested.write_text("\n".join(json.dumps(step) for step in _plain_steps()) + "\n")
    assert _hook(agy, agy.parent / "hint.db", "sess", transcriptPath=str(nested)) > 0


@pytest.mark.parametrize("stage", ["capture", "write"])
@pytest.mark.parametrize("failure", [RuntimeError("boom"), sqlite3.OperationalError("database is locked")])
def test_backfill_isolates_a_failing_session(agy, tmp_path, monkeypatch, capsys, stage, failure):
    for session_id in ("aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"):
        write_conversation(agy, session_id, MIXED_CALLS[:2])
    original_capture, original_write = writer.capture_session, writer._write_sessions

    def capture(base, session_id, hint=None):
        if stage == "capture" and session_id == "bbbbbbbb-2":
            raise failure
        return original_capture(base, session_id, hint)

    def write(sessions, db_path=None):
        if stage == "write" and sessions[0].id == "bbbbbbbb-2":
            raise failure
        return original_write(sessions, db_path)

    monkeypatch.setattr(writer, "capture_session", capture)
    monkeypatch.setattr(writer, "_write_sessions", write)
    db = tmp_path / "isolated.db"
    assert writer.backfill_agy_usage(db_path=db, agy_dir=agy) == (2, 4)
    err = capsys.readouterr().err
    assert "tier1=2 tier2=0 tier3=0 skipped=1" in err
    assert "Antigravity backfill skipped bbbbbbbb-2" in err and str(failure) in err
    assert sorted(session.id for session in read_usage_sessions("antigravity", db_path=db)) == [
        "aaaaaaaa-1", "cccccccc-3",
    ]


def test_backfill_works_without_brain(agy, tmp_path):
    write_conversation(agy, REAL_ID, MIXED_CALLS)
    assert not (agy / "brain").exists()
    assert writer.backfill_agy_usage(db_path=tmp_path / "no-brain.db", agy_dir=agy) == (1, 3)
    assert _stored(tmp_path / "no-brain.db").usage.total_tokens == 1077


def test_backfill_works_without_conversations(agy, tmp_path):
    _write_transcript(agy, REAL_ID, _plain_steps())
    assert writer.backfill_agy_usage(db_path=tmp_path / "no-conversations.db", agy_dir=agy) == (1, 1)


def test_backfill_requires_at_least_one_directory(agy, tmp_path):
    with pytest.raises(FileNotFoundError):
        writer.backfill_agy_usage(db_path=tmp_path / "none.db", agy_dir=tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        writer.backfill_agy_usage(db_path=tmp_path / "none.db", agy_dir=agy)
    assert not (tmp_path / "none.db").exists()


def test_backfill_cli_prints_tier_counts_and_never_stores_text(agy, tmp_path):
    _populate_backfill(agy)
    db = tmp_path / "cli-backfill.db"
    result = subprocess.run(
        [sys.executable, str(Path(writer.__file__)), "--backfill", "--agy-dir", str(agy), "--db", str(db)],
        input="", capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "tier1=2 tier2=1 tier3=2 skipped=2" in result.stderr and "Backfilled 5" in result.stderr
    assert len(read_usage_sessions("antigravity", db_path=db)) == 5


def test_hook_capture_of_a_conversation_reaches_the_dashboard_as_reported(agy, tmp_path, monkeypatch):
    import src.app as app_module
    from test_store_read_path import TestClient

    db = tmp_path / "real-e2e.db"
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(db))
    monkeypatch.setattr(app_module, "refresh_pricing", lambda: None)
    calls = [
        {"uncached": 100, "cache_read": 900, "output": 50, "reasoning": 20, "model": "gemini-3.8-flash"},
        {"uncached": 50, "cache_read": 450, "output": 30, "reasoning": 10, "model": "gemini-3.8-flash"},
        {"uncached": 25, "cache_read": 0, "output": 5, "model": "gemini-3.8-flash"},
    ]
    write_conversation(agy, REAL_ID, calls, start=START)
    # A transcript-based estimate must not matter once the database has real counts.
    _write_transcript(agy, REAL_ID, _plain_steps())
    command = [sys.executable, str(Path(writer.__file__)), "--agy-dir", str(agy)]
    payload = json.dumps({"conversationId": REAL_ID})
    for _ in range(2):
        result = subprocess.run(command, input=payload, capture_output=True, text=True, check=False, env=os.environ.copy())
        assert result.returncode == 0 and json.loads(result.stdout) == {}, result.stderr
    with TestClient(app_module.app) as client:
        response = client.get("/api/usage?tool=agy")
    assert response.status_code == 200
    data = response.json()
    uncached, cache_read, output = 175, 1350, 85
    summary = data["summary"]
    assert summary["session_count"] == 1 and summary["call_count"] == 3
    assert summary["cached_input"] == cache_read and summary["total_input"] == uncached + cache_read
    assert summary["output"] == output and summary["reasoning_output"] == 30
    assert summary["total_tokens"] == uncached + cache_read + output
    assert summary["cache_hit_rate"] == round(cache_read / (uncached + cache_read) * 100, 2)
    (session,) = data["sessions"]
    assert session["estimated"] is False and session["token_source"] == "reported"
    assert session["call_count"] == 3 and session["model"] == "gemini-3.8-flash"
