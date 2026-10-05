"""Synthetic coverage for the shared provider-aware SQLite usage store."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from src.parsers.contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession
from src.usage_store import (
    DB_PATH_ENV_VAR,
    ensure_schema,
    is_provider_capture_enabled,
    mark_provider_capture_enabled,
    read_usage_sessions,
    resolve_db_path,
    write_usage_sessions,
)


def _legacy_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                model TEXT NOT NULL,
                workspace TEXT,
                title TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                call_count INTEGER NOT NULL DEFAULT 0,
                step_count INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                last_step_index INTEGER NOT NULL DEFAULT -1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE token_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                UNIQUE(session_id, step_index)
            );
            CREATE VIEW daily_summary AS
                SELECT date, COUNT(*) AS sessions FROM sessions GROUP BY date;
            CREATE VIEW model_summary AS
                SELECT model, COUNT(*) AS sessions FROM sessions GROUP BY model;
            INSERT INTO sessions VALUES (
                'agy-old', '2026-09-20T10:00:00+00:00', '2026-09-20',
                'Gemini Flash', 'workspace-a', 'old session',
                100, 40, 20, 15, 2, 120, 1, 5, 0.0123, 0,
                '2026-09-20T10:02:00+00:00'
            );
            INSERT INTO token_events VALUES (
                1, 'agy-old', 0, '2026-09-20T10:00:00+00:00', 'Gemini Flash',
                100, 40, 20, 15, 2, 120, 0.0123
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def _session(
    provider: str = "codex",
    session_id: str = "same-native-id",
    *,
    input_tokens: int = 100,
    output_tokens: int = 25,
    event_count: int = 1,
    mtime: int | None = None,
    source_hash: str = "sha256:rollout-a",
    cost_source: str = "estimated",
) -> UsageSession:
    events = []
    for index in range(event_count):
        event_input = input_tokens // event_count + (1 if index < input_tokens % event_count else 0)
        event_cached = 20 // event_count + (1 if index < 20 % event_count else 0)
        event_output = output_tokens // event_count + (1 if index < output_tokens % event_count else 0)
        event_reasoning = 4 // event_count + (1 if index < 4 % event_count else 0)
        event_write = 7 // event_count + (1 if index < 7 % event_count else 0)
        event_write_5m = 3 // event_count + (1 if index < 3 % event_count else 0)
        event_write_1h = 4 // event_count + (1 if index < 4 % event_count else 0)
        events.append(UsageEvent(
            timestamp=datetime(2026, 9, 21, 10, index, tzinfo=timezone.utc),
            model="gpt-5-codex",
            usage=TokenUsage(
                input_tokens=event_input,
                cached_input_tokens=event_cached,
                output_tokens=event_output,
                reasoning_output_tokens=event_reasoning,
                total_tokens=event_input + event_output + event_write,
                cache_write_tokens=event_write,
                cache_write_5m_tokens=event_write_5m,
                cache_write_1h_tokens=event_write_1h,
            ),
            cost=CostEstimate(
                cached_usd=0.012,
                uncached_usd=0.02,
                savings_usd=0.004,
                reported_usd=0.009 if cost_source == "reported" else None,
                source=cost_source,
            ),
            event_id=f"response-{index}" if index == 0 else None,
            metadata={
                "tps_duration_seconds": 2.5,
                "tps_output_tokens": event_output,
                "tps_trustworthy": True,
                "prompt": "must not be stored",
            },
        ))
    usage = TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=20,
        output_tokens=output_tokens,
        reasoning_output_tokens=4,
        total_tokens=input_tokens + 7 + output_tokens,
        cache_write_tokens=7,
        cache_write_5m_tokens=3,
        cache_write_1h_tokens=4,
    )
    metadata = {
        "token_source": "reported",
        "estimated": False,
        "prompt": "must not be stored",
    }
    if mtime is not None:
        metadata.update({
            "capture_source_hash": source_hash,
            "capture_mtime_ns": mtime,
            "capture_ctime_ns": mtime + 1,
            "capture_size": mtime + 2,
        })
    return UsageSession(
        id=session_id,
        tool=provider,
        provider=provider,
        model="gpt-5-codex",
        title="usage test",
        created_at=datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc),
        start_time=datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc),
        activity_at=datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc),
        reasoning_effort="high",
        usage=usage,
        events=events,
        cost=CostEstimate(
            cached_usd=0.12,
            uncached_usd=0.2,
            savings_usd=0.04,
            reported_usd=0.09 if cost_source == "reported" else None,
            source=cost_source,
        ),
        metadata=metadata,
        call_count=max(1, event_count),
    )


def test_read_missing_database_is_empty_and_does_not_create_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    assert read_usage_sessions("codex", missing) == []
    assert not missing.exists()
    assert not is_provider_capture_enabled("codex", missing)


def test_env_override_and_providerless_legacy_reads_are_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "old.db"
    _legacy_db(path)
    monkeypatch.setenv(DB_PATH_ENV_VAR, str(path))

    assert resolve_db_path() == path
    (session,) = read_usage_sessions("antigravity")
    assert session.usage.total_tokens == 120
    assert session.events[0].usage.total_tokens == 120
    assert session.usage.cache_write_tokens == 0
    assert session.events[0].usage.cache_write_tokens == 0
    assert read_usage_sessions("codex") == []
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    assert "provider" not in columns


def test_migration_preserves_legacy_agy_rows_and_keeps_summary_views_agy_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "token_usage.db"
    _legacy_db(path)
    with sqlite3.connect(path) as connection:
        before_session = connection.execute(
            "SELECT session_id, timestamp, date, model, workspace, title, input_tokens, "
            "cached_input_tokens, output_tokens, cache_write_tokens, reasoning_output_tokens, "
            "total_tokens, call_count, step_count, cost_usd, last_step_index, updated_at "
            "FROM sessions"
        ).fetchone()
        before_event = connection.execute(
            "SELECT id, session_id, step_index, timestamp, model, input_tokens, "
            "cached_input_tokens, output_tokens, cache_write_tokens, reasoning_output_tokens, "
            "total_tokens, cost_usd FROM token_events"
        ).fetchone()

    ensure_schema(path)
    write_usage_sessions("codex", [_session(input_tokens=120, output_tokens=40)], path)

    with sqlite3.connect(path) as connection:
        after_session = connection.execute(
            "SELECT session_id, timestamp, date, model, workspace, title, input_tokens, "
            "cached_input_tokens, output_tokens, cache_write_tokens, reasoning_output_tokens, "
            "total_tokens, call_count, step_count, cost_usd, last_step_index, updated_at "
            "FROM sessions WHERE session_id = 'agy-old'"
        ).fetchone()
        after_event = connection.execute(
            "SELECT id, session_id, step_index, timestamp, model, input_tokens, "
            "cached_input_tokens, output_tokens, cache_write_tokens, reasoning_output_tokens, "
            "total_tokens, cost_usd FROM token_events WHERE session_id = 'agy-old'"
        ).fetchone()
        migrated = connection.execute(
            "SELECT provider, usage_semantics_version, cache_write_mode FROM sessions "
            "WHERE session_id = 'agy-old'"
        ).fetchone()
        daily = connection.execute("SELECT sessions, total_tokens FROM daily_summary").fetchall()
        provider_daily = connection.execute(
            "SELECT provider, sessions FROM provider_daily_summary ORDER BY provider"
        ).fetchall()

    assert after_session == before_session
    assert after_event == before_event
    assert migrated == ("antigravity", 1, "embedded_in_input")
    assert daily == [(1, 120)]
    assert provider_daily == [("antigravity", 1), ("codex", 1)]


def test_provider_namespaces_and_token_cost_timing_metadata_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    codex = _session("codex", "shared-id", input_tokens=120, output_tokens=40, cost_source="reported")
    codex.events[0].timestamp = None
    claude = _session("claude-code", "shared-id", input_tokens=200, output_tokens=80)
    agy = _session("antigravity", "shared-id", input_tokens=150, output_tokens=30)

    write_usage_sessions("codex", [codex], path)
    write_usage_sessions("claude-code", [claude], path)
    write_usage_sessions("antigravity", [agy], path)

    read_codex = read_usage_sessions("codex", path)
    read_claude = read_usage_sessions("claude-code", path)
    assert [session.id for session in read_codex] == ["shared-id"]
    assert [session.id for session in read_claude] == ["shared-id"]
    assert [session.id for session in read_usage_sessions("agy", path)] == ["shared-id"]
    assert read_usage_sessions("claude", path) == read_claude
    assert read_codex[0].usage.input_tokens == 120
    assert read_codex[0].usage.cached_input_tokens == 20
    assert read_codex[0].usage.output_tokens == 40
    assert read_codex[0].usage.reasoning_output_tokens == 4
    assert read_codex[0].usage.total_tokens == 167
    assert read_codex[0].usage.cache_write_tokens == 7
    assert read_codex[0].usage.cache_write_5m_tokens == 3
    assert read_codex[0].usage.cache_write_1h_tokens == 4
    assert read_codex[0].cost is not None
    assert read_codex[0].cost.source == "reported"
    assert read_codex[0].cost.reported_usd == Decimal("0.09")
    assert read_codex[0].cost.cached_usd == Decimal("0.12")
    assert read_codex[0].cost.uncached_usd == Decimal("0.2")
    assert read_codex[0].events[0].event_id == "response-0"
    assert read_codex[0].events[0].timestamp is None
    assert read_codex[0].events[0].cost is not None
    assert read_codex[0].events[0].cost.reported_usd == Decimal("0.009")
    assert read_codex[0].events[0].cost.cached_usd == Decimal("0.012")
    assert read_codex[0].events[0].metadata == {
        "tps_duration_seconds": 2.5,
        "tps_output_tokens": 40,
        "tps_trustworthy": True,
    }
    assert read_codex[0].reasoning_effort == "high"
    assert read_codex[0].start_time is not None
    assert read_codex[0].end_time is not None
    with sqlite3.connect(path) as connection:
        identifiers = connection.execute(
            "SELECT session_id, provider, usage_semantics_version, cache_write_mode "
            "FROM sessions ORDER BY provider"
        ).fetchall()
        event_identifiers = connection.execute(
            "SELECT session_id, provider FROM token_events ORDER BY provider"
        ).fetchall()
        raw_metadata = connection.execute(
            "SELECT metadata_json FROM sessions WHERE provider = 'codex'"
        ).fetchone()[0]
    assert identifiers == [
        ("shared-id", "antigravity", 2, "additive"),
        ("claude-code:shared-id", "claude-code", 2, "additive"),
        ("codex:shared-id", "codex", 2, "additive"),
    ]
    assert event_identifiers == [(row[0], row[1]) for row in identifiers]
    assert "must not be stored" not in raw_metadata


def test_existing_legacy_antigravity_session_upserts_in_place(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    _legacy_db(path)
    original = read_usage_sessions("antigravity", path)[0]
    updated = _session("antigravity", original.id, input_tokens=300, output_tokens=90)

    assert write_usage_sessions("agy", [updated], path) == 1
    assert write_usage_sessions("antigravity", [updated], path) == 1
    (stored,) = read_usage_sessions("antigravity", path)
    assert stored.id == original.id == "agy-old"
    assert stored.usage == updated.usage
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT session_id, total_tokens FROM sessions").fetchall() == [
            ("agy-old", updated.usage.total_tokens),
        ]
        assert connection.execute("SELECT session_id FROM token_events").fetchall() == [("agy-old",)]


@pytest.mark.parametrize("raw_exists", [False, True])
@pytest.mark.parametrize("incoming_id", ["agy-id", "antigravity:agy-id"])
def test_prefixed_antigravity_snapshot_folds_on_write(
    tmp_path: Path, raw_exists: bool, incoming_id: str,
) -> None:
    path = tmp_path / "usage.db"
    old = _session("antigravity", "agy-id", input_tokens=100, event_count=2)
    write_usage_sessions("antigravity", [old], path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET session_id = 'antigravity:agy-id'")
        connection.execute("UPDATE token_events SET session_id = 'antigravity:agy-id'")
    assert [session.id for session in read_usage_sessions("antigravity", path)] == ["agy-id"]
    if raw_exists:
        # Seed both forms as left by the previous namespacing scheme.
        with sqlite3.connect(path) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(sessions)")]
            selection = ", ".join("'agy-id'" if name == "session_id" else name for name in columns)
            connection.execute(f"INSERT INTO sessions SELECT {selection} FROM sessions WHERE session_id = 'antigravity:agy-id'")
            columns = [row[1] for row in connection.execute("PRAGMA table_info(token_events)") if row[1] != "id"]
            selection = ", ".join("'agy-id'" if name == "session_id" else name for name in columns)
            connection.execute(
                f"INSERT INTO token_events ({', '.join(columns)}) SELECT {selection} "
                "FROM token_events WHERE session_id = 'antigravity:agy-id'"
            )
    updated = _session("antigravity", incoming_id, input_tokens=400, output_tokens=90)
    assert write_usage_sessions("antigravity", [updated], path) == 1
    (stored,) = read_usage_sessions("antigravity", path)
    assert stored.id == "agy-id"
    assert stored.usage == updated.usage
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT session_id FROM sessions").fetchall() == [("agy-id",)]
        assert connection.execute("SELECT session_id FROM token_events").fetchall() == [("agy-id",)]


def test_prefixed_antigravity_ownership_and_stale_snapshot_preserve_raw_owner(tmp_path: Path) -> None:
    from src.usage_store import find_event_owners, write_owned_usage_sessions

    path = tmp_path / "usage.db"
    newest = _session("antigravity", "agy-id", input_tokens=300, mtime=200)
    write_usage_sessions("antigravity", [newest], path)
    mark_provider_capture_enabled("antigravity", path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET session_id = 'antigravity:agy-id'")
        connection.execute("UPDATE token_events SET session_id = 'antigravity:agy-id'")
    assert find_event_owners("antigravity", ["response-0"], path) == {"response-0": "agy-id"}
    stale = _session("antigravity", "agy-id", input_tokens=100, mtime=100)
    assert write_owned_usage_sessions("antigravity", [stale], path) == []
    (stored,) = read_usage_sessions("antigravity", path)
    assert stored.id == "agy-id"
    assert stored.usage == newest.usage
    assert stored.events[0].usage == newest.events[0].usage
    newer = _session("antigravity", "agy-id", input_tokens=400, mtime=300)
    assert len(write_owned_usage_sessions("antigravity", [newer], path)) == 1
    assert read_usage_sessions("antigravity", path)[0].usage == newer.usage
    assert is_provider_capture_enabled("antigravity", path)
    assert find_event_owners("antigravity", ["response-0"], path) == {"response-0": "agy-id"}
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT session_id FROM sessions").fetchall() == [("agy-id",)]
        assert connection.execute("SELECT session_id FROM token_events").fetchall() == [("agy-id",)]
        assert connection.execute("SELECT provider FROM usage_capture_state").fetchall() == [("antigravity",)]


@pytest.mark.parametrize("raw_exists", [False, True])
def test_antigravity_id_fold_rolls_back_with_failed_write(tmp_path: Path, raw_exists: bool) -> None:
    path = tmp_path / "usage.db"
    original = _session("antigravity", "agy-id")
    write_usage_sessions("antigravity", [original], path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET session_id = 'antigravity:agy-id'")
        connection.execute("UPDATE token_events SET session_id = 'antigravity:agy-id'")
        if raw_exists:
            connection.execute(
                "INSERT INTO sessions(session_id,timestamp,date,model,updated_at) VALUES(?,?,?,?,?)",
                ("agy-id", "2026-09-21", "2026-09-21", "gemini", "2026-09-21"),
            )
        connection.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON token_events "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        before_sessions = connection.execute("SELECT * FROM sessions ORDER BY session_id").fetchall()
        before_events = connection.execute("SELECT * FROM token_events ORDER BY id").fetchall()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        write_usage_sessions("antigravity", [_session("antigravity", "agy-id", input_tokens=400)], path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM sessions ORDER BY session_id").fetchall() == before_sessions
        assert connection.execute("SELECT * FROM token_events ORDER BY id").fetchall() == before_events


def test_snapshot_replay_is_idempotent_and_replacement_removes_stale_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "usage.db"
    full = _session(event_count=2)
    write_usage_sessions("codex", [full], path)
    write_usage_sessions("codex", [full], path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM token_events").fetchone()[0] == 2

    shorter = _session(input_tokens=60, output_tokens=10, event_count=1)
    write_usage_sessions("codex", [shorter], path)
    result = read_usage_sessions("codex", path)
    assert result[0].usage.total_tokens == 77
    assert len(result[0].events) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM token_events").fetchone()[0] == 1


def test_older_overlapping_hook_cannot_replace_newer_transcript_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    newer = _session(input_tokens=200, output_tokens=50, event_count=2, mtime=200)
    older = _session(input_tokens=100, output_tokens=25, event_count=1, mtime=100)
    write_usage_sessions("codex", [newer], path)
    write_usage_sessions("codex", [older], path)

    result = read_usage_sessions("codex", path)
    assert result[0].usage.total_tokens == 257
    assert len(result[0].events) == 2

    # A newer correction may legitimately reduce totals and remove old events.
    correction = _session(input_tokens=50, output_tokens=10, event_count=1, mtime=300)
    write_usage_sessions("codex", [correction], path)
    corrected = read_usage_sessions("codex", path)
    assert corrected[0].usage.total_tokens == 67
    assert len(corrected[0].events) == 1

    # A fallback from state_5.sqlite has no transcript revision. It may not
    # erase the richer persisted rollout snapshot when the source was removed.
    unrevisioned_fallback = _session(input_tokens=1, output_tokens=1, event_count=1)
    unrevisioned_fallback.metadata.pop("capture_source_hash", None)
    unrevisioned_fallback.metadata.pop("capture_mtime_ns", None)
    unrevisioned_fallback.metadata.pop("capture_ctime_ns", None)
    unrevisioned_fallback.metadata.pop("capture_size", None)
    write_usage_sessions("codex", [unrevisioned_fallback], path)
    after_fallback = read_usage_sessions("codex", path)
    assert after_fallback[0].usage.total_tokens == 67
    assert len(after_fallback[0].events) == 1


def test_state_summary_is_kept_as_provenance_and_cannot_replace_transcript_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "usage.db"
    transcript = _session(input_tokens=200, output_tokens=50, event_count=2, mtime=200)
    write_usage_sessions("codex", [transcript], path)

    summary = _session(input_tokens=12, output_tokens=3, event_count=1)
    summary.metadata = {"capture_quality": "state-summary"}
    write_usage_sessions("codex", [summary], path)

    result = read_usage_sessions("codex", path)
    assert result[0].usage.total_tokens == 257
    assert len(result[0].events) == 2
    assert result[0].metadata["capture_source_hash"] == "sha256:rollout-a"

    # A new session can retain its coarse-source provenance while remaining
    # eligible for a later, richer transcript snapshot.
    summary_only = _session(session_id="summary-only")
    summary_only.metadata = {"capture_quality": "state-summary"}
    write_usage_sessions("codex", [summary_only], path)
    coarse = next(row for row in read_usage_sessions("codex", path) if row.id == "summary-only")
    assert coarse.metadata["capture_quality"] == "state-summary"


def test_fragment_updates_replace_only_the_matching_revisioned_source(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    first = _session(
        session_id="fragmented-thread",
        input_tokens=100,
        output_tokens=25,
        mtime=100,
        source_hash="sha256:fragment-a",
    )
    second = _session(
        session_id="fragmented-thread",
        input_tokens=200,
        output_tokens=40,
        mtime=200,
        source_hash="sha256:fragment-b",
    )
    second.events[0].timestamp = datetime(2026, 9, 21, 11, 0, tzinfo=timezone.utc)
    second.events[0].event_id = "fragment-b-event"

    for event in first.events:
        event.metadata["capture_source_hash"] = "sha256:fragment-a"
    for event in second.events:
        event.metadata["capture_source_hash"] = "sha256:fragment-b"
    merged = UsageSession(
        id="fragmented-thread",
        tool="codex",
        provider="codex",
        model="gpt-5-codex",
        title="fragmented usage",
        events=[*first.events, *second.events],
        metadata={
            "capture_quality": "merged-fragments",
            "capture_sources": {
                "sha256:fragment-a": {
                    "capture_mtime_ns": 100,
                    "capture_ctime_ns": 101,
                    "capture_size": 102,
                },
                "sha256:fragment-b": {
                    "capture_mtime_ns": 200,
                    "capture_ctime_ns": 201,
                    "capture_size": 202,
                },
            },
        },
    )
    write_usage_sessions("codex", [merged], path)
    initial = read_usage_sessions("codex", path)[0]
    assert initial.usage.total_tokens == 379
    assert len(initial.events) == 2

    newer_first = _session(
        session_id="fragmented-thread",
        input_tokens=50,
        output_tokens=10,
        mtime=300,
        source_hash="sha256:fragment-a",
    )
    newer_first.events[0].event_id = "fragment-a-new"
    newer_first.events[0].timestamp = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    assert write_usage_sessions("codex", [newer_first], path) == 1

    updated = read_usage_sessions("codex", path)[0]
    assert updated.usage.total_tokens == 314
    assert [event.event_id for event in updated.events] == [
        "fragment-a-new",
        "fragment-b-event",
    ]

    older_first = _session(
        session_id="fragmented-thread",
        input_tokens=10,
        output_tokens=1,
        mtime=250,
        source_hash="sha256:fragment-a",
    )
    assert write_usage_sessions("codex", [older_first], path) == 0
    after_stale = read_usage_sessions("codex", path)[0]
    assert after_stale.usage.total_tokens == 314
    assert len(after_stale.events) == 2

    correction = _session(
        session_id="fragmented-thread",
        input_tokens=40,
        output_tokens=5,
        mtime=400,
        source_hash="sha256:fragment-a",
    )
    correction.events[0].event_id = "fragment-a-corrected"
    assert write_usage_sessions("codex", [correction], path) == 1
    corrected = read_usage_sessions("codex", path)[0]
    assert corrected.usage.total_tokens == 299
    assert [event.event_id for event in corrected.events] == [
        "fragment-a-corrected",
        "fragment-b-event",
    ]


def test_stale_full_backfill_preserves_newer_hook_and_adds_missing_fragment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "usage.db"
    source_a = "sha256:fragment-a"
    source_b = "sha256:fragment-b"
    latest_a = _session(
        session_id="concurrent-thread",
        input_tokens=50,
        output_tokens=10,
        mtime=300,
        source_hash=source_a,
    )
    latest_a.events[0].event_id = "a-newer-hook"
    write_usage_sessions("codex", [latest_a], path)

    older_a = _session(
        session_id="concurrent-thread",
        input_tokens=100,
        output_tokens=25,
        mtime=200,
        source_hash=source_a,
    )
    older_a.events[0].event_id = "a-stale-backfill"
    fragment_b = _session(
        session_id="concurrent-thread",
        input_tokens=200,
        output_tokens=40,
        mtime=400,
        source_hash=source_b,
    )
    fragment_b.events[0].timestamp = datetime(2026, 9, 21, 11, 0, tzinfo=timezone.utc)
    fragment_b.events[0].event_id = "b-from-backfill"
    older_a.events[0].metadata["capture_source_hash"] = source_a
    fragment_b.events[0].metadata["capture_source_hash"] = source_b
    stale_backfill = UsageSession(
        id="concurrent-thread",
        tool="codex",
        provider="codex",
        model="gpt-5-codex",
        events=[older_a.events[0], fragment_b.events[0]],
        metadata={
            "capture_quality": "merged-fragments",
            "capture_sources": {
                source_a: {
                    "capture_mtime_ns": 200,
                    "capture_ctime_ns": 201,
                    "capture_size": 202,
                },
                source_b: {
                    "capture_mtime_ns": 400,
                    "capture_ctime_ns": 401,
                    "capture_size": 402,
                },
            },
        },
    )

    assert write_usage_sessions("codex", [stale_backfill], path) == 1
    result = read_usage_sessions("codex", path)[0]
    assert result.usage.total_tokens == 314
    assert [event.event_id for event in result.events] == [
        "a-newer-hook",
        "b-from-backfill",
    ]
    assert result.metadata["capture_sources"][source_a]["capture_mtime_ns"] == 300
    assert result.metadata["capture_sources"][source_b]["capture_mtime_ns"] == 400


def test_failed_snapshot_rolls_back_session_update_and_event_replacement(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    original = _session(input_tokens=100, output_tokens=20, event_count=1)
    write_usage_sessions("codex", [original], path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_second_event BEFORE INSERT ON token_events
               WHEN NEW.step_index = 1 BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError):
        write_usage_sessions(
            "codex",
            [_session(input_tokens=300, output_tokens=90, event_count=2)],
            path,
        )

    after = read_usage_sessions("codex", path)
    assert after[0].usage.total_tokens == 127
    assert len(after[0].events) == 1


def test_concurrent_codex_and_explicit_column_agy_writes_coexist(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    ensure_schema(path)
    start = Barrier(2)

    def codex_writer() -> None:
        start.wait()
        for index in range(12):
            write_usage_sessions(
                "codex",
                [_session(session_id=f"codex-{index}")],
                path,
            )

    def agy_writer() -> None:
        start.wait()
        for index in range(12):
            with sqlite3.connect(path, timeout=5) as connection:
                connection.execute(
                    """INSERT INTO sessions (
                        session_id, timestamp, date, model, input_tokens,
                        cached_input_tokens, output_tokens, cache_write_tokens,
                        reasoning_output_tokens, total_tokens, call_count,
                        step_count, cost_usd, last_step_index, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        total_tokens=excluded.total_tokens,
                        updated_at=excluded.updated_at""",
                    (
                        "agy-concurrent",
                        "2026-09-22T10:00:00+00:00",
                        "2026-09-22",
                        "Gemini Flash",
                        index,
                        0,
                        0,
                        0,
                        0,
                        index,
                        1,
                        1,
                        0.0,
                        0,
                        "2026-09-22T10:00:00+00:00",
                    ),
                )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(codex_writer)
        second = executor.submit(agy_writer)
        first.result(timeout=15)
        second.result(timeout=15)

    assert len(read_usage_sessions("codex", path)) == 12
    assert len(read_usage_sessions("antigravity", path)) == 1
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT provider, total_tokens FROM sessions WHERE session_id = 'agy-concurrent'"
        ).fetchone()
    assert row == ("antigravity", 11)


def test_capture_mode_activates_only_after_explicit_completion_mark(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    assert not is_provider_capture_enabled("codex", path)
    ensure_schema(path)
    assert not is_provider_capture_enabled("codex", path)

    mark_provider_capture_enabled("codex", path)

    assert is_provider_capture_enabled("codex", path)
    with sqlite3.connect(path) as connection:
        completed_at = connection.execute(
            "SELECT backfill_completed_at FROM usage_capture_state WHERE provider = 'codex'"
        ).fetchone()[0]
    assert completed_at


def test_default_path_is_tool_neutral_and_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.usage_store import DEFAULT_DB_RELATIVE_PATH, LEGACY_DB_RELATIVE_PATH

    assert DEFAULT_DB_RELATIVE_PATH == Path(".local/share/ai-usage/usage.db")
    assert LEGACY_DB_RELATIVE_PATH == Path(".gemini/antigravity-cli/token_usage.db")
    monkeypatch.delenv(DB_PATH_ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_db_path() == tmp_path / ".local/share/ai-usage/usage.db"
    monkeypatch.setenv(DB_PATH_ENV_VAR, str(tmp_path / "other.db"))
    assert resolve_db_path() == tmp_path / "other.db"


def test_writer_creates_missing_parent_but_reader_does_not(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "usage.db"
    assert read_usage_sessions("codex", db_path=path) == []
    assert not path.parent.exists()
    ensure_schema(path)
    assert path.is_file()


@pytest.mark.parametrize("provider", ["codex", "claude-code"])
@pytest.mark.parametrize("identity", ["event_id", "message_id", "request_id"])
def test_shared_fragment_response_keeps_union_and_provenance(tmp_path, provider, identity):
    from copy import deepcopy

    db = tmp_path / "usage.db"
    a = _session(provider, input_tokens=5, output_tokens=2, mtime=100, source_hash="source-a")
    b = deepcopy(a)
    b.metadata.update(capture_source_hash="source-b")
    for session in (a, b):
        if identity != "event_id":
            session.events[0].metadata[identity] = "shared-response"
            session.events[0].event_id = None
    write_usage_sessions(provider, [a, b], db)
    write_usage_sessions(provider, [a], db)
    (stored,) = read_usage_sessions(provider, db)
    assert stored.usage.total_tokens == 14
    assert stored.call_count == 1
    assert len(stored.events) == 1
    assert set(stored.events[0].metadata["capture_source_hashes"]) == {"source-a", "source-b"}
    assert set(stored.metadata["capture_sources"]) == {"source-a", "source-b"}
    # Removing the response from one source must preserve the other's copy.
    a.events = []
    a.usage = TokenUsage()
    a.metadata["capture_mtime_ns"] = 200
    write_usage_sessions(provider, [a], db)
    (stored,) = read_usage_sessions(provider, db)
    assert stored.usage.total_tokens == 14
    assert stored.events[0].metadata["capture_source_hashes"] == ["source-b"]
    b.events = []
    b.usage = TokenUsage()
    b.metadata["capture_mtime_ns"] = 200
    write_usage_sessions(provider, [b], db)
    assert read_usage_sessions(provider, db)[0].usage.total_tokens == 0


def test_store_health_distinguishes_missing_unreadable_and_empty(tmp_path):
    import src.usage_store as store

    db = tmp_path / "usage.db"
    assert store.read_store_health(db) == {
        "database_exists": False, "readable": False, "error": None, "path": str(db),
    }
    assert store.get_usage_store_status(db)["schema_version"] is None
    assert not db.exists()
    db.write_bytes(b"not sqlite")
    health = store.read_store_health(db)
    assert health["database_exists"] and not health["readable"] and health["error"]
    assert read_usage_sessions("codex", db) == []
    db.unlink()
    ensure_schema(db)
    assert store.read_store_health(db) == {
        "database_exists": True, "readable": True, "error": None, "path": str(db),
    }
    assert read_usage_sessions("codex", db) == []
    write_usage_sessions("codex", [_session()], db)
    status = store.get_usage_store_status(db)
    assert status["database_exists"] is True
    assert status["path"] == str(db)
    assert status["schema_version"] == store.SCHEMA_VERSION
    with sqlite3.connect(db) as connection:
        last_write = connection.execute("SELECT MAX(updated_at) FROM sessions").fetchone()[0]
    assert status["providers"]["codex"]["last_write_at"] == last_write
    assert status["providers"]["codex"]["sessions"] == 1
    assert status["providers"]["codex"]["events"] == 1


def test_read_projects_only_needed_columns_and_decodes_metadata_once(tmp_path, monkeypatch):
    import src.usage_store as store

    db = tmp_path / "usage.db"
    write_usage_sessions("codex", [_session()], db)
    with sqlite3.connect(db) as connection:
        connection.execute("ALTER TABLE sessions ADD COLUMN unused_blob BLOB")
        connection.execute("ALTER TABLE token_events ADD COLUMN unused_blob BLOB")
    queries = []
    original_connect = store._connect_read_only
    original_decode = store._decode_metadata
    decoded = []

    def connect(path):
        connection = original_connect(path)
        connection.set_trace_callback(queries.append)
        return connection

    def decode(value):
        decoded.append(value)
        return original_decode(value)

    monkeypatch.setattr(store, "_connect_read_only", connect)
    monkeypatch.setattr(store, "_decode_metadata", decode)
    assert len(read_usage_sessions("codex", db)) == 1
    assert len(decoded) == 2
    selects = [q for q in queries if q.startswith("SELECT")]
    assert all("*" not in q and "unused_blob" not in q for q in selects)


def test_owned_writer_resolves_and_writes_under_one_transaction(tmp_path, monkeypatch):
    import src.usage_store as store

    db = tmp_path / "usage.db"
    ensure_schema(db)
    connections = []
    original_owned = store._owned_snapshot
    original_upsert = store._upsert_session

    def owned(connection, provider, session):
        assert connection.in_transaction
        connections.append(connection)
        return original_owned(connection, provider, session)

    def upsert(connection, provider, session):
        assert connection.in_transaction
        assert connection is connections[-1]
        return original_upsert(connection, provider, session)

    monkeypatch.setattr(store, "_owned_snapshot", owned)
    monkeypatch.setattr(store, "_upsert_session", upsert)
    a = _session("claude-code", "owner-a")
    b = _session("claude-code", "owner-b", event_count=2)
    b.events[1].event_id = "unique-b"
    written = store.write_owned_usage_sessions("claude-code", [a, b], db)
    assert len(written) == 2
    sessions = read_usage_sessions("claude-code", db)
    assert sum(len(session.events) for session in sessions) == 2
    owner_b = next(session for session in sessions if session.id == "owner-b")
    assert [event.event_id for event in owner_b.events] == ["unique-b"]
    assert owner_b.usage.total_tokens == b.events[1].usage.total_tokens
    assert owner_b.cost.total_usd == b.events[1].cost.total_usd


def test_stale_shared_backfill_keeps_newer_response_revision(tmp_path):
    from copy import deepcopy

    db = tmp_path / "usage.db"
    latest = _session(mtime=300, input_tokens=50, source_hash="source-a")
    write_usage_sessions("codex", [latest], db)
    old_a = _session(mtime=100, input_tokens=10, source_hash="source-a")
    b = deepcopy(old_a)
    b.metadata["capture_source_hash"] = "source-b"
    for session in (old_a, b):
        session.events[0].metadata["capture_source_hash"] = session.metadata["capture_source_hash"]
    merged = UsageSession(
        id=latest.id, tool="codex", events=[*old_a.events, *b.events],
        metadata={"capture_sources": {
            "source-a": {"capture_mtime_ns": 100, "capture_ctime_ns": 101, "capture_size": 102},
            "source-b": {"capture_mtime_ns": 100, "capture_ctime_ns": 101, "capture_size": 102},
        }},
    )
    write_usage_sessions("codex", [merged], db)
    (stored,) = read_usage_sessions("codex", db)
    assert stored.usage.total_tokens == latest.usage.total_tokens
    assert set(stored.events[0].metadata["capture_source_hashes"]) == {"source-a", "source-b"}


def test_write_deadline_bounds_busy_wait_and_raises(tmp_path, monkeypatch):
    from src import usage_store as store
    import sqlite3
    import time

    db = tmp_path / "usage.db"
    ensure_schema(db)
    # Expired deadline fails before touching the database.
    with pytest.raises(store.UsageStoreDeadlineExceeded):
        write_usage_sessions("codex", [_session()], db, deadline=time.monotonic() - 1)
    # A lock held by another connection is waited on only until the deadline.
    blocker = sqlite3.connect(db)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(store.UsageStoreDeadlineExceeded):
            write_usage_sessions("codex", [_session()], db, deadline=started + 0.3)
        assert time.monotonic() - started < 1.5
    finally:
        blocker.rollback()
        blocker.close()
    # Without contention a deadline does not interfere.
    assert write_usage_sessions("codex", [_session()], db, deadline=time.monotonic() + 5) == 1
