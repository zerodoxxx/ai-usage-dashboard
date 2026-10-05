"""Regression coverage for Codex and AGY readers of the shared usage DB."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.parsers.agy import parse_agy_usage
from src.parsers.aggregator import get_tool_usage
from src.parsers.codex import (
    CodexSource,
    extract_all_codex_sessions_for_capture,
    extract_codex_session_for_capture,
    parse_codex_usage,
)
from src.parsers.contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession
from src.parsers.source_registry import SourceRegistry
from src.usage_store import mark_provider_capture_enabled, write_usage_sessions


_TIME = "2026-09-30T10:00:00+00:00"
_MODEL = "gpt-5.6-luna"


def _session(
    session_id: str,
    provider: str,
    *,
    tokens: int = 100,
    model: str = _MODEL,
    cost: float = 999.0,
) -> UsageSession:
    usage = TokenUsage(
        input_tokens=tokens - 20,
        cached_input_tokens=20 if tokens > 20 else 0,
        output_tokens=20 if tokens > 20 else 0,
        total_tokens=tokens,
    )
    event = UsageEvent(
        timestamp=_TIME,
        model=model,
        usage=usage,
        cost=CostEstimate(cached_usd=cost, source="estimated"),
        metadata={"tps_duration_seconds": 5, "tps_output_tokens": usage.output_tokens},
    )
    return UsageSession(
        id=session_id,
        provider=provider,
        tool=provider,
        model=model,
        title=f"{provider} session",
        created_at=_TIME,
        start_time=_TIME,
        end_time=_TIME,
        usage=usage,
        events=[event],
        cost=CostEstimate(cached_usd=cost, source="estimated"),
        call_count=1,
    )


def _write_rollout(root: Path, thread_id: str, total_tokens: int) -> Path:
    path = root / "sessions" / f"rollout-2026-09-30-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = {
        "input_tokens": total_tokens - 20,
        "cached_input_tokens": 0,
        "output_tokens": 20,
        "reasoning_output_tokens": 0,
        "cache_write_input_tokens": 0,
        "total_tokens": total_tokens,
    }
    records = [
        {
            "timestamp": _TIME,
            "type": "session_meta",
            "payload": {"model": _MODEL},
        },
        {
            "timestamp": _TIME,
            "type": "token_usage_record",
            "payload": {"response_id": f"response-{thread_id}", "usage": usage},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def _write_threads_db(root: Path, rows: list[tuple[str, str, int]]) -> None:
    db_path = root / "state_5.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """CREATE TABLE threads (
                id TEXT, title TEXT, model TEXT, reasoning_effort TEXT,
                tokens_used INTEGER, created_at TEXT, rollout_path TEXT
            )"""
        )
        connection.executemany(
            """INSERT INTO threads
                (id, title, model, reasoning_effort, tokens_used, created_at, rollout_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (thread_id, f"Thread {thread_id}", _MODEL, "high", tokens, _TIME, rollout_path)
                for thread_id, rollout_path, tokens in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_agy_legacy_database_without_provider_column_keeps_old_totals(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "token_usage.db")
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT, title TEXT, model TEXT, input_tokens INTEGER,
                cached_input_tokens INTEGER, output_tokens INTEGER,
                reasoning_output_tokens INTEGER, total_tokens INTEGER,
                cache_write_tokens INTEGER, cost_usd REAL, call_count INTEGER,
                timestamp TEXT, updated_at TEXT
            );
            CREATE TABLE token_events (
                session_id TEXT, step_index INTEGER, timestamp TEXT, model TEXT,
                input_tokens INTEGER, cached_input_tokens INTEGER,
                output_tokens INTEGER, cache_write_tokens INTEGER,
                reasoning_output_tokens INTEGER, total_tokens INTEGER, cost_usd REAL
            );
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "Legacy AGY", "Gemini 3.8 Flash (High)", 100, 80, 10, 0, 110,
             20, 0.25, 1, _TIME, _TIME),
        )
        connection.execute(
            "INSERT INTO token_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy", 0, _TIME, "Gemini 3.8 Flash (High)", 100, 80, 10, 20, 0, 110, 0.25),
        )
        connection.commit()
    finally:
        connection.close()

    result = parse_agy_usage(tmp_path, db_path=tmp_path / "token_usage.db")

    assert result["summary"]["total_tokens"] == 110
    assert result["summary"]["total_input"] == 100
    assert result["summary"]["cache_write"] == 0
    assert result["sessions"][0]["id"] == "legacy"


def test_shared_database_keeps_agy_codex_and_claude_provider_rows_separate(tmp_path: Path) -> None:
    agy_root = tmp_path / "agy"
    agy_root.mkdir()
    db_path = agy_root / "token_usage.db"
    rows = (
        _session("same-native-id", "antigravity", tokens=110),
        _session("same-native-id", "codex", tokens=120),
        _session("same-native-id", "claude-code", tokens=130),
    )
    for row in rows:
        write_usage_sessions(row.provider or row.tool, [row], db_path=db_path)

    agy = parse_agy_usage(agy_root, db_path=db_path)
    codex = CodexSource(usage_db_path=db_path).extract_sessions(agy_root / "separate-codex-root")

    assert [(row["tool"], row["total_tokens"]) for row in agy["sessions"]] == [
        ("antigravity", 110),
    ]
    assert [(row.provider, row.id, row.usage.total_tokens) for row in codex] == [
        ("codex", "same-native-id", 120),
    ]


def test_codex_database_first_deduplicates_captured_thread_and_keeps_uncaptured_fallback(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "shared.db"
    root = tmp_path / "codex"
    captured_id = "11111111-1111-4111-8111-111111111111"
    uncaptured_id = "22222222-2222-4222-8222-222222222222"
    indexed_id = "88888888-8888-4888-8888-888888888888"
    captured_rollout = _write_rollout(root, captured_id, 900)
    uncaptured_rollout = _write_rollout(root, uncaptured_id, 50)
    indexed_rollout = _write_rollout(root, indexed_id, 60)
    _write_threads_db(root, [
        (captured_id, str(captured_rollout), 999),
        (indexed_id, str(indexed_rollout), 60),
    ])
    write_usage_sessions("codex", [_session(captured_id, "codex", tokens=100)], db_path=db_path)

    result = parse_codex_usage(root, usage_db_path=db_path)

    by_id = {row["id"]: row for row in result["sessions"]}
    assert set(by_id) == {captured_id, uncaptured_id, indexed_id}
    assert by_id[captured_id]["total_tokens"] == 100
    assert by_id[uncaptured_id]["total_tokens"] == 50
    assert by_id[indexed_id]["total_tokens"] == 60
    assert result["summary"]["total_tokens"] == 210
    assert result["summary"]["call_count"] == 3


def test_codex_history_survives_deleted_source_and_active_capture_is_db_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "shared.db"
    root = tmp_path / "deleted-codex-root"
    write_usage_sessions("codex", [_session("retained", "codex", tokens=100)], db_path=db_path)
    mark_provider_capture_enabled("codex", db_path=db_path)

    import src.parsers.codex as codex_module

    def unexpected_parse(_path: Path) -> dict:
        raise AssertionError("DB-only mode must not parse local rollout files")

    monkeypatch.setattr(codex_module, "_parse_rollout_file", unexpected_parse)
    result = parse_codex_usage(root, usage_db_path=db_path)

    assert result["summary"]["total_tokens"] == 100
    assert [row["id"] for row in result["sessions"]] == ["retained"]


def test_active_codex_capture_ignores_uncaptured_rollouts(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "shared.db"
    root = tmp_path / "codex"
    root.mkdir()
    _write_rollout(root, "77777777-7777-4777-8777-777777777777", 900)
    write_usage_sessions("codex", [_session("stored-only", "codex", tokens=100)], db_path=db_path)
    mark_provider_capture_enabled("codex", db_path=db_path)

    import src.parsers.codex as codex_module

    def unexpected_parse(_path: Path) -> dict:
        raise AssertionError("active capture mode must not scan uncaptured rollout files")

    monkeypatch.setattr(codex_module, "_parse_rollout_file", unexpected_parse)
    result = parse_codex_usage(root, usage_db_path=db_path)

    assert [row["id"] for row in result["sessions"]] == ["stored-only"]
    assert result["summary"]["total_tokens"] == 100


def test_codex_database_sessions_are_repriced_by_live_aggregator(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    root = tmp_path / "codex"
    root.mkdir()
    write_usage_sessions(
        "codex",
        [_session("priced", "codex", tokens=100, cost=999.0)],
        db_path=db_path,
    )
    registry = SourceRegistry([CodexSource(usage_db_path=db_path)])

    result = get_tool_usage(
        "codex",
        registry=registry,
        source_dirs={"codex": root},
        time_range="all",
    )

    assert result["summary"]["total_tokens"] == 100
    assert 0 < result["summary"]["cost_cached_usd"] < 999


def test_single_transcript_capture_uses_only_that_file_and_observed_child_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "codex"
    session_id = "33333333-3333-4333-8333-333333333333"
    transcript = _write_rollout(root, session_id, 80)
    transcript.write_text(
        "\n".join((
            json.dumps({"timestamp": _TIME, "type": "turn_context", "payload": {"model": "child-model"}}),
            json.dumps({
                "timestamp": _TIME,
                "type": "token_usage_record",
                "payload": {
                    "response_id": "child-response",
                    "usage": {
                        "input_tokens": 60,
                        "cached_input_tokens": 0,
                        "output_tokens": 20,
                        "total_tokens": 80,
                    },
                },
            }),
        )) + "\n",
        encoding="utf-8",
    )
    _write_threads_db(root, [(session_id, str(transcript), 999)])
    unrelated = _write_rollout(root, "44444444-4444-4444-8444-444444444444", 500)

    import src.parsers.codex as codex_module

    observed_paths: list[Path] = []
    original_parse = codex_module._parse_rollout_file_uncached

    def tracking_parse(path: Path, *, reject_malformed_tail: bool = False) -> tuple[dict, bool]:
        observed_paths.append(path.resolve())
        return original_parse(path, reject_malformed_tail=reject_malformed_tail)

    monkeypatch.setattr(codex_module, "_parse_rollout_file_uncached", tracking_parse)
    captured = extract_codex_session_for_capture(
        transcript,
        session_id,
        model="parent-model",
        codex_dir=root,
    )

    assert captured is not None
    assert captured.id == session_id
    assert captured.model == "child-model"
    assert captured.usage.total_tokens == 80
    assert observed_paths == [transcript.resolve()]
    assert unrelated.exists()
    assert captured.metadata["capture_source_hash"]
    assert captured.metadata["capture_size"] == transcript.stat().st_size
    assert "rollout_path" not in captured.metadata


def _codex_counters(input_tokens: int, cached: int, output: int, reasoning: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "cache_write_input_tokens": 0,
        "total_tokens": input_tokens + output,
    }


def _codex_token_record(response_id: str, usage: dict, **scope) -> dict:
    return {
        "timestamp": _TIME,
        "type": "token_usage_record",
        "payload": {"response_id": response_id, "usage": usage, **scope},
    }


def _codex_status_record(last: dict, total: dict) -> dict:
    return {
        "timestamp": _TIME,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": last, "total_token_usage": total},
        },
    }


def _capture_codex_records(tmp_path: Path, records: list[dict]) -> UsageSession:
    root = tmp_path / "codex"
    transcript = _write_rollout(root, "minimised", 100)
    transcript.write_text(
        "\n".join(json.dumps(record) for record in [
            {"timestamp": _TIME, "type": "session_meta", "payload": {"model": _MODEL}},
            *records,
        ]) + "\n",
        encoding="utf-8",
    )
    captured = extract_codex_session_for_capture(transcript, "minimised", codex_dir=root)
    assert captured is not None
    for field in (
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "cache_write_tokens", "total_tokens",
    ):
        assert getattr(captured.usage, field) == sum(
            getattr(event.usage, field) for event in captured.events
        )
    return captured


@pytest.mark.parametrize("omitted", [
    # Actual omitted responses from 01a08499, 01a084f8, and 01a08537;
    # preceding history is reduced to one response, with payload text removed.
    _codex_counters(219376, 213120, 3550),
    _codex_counters(246378, 233216, 6660),
    _codex_counters(245892, 240384, 7623),
])
def test_codex_capture_legacy_status_cannot_erase_modern_response(tmp_path: Path, omitted: dict) -> None:
    first = _codex_counters(100, 20, 10, 2)
    final = _codex_counters(50, 10, 5, 1)
    cumulative = {field: first[field] + omitted[field] for field in first}
    final_cumulative = {field: cumulative[field] + final[field] for field in first}
    legacy_final = {field: first[field] + final[field] for field in first}
    # Codex re-emits an unchanged legacy cumulative counter with zeroed
    # component usage (and sometimes a nonzero last total) after the response.
    zeroed = {field: 0 for field in first}
    zeroed["total_tokens"] = 13918
    missing = _codex_token_record(
        "omitted", omitted, turn_id="turn", thread_token_usage=cumulative, turn_token_usage=cumulative,
    )
    captured = _capture_codex_records(tmp_path, [
        _codex_token_record("first", first, turn_id="turn", thread_token_usage=first, turn_token_usage=first),
        _codex_status_record(first, first),
        missing,
        _codex_status_record(zeroed, first),
        missing,  # Replayed identity must still count only once.
        _codex_token_record("final", final, turn_id="turn", thread_token_usage=final_cumulative),
        _codex_status_record(final, legacy_final),
    ])
    assert captured.usage.total_tokens == final_cumulative["total_tokens"]
    assert captured.usage.cached_input_tokens == final_cumulative["cached_input_tokens"]
    assert captured.usage.reasoning_output_tokens == final_cumulative["reasoning_output_tokens"]
    assert captured.call_count == 3
    assert [event.event_id for event in captured.events] == ["first", "omitted", "final"]
    assert [event.usage.total_tokens for event in captured.events] == [
        first["total_tokens"], omitted["total_tokens"], final["total_tokens"],
    ]


@pytest.mark.parametrize("scope", [
    "thread_first_only", "thread_last_only", "unscoped_turn", "explicit_turns", "context_turns", "both_scopes",
])
def test_codex_capture_distinct_responses_survive_sparse_and_turn_totals(tmp_path: Path, scope: str) -> None:
    # Minimise the real two-stream records to two identical usages with
    # distinct identities, retaining or removing scoped cumulative fields.
    usage = _codex_counters(100, 20, 10, 2)
    doubled = {field: value * 2 for field, value in usage.items()}
    first = _codex_token_record("one", usage)
    second = _codex_token_record("two", usage)
    records = [first, first, second, second]
    if scope == "thread_first_only":
        first["payload"]["thread_token_usage"] = usage
    elif scope == "thread_last_only":
        second["payload"]["thread_token_usage"] = doubled
    else:
        first["payload"]["turn_token_usage"] = usage
        second["payload"]["turn_token_usage"] = usage
        if scope in ("explicit_turns", "both_scopes"):
            first["payload"]["turn_id"] = "turn-one"
            second["payload"]["turn_id"] = "turn-two"
        if scope == "context_turns":
            records = [
                {"type": "turn_context", "payload": {"turn_id": "turn-one"}}, first, first,
                {"type": "turn_context", "payload": {"turn_id": "turn-two"}}, second, second,
            ]
        if scope == "both_scopes":
            first["payload"]["thread_token_usage"] = usage
            second["payload"]["thread_token_usage"] = doubled
    captured = _capture_codex_records(tmp_path, records)
    assert captured.usage.total_tokens == 220
    assert captured.usage.cached_input_tokens == 40
    assert captured.call_count == 2
    assert [event.event_id for event in captured.events] == ["one", "two"]
    assert [event.usage.total_tokens for event in captured.events] == [110, 110]


@pytest.mark.parametrize("scope", ["thread_token_usage", "turn_token_usage", "legacy"])
def test_codex_capture_cumulative_gaps_remain_available(tmp_path: Path, scope: str) -> None:
    usage = _codex_counters(100, 20, 10, 2)
    cumulative = {field: value * 2 for field, value in usage.items()}
    if scope == "legacy":
        records = [
            _codex_token_record("one", usage),
            _codex_status_record(usage, cumulative),
        ]
        expected = 220
    else:
        records = [
            _codex_token_record("one", usage, turn_id="turn", **{scope: cumulative}),
            _codex_status_record(usage, cumulative),
            _codex_token_record("two", usage, turn_id="turn"),
        ]
        expected = 330
    captured = _capture_codex_records(tmp_path, records)
    assert captured.usage.total_tokens == expected
    assert captured.usage.cached_input_tokens == expected // 110 * 20
    assert all(event.metadata["tps_trustworthy"] is False for event in captured.events)
    if scope != "legacy":
        assert captured.call_count == 2
        assert [event.event_id for event in captured.events] == ["one", "two"]


@pytest.mark.parametrize("history", [
    {"forked_from_id": "parent"},
    {"history_base": {"thread_id": "minimised", "end_ordinal_exclusive": 227}},
    {"history_base": {"thread_id": "previous-fragment", "end_ordinal_exclusive": 218}},
])
@pytest.mark.parametrize("legacy_inherits", [False, True])
def test_codex_capture_excludes_inherited_counters(
    tmp_path: Path, history: dict, legacy_inherits: bool,
) -> None:
    # Real fork 01a0aeab starts with 190208 local tokens but a 5420628
    # cumulative total. Real paginated continuations also carry prior totals.
    usage = _codex_counters(190000, 180000, 208)
    inherited = _codex_counters(5000000, 4000000, 230420)
    first_total = {field: usage[field] + inherited[field] for field in usage}
    local_total = {field: value * 2 for field, value in usage.items()}
    last_total = {field: local_total[field] + inherited[field] for field in usage}
    first = _codex_token_record(
        "one", usage, turn_id="turn", thread_token_usage=first_total, turn_token_usage=first_total,
    )
    captured = _capture_codex_records(tmp_path, [
        {"type": "session_meta", "payload": history},
        first,
        _codex_status_record(usage, first_total if legacy_inherits else usage),
        # A later metadata record must not erase the inherited-history marker.
        {"type": "session_meta", "payload": {"id": "minimised"}},
        first,
        _codex_token_record("two", usage, turn_id="turn", thread_token_usage=last_total),
        _codex_status_record(usage, last_total if legacy_inherits else local_total),
    ])
    assert captured.usage.total_tokens == local_total["total_tokens"]
    assert captured.usage.input_tokens == local_total["input_tokens"]
    assert captured.usage.cached_input_tokens == local_total["cached_input_tokens"]
    assert captured.call_count == 2
    assert [event.event_id for event in captured.events] == ["one", "two"]


def test_codex_capture_merges_paginated_fragments_without_repeating_history(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    thread_id = "33333333-3333-4333-8333-333333333333"
    first = _write_rollout(root, thread_id, 110)
    usage = _codex_counters(90, 0, 20)
    continuation = first.with_name(first.stem + "_44444444-4444-4444-8444-444444444444.jsonl")
    records = [{"type": "session_meta", "payload": {
        "model": _MODEL, "history_base": {"thread_id": thread_id, "end_ordinal_exclusive": 227},
    }}]
    for ordinal in (2, 3):
        record = _codex_token_record(
            f"response-{ordinal}", usage,
            thread_token_usage={field: value * ordinal for field, value in usage.items()},
        )
        record["timestamp"] = f"2026-09-30T10:0{ordinal}:00+00:00"
        records.append(record)
    continuation.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    _write_threads_db(root, [(thread_id, str(continuation), 330)])
    sessions = extract_all_codex_sessions_for_capture(root)
    assert len(sessions) == 1
    assert sessions[0].usage.total_tokens == 330
    assert len(sessions[0].events) == 3
    assert {event.event_id for event in sessions[0].events} == {
        f"response-{thread_id}", "response-2", "response-3",
    }


def test_codex_capture_rejects_missing_zero_and_never_stable_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "codex"
    missing = root / "missing.jsonl"
    assert extract_codex_session_for_capture(missing, "missing", codex_dir=root) is None

    zero = root / "zero.jsonl"
    zero.parent.mkdir(parents=True)
    zero.write_text("{}\n", encoding="utf-8")
    _write_threads_db(root, [("zero", str(zero), 999)])
    assert extract_codex_session_for_capture(zero, "zero", codex_dir=root) is None

    path = _write_rollout(root, "55555555-5555-4555-8555-555555555555", 100)
    import src.parsers.codex as codex_module

    fingerprints = iter([
        {"capture_source_hash": "a", "capture_mtime_ns": 1, "capture_ctime_ns": 1, "capture_size": 1},
        {"capture_source_hash": "a", "capture_mtime_ns": 2, "capture_ctime_ns": 2, "capture_size": 2},
        {"capture_source_hash": "a", "capture_mtime_ns": 3, "capture_ctime_ns": 3, "capture_size": 3},
        {"capture_source_hash": "a", "capture_mtime_ns": 4, "capture_ctime_ns": 4, "capture_size": 4},
        {"capture_source_hash": "a", "capture_mtime_ns": 5, "capture_ctime_ns": 5, "capture_size": 5},
        {"capture_source_hash": "a", "capture_mtime_ns": 6, "capture_ctime_ns": 6, "capture_size": 6},
    ])
    parse_count = 0

    def moving_file(_path: Path) -> dict:
        return next(fingerprints)

    def parse_success(
        _path: Path,
        *,
        reject_malformed_tail: bool = False,
    ) -> tuple[dict, bool]:
        nonlocal parse_count
        parse_count += 1
        return ({
            "call_count": 1,
            "input_tokens": 80,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 80,
            "output_tokens": 20,
            "reasoning_output_tokens": 0,
            "cache_write_input_tokens": 0,
            "total_tokens": 100,
            "start_time": _TIME,
            "end_time": _TIME,
            "model": _MODEL,
            "usage_events": [],
        }, True)

    monkeypatch.setattr(codex_module, "_capture_file_metadata", moving_file)
    monkeypatch.setattr(codex_module, "_parse_rollout_file_uncached", parse_success)
    monkeypatch.setattr(codex_module.time, "sleep", lambda _seconds: None)

    assert extract_codex_session_for_capture(path, "unstable", codex_dir=root) is None
    assert parse_count == 3


def test_capture_rejects_truncated_trailing_json_without_tightening_dashboard_parse(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    session_id = "99999999-9999-4999-8999-999999999999"
    transcript = _write_rollout(root, session_id, 100)
    with transcript.open("a", encoding="utf-8") as file:
        file.write('{"timestamp":"2026-09-30T10:00:01Z","type":"token_usage_record"')

    from src.parsers.codex import _parse_rollout_file_uncached

    normal_result, normal_read_succeeded = _parse_rollout_file_uncached(transcript)
    captured = extract_codex_session_for_capture(transcript, session_id, codex_dir=root)

    assert normal_read_succeeded is True
    assert normal_result["total_tokens"] == 100
    assert captured is None


def test_capture_rejects_malformed_interior_record_even_if_later_lines_are_valid(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    session_id = "12121212-1212-4212-8212-121212121212"
    transcript = _write_rollout(root, session_id, 100)
    with transcript.open("a", encoding="utf-8") as file:
        file.write('{"timestamp":"bad","type":"token_usage_record"\n')
        file.write(json.dumps({"timestamp": _TIME, "type": "turn_context", "payload": {"model": _MODEL}}))
        file.write("\n")

    from src.parsers.codex import _parse_rollout_file_uncached

    _result, normal_read_succeeded = _parse_rollout_file_uncached(transcript)
    captured = extract_codex_session_for_capture(transcript, session_id, codex_dir=root)

    assert normal_read_succeeded is True
    assert captured is None


def test_capture_rejects_invalid_utf8_while_legacy_parser_remains_tolerant(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    session_id = "13131313-1313-4313-8313-131313131313"
    transcript = _write_rollout(root, session_id, 100)
    with transcript.open("ab") as file:
        file.write(b"\xff\n")

    from src.parsers.codex import _parse_rollout_file_uncached

    _result, normal_read_succeeded = _parse_rollout_file_uncached(transcript)

    assert normal_read_succeeded is True
    assert extract_codex_session_for_capture(transcript, session_id, codex_dir=root) is None


def test_capture_read_error_cannot_fall_back_to_coarse_state_token_total(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "codex"
    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    transcript = _write_rollout(root, session_id, 100)
    _write_threads_db(root, [(session_id, str(transcript), 999)])

    import src.parsers.codex as codex_module

    parsed = {
        "call_count": 1,
        "input_tokens": 80,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 80,
        "output_tokens": 20,
        "reasoning_output_tokens": 0,
        "cache_write_input_tokens": 0,
        "total_tokens": 100,
        "start_time": _TIME,
        "end_time": _TIME,
        "model": _MODEL,
        "usage_events": [],
    }
    monkeypatch.setattr(
        codex_module,
        "_parse_rollout_file_uncached",
        lambda *_args, **_kwargs: (parsed, False),
    )
    monkeypatch.setattr(
        codex_module,
        "_read_codex_thread_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed transcript reads must not fall back to state_5 totals")
        ),
    )

    assert extract_codex_session_for_capture(transcript, session_id, codex_dir=root) is None


def test_one_time_codex_capture_bypasses_shared_database(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "codex"
    session_id = "66666666-6666-4666-8666-666666666666"
    _write_rollout(root, session_id, 100)
    import src.usage_store as usage_store

    def should_not_read(*_args, **_kwargs):
        raise AssertionError("one-time capture must bypass the shared store")

    monkeypatch.setattr(usage_store, "read_usage_sessions", should_not_read)

    sessions = extract_all_codex_sessions_for_capture(root)

    assert len(sessions) == 1
    assert sessions[0].id == session_id
    assert sessions[0].usage.total_tokens == 100
    assert sessions[0].metadata["capture_source_hash"]


def test_one_time_backfill_fails_on_unreadable_transcript_without_activation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    session_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    transcript = _write_rollout(root, session_id, 100)
    with transcript.open("a", encoding="utf-8") as file:
        file.write('{"truncated":')
    db_path = tmp_path / "shared.db"

    with pytest.raises(RuntimeError, match="unreadable"):
        extract_all_codex_sessions_for_capture(root)

    from src.usage_store import is_provider_capture_enabled

    assert is_provider_capture_enabled("codex", db_path=db_path) is False


def test_backfill_keeps_state_only_totals_as_explicit_coarse_summary(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    session_id = "abababab-abab-4bab-8bab-abababababab"
    _write_threads_db(root, [(session_id, str(root / "missing.jsonl"), 321)])

    sessions = extract_all_codex_sessions_for_capture(root)

    assert len(sessions) == 1
    assert sessions[0].id == session_id
    assert sessions[0].usage.total_tokens == 321
    assert sessions[0].metadata["capture_quality"] == "state-summary"
    assert "capture_source_hash" not in sessions[0].metadata


def test_backfill_merges_disjoint_thread_rollouts_and_preserves_event_models(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    session_id = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
    earlier = _write_rollout(root / "earlier-fragment", session_id, 100)
    earlier.write_text(
        earlier.read_text(encoding="utf-8")
        .replace(_TIME, "2026-09-30T09:00:00+00:00")
        .replace(_MODEL, "gpt-older")
        .replace(f"response-{session_id}", "response-older"),
        encoding="utf-8",
    )
    canonical = _write_rollout(root / "canonical-fragment", session_id, 900)
    canonical.write_text(
        canonical.read_text(encoding="utf-8")
        .replace(_MODEL, "gpt-newer")
        .replace(f"response-{session_id}", "response-newer"),
        encoding="utf-8",
    )
    _write_threads_db(root, [(session_id, str(canonical), 999)])

    sessions = extract_all_codex_sessions_for_capture(root)

    assert len(sessions) == 1
    assert sessions[0].id == session_id
    assert sessions[0].usage.total_tokens == 1000
    assert sessions[0].call_count == 2
    assert {event.model for event in sessions[0].events} == {"gpt-older", "gpt-newer"}
    assert {event.metadata["capture_source_hash"] for event in sessions[0].events} == set(
        sessions[0].metadata["capture_sources"]
    )
    assert sessions[0].metadata["capture_quality"] == "merged-fragments"


def test_backfill_refuses_overlapping_thread_rollouts(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    session_id = "dededede-dede-4ede-8ede-dededededede"
    _write_rollout(root / "first", session_id, 100)
    overlapping = _write_rollout(root / "second", session_id, 900)
    overlapping.write_text(
        overlapping.read_text(encoding="utf-8").replace(
            f"response-{session_id}", "another-response"
        ),
        encoding="utf-8",
    )
    _write_threads_db(root, [(session_id, str(overlapping), 999)])

    with pytest.raises(RuntimeError, match="overlap in time"):
        extract_all_codex_sessions_for_capture(root)


def test_custom_codex_root_never_uses_implicit_global_shared_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.usage_store as usage_store

    def should_not_read(*_args, **_kwargs):
        raise AssertionError("custom roots require an explicit database path")

    monkeypatch.setattr(usage_store, "read_usage_sessions", should_not_read)
    monkeypatch.setattr(usage_store, "is_provider_capture_enabled", should_not_read)

    result = CodexSource().extract_sessions(tmp_path / "custom-codex")

    assert result == []
