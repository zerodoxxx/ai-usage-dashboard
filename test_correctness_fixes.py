"""Regression tests for dashboard calculation correctness fixes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.parsers.agy import parse_agy_usage
from src.parsers.claude import _usage_event
from src.parsers.codex import _normalize_usage
from src.parsers.contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession
from src.parsers.source_registry import SourceRegistry
from src.pricing import PricingCatalog, PricingRates, calculate_cost_strict


def _write_agy_transcript(root: Path, session_id: str, records: list[dict]) -> None:
    transcript = root / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _create_agy_metadata_db(root: Path) -> None:
    connection = sqlite3.connect(root / "conversation_summaries.db")
    try:
        connection.execute(
            """
            CREATE TABLE conversation_summaries (
                conversation_id TEXT,
                title TEXT,
                preview TEXT,
                step_count INTEGER,
                last_modified_time TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO conversation_summaries
                (conversation_id, title, preview, step_count, last_modified_time)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "metadata-only",
                "Metadata-only conversation",
                "",
                0,
                "0001-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _create_agy_zero_token_db(root: Path) -> None:
    connection = sqlite3.connect(root / "token_usage.db")
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
            INSERT INTO sessions VALUES
                ('zero-db', 'Zero database row', 'Gemini 3.8 Flash (High)', 0, 0, 0, 0, 0, 0, 0, 0,
                 '2026-09-18T10:00:00+00:00', '2026-09-18T10:00:00+00:00');
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_agy_zero_usage_database_row_does_not_fabricate_usage(tmp_path: Path) -> None:
    _create_agy_zero_token_db(tmp_path)

    result = parse_agy_usage(tmp_path)

    assert result["sessions"] == []
    assert result["summary"]["session_count"] == 0
    assert result["summary"]["call_count"] == 0
    assert result["summary"]["total_tokens"] == 0


def test_agy_database_cache_writes_are_not_additive_input(tmp_path: Path) -> None:
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
            INSERT INTO sessions VALUES
                ('write-contained', 'Write contained', 'Gemini 3.8 Flash (High)', 100, 80, 10, 0, 110, 20, 0.1, 1,
                 '2026-09-18T10:00:00+00:00', '2026-09-18T10:00:00+00:00');
            INSERT INTO token_events VALUES
                ('write-contained', 0, '2026-09-18T10:00:00+00:00', 'Gemini 3.8 Flash (High)',
                 100, 80, 10, 20, 0, 110, 0.1);
            """
        )
        connection.commit()
    finally:
        connection.close()

    result = parse_agy_usage(tmp_path)

    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["total_input"] == 100
    assert result["sessions"][0]["cache_write"] == 0
    assert result["sessions"][0]["total_tokens"] == 110
    assert result["summary"]["total_input"] == 100
    assert result["summary"]["total_tokens"] == 110
    assert result["summary"]["cache_write"] == 0


def test_agy_metadata_only_session_does_not_fabricate_usage(tmp_path: Path) -> None:
    _create_agy_metadata_db(tmp_path)

    result = parse_agy_usage(tmp_path)

    assert result["sessions"] == []
    assert result["summary"]["session_count"] == 0
    assert result["summary"]["call_count"] == 0
    assert result["summary"]["total_tokens"] == 0
    assert result["summary"]["cost_cached_usd"] == 0.0


def test_agy_user_only_transcript_is_not_reported_as_an_api_call(tmp_path: Path) -> None:
    _write_agy_transcript(
        tmp_path,
        "user-only",
        [
            {
                "created_at": "2026-09-18T10:00:00Z",
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "I opened a conversation but no model response was recorded.",
            }
        ],
    )

    result = parse_agy_usage(tmp_path)

    assert result["sessions"] == []
    assert result["summary"]["call_count"] == 0
    assert result["summary"]["total_tokens"] == 0


def test_claude_cache_writes_are_visible_and_priced_by_ttl() -> None:
    record = {
        "timestamp": "2026-09-18T10:00:00Z",
        "message": {"id": "cache-write-only"},
    }
    usage = {
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 1_000_000,
            "ephemeral_1h_input_tokens": 1_000_000,
        },
        "output_tokens": 0,
    }

    event = _usage_event(record, usage, "claude-sonnet-4-20250514", 1)

    assert event is not None
    assert event.usage.input_tokens == 0
    assert event.usage.total_input == 2_000_000
    assert event.usage.total_tokens == 2_000_000
    assert event.usage.cache_write_tokens == 2_000_000
    assert event.usage.cache_write_5m_tokens == 1_000_000
    assert event.usage.cache_write_1h_tokens == 1_000_000
    assert event.cost is not None
    # Sonnet 4: 5m writes cost 1.25x input and 1h writes cost 2x input.
    assert float(event.cost.cached_usd) == 9.75
    assert float(event.cost.uncached_usd) == 6.0
    assert float(event.cost.savings_usd) == -3.75


def test_aggregate_cache_write_remainder_is_priced_end_to_end() -> None:
    from src.parsers.aggregator import _refresh_estimated_session_cost

    usage = TokenUsage(
        cache_write_tokens=3_000_000,
        cache_write_5m_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
    )
    session = UsageSession(
        id="aggregate-cache-write",
        tool="claude-code",
        provider="claude",
        model="claude-sonnet-4-20250514",
        usage=usage,
        cost=CostEstimate(source="estimated"),
    )
    expected = calculate_cost_strict(
        "claude-sonnet-4-20250514",
        0,
        0,
        0,
        provider="claude",
        cache_write=3_000_000,
        cache_write_5m=1_000_000,
        cache_write_1h=1_000_000,
    )

    _refresh_estimated_session_cost(session)

    assert session.cost is not None
    assert float(session.cost.cached_usd) == expected["cost_cached_usd"]
    assert float(session.cost.uncached_usd) == expected["cost_uncached_usd"]


def test_serialized_totals_reconcile_components_when_provider_total_is_lower() -> None:
    from src.parsers.aggregator import get_tool_usage
    from src.parsers.source_registry import SourceRegistry

    usage = TokenUsage.from_legacy_dict(
        {
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "cache_write_tokens": 50,
            "output_tokens": 10,
            "total_tokens": 110,
        },
        preserve_total=True,
    )
    event = UsageEvent.from_legacy_dict({
        "timestamp": "2026-09-18T10:00:00+00:00",
        "model": "gpt-5.6-luna",
        "input_tokens": 100,
        "cached_input_tokens": 0,
        "cache_write_tokens": 50,
        "output_tokens": 10,
        "total_tokens": 110,
    })

    class ComponentTotalSource:
        key = "component-total-source"
        aliases = ()
        provider = "codex"

        def extract_sessions(self, root=None):
            return [UsageSession(
                id="component-total",
                tool=self.key,
                provider=self.provider,
                model="gpt-5.6-luna",
                created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
                usage=usage,
                events=[event],
            )]

    result = get_tool_usage("all", registry=SourceRegistry((ComponentTotalSource(),)))

    assert result["summary"]["total_tokens"] == 160
    assert result["models"][0]["total_tokens"] == 160
    assert result["timeline"][0]["total_tokens"] == 160
    assert result["sessions"][0]["total_tokens"] == 160
    assert result["sessions"][0]["reported_total_tokens"] == 110


def test_cache_write_fields_round_trip_through_legacy_session_contract() -> None:
    usage = TokenUsage(
        input_tokens=100,
        output_tokens=10,
        cache_write_tokens=70,
        cache_write_5m_tokens=30,
        cache_write_1h_tokens=40,
    )
    session = UsageSession(
        id="cache-round-trip",
        tool="claude-code",
        model="claude-sonnet-4-20250514",
        usage=usage,
    )

    restored = UsageSession.from_legacy_dict(session.to_legacy_dict())

    assert restored.usage.cache_write_tokens == 70
    assert restored.usage.cache_write_5m_tokens == 30
    assert restored.usage.cache_write_1h_tokens == 40
    assert restored.usage.total_input == 170
    assert restored.usage.total_tokens == 180


def test_codex_cache_write_only_usage_is_preserved() -> None:
    usage = _normalize_usage(
        {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 1_000_000,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 1_000_000,
        }
    )

    assert usage is not None
    assert usage["cache_write_input_tokens"] == 1_000_000
    assert usage["total_tokens"] == 1_000_000


class MixedModelSource:
    key = "mixed-source"
    aliases = ("mixed",)
    provider = "codex"

    def extract_sessions(self, root=None):
        common = {
            "id": "thread-1",
            "tool": self.key,
            "provider": self.key,
            "title": "Mixed model thread",
        }
        return [
            UsageSession(
                **common,
                created_at=datetime.fromisoformat("2026-09-18T10:00:00+00:00"),
                start_time=datetime.fromisoformat("2026-09-18T10:00:00+00:00"),
                activity_at=datetime.fromisoformat("2026-09-18T10:01:00+00:00"),
                model="gpt-5.6-luna",
                usage=TokenUsage(input_tokens=100, output_tokens=10),
                events=[UsageEvent(
                    event_id="luna-call",
                    timestamp=datetime.fromisoformat("2026-09-18T10:00:00+00:00"),
                    model="gpt-5.6-luna",
                    usage=TokenUsage(input_tokens=100, output_tokens=10),
                )],
            ),
            UsageSession(
                **common,
                model="gpt-6-astra",
                created_at=datetime.fromisoformat("2026-09-18T10:01:00+00:00"),
                start_time=datetime.fromisoformat("2026-09-18T10:01:00+00:00"),
                activity_at=datetime.fromisoformat("2026-09-18T10:02:00+00:00"),
                usage=TokenUsage(input_tokens=200, output_tokens=20),
                events=[UsageEvent(
                    event_id="astra-call",
                    timestamp=datetime.fromisoformat("2026-09-18T10:01:00+00:00"),
                    model="gpt-6-astra",
                    usage=TokenUsage(input_tokens=200, output_tokens=20),
                )],
            ),
        ]


def test_duplicate_sessions_merge_and_models_aggregate_from_events() -> None:
    from src.parsers.aggregator import get_tool_usage

    result = get_tool_usage(
        "all",
        registry=SourceRegistry((MixedModelSource(),)),
    )

    assert result["summary"]["session_count"] == 1
    assert result["summary"]["call_count"] == 2
    assert result["summary"]["total_tokens"] == 330
    assert result["sessions"][0]["model"] == "mixed"
    assert len(result["models"]) == 2

    by_model = {row["model"]: row for row in result["models"]}
    assert by_model["gpt-5.6-luna"]["total_tokens"] == 110
    assert by_model["gpt-5.6-luna"]["call_count"] == 1
    assert by_model["gpt-5.6-luna"]["session_count"] == 1
    assert by_model["gpt-6-astra"]["total_tokens"] == 220
    assert by_model["gpt-6-astra"]["call_count"] == 1
    assert by_model["gpt-6-astra"]["session_count"] == 1
    assert sum(row["total_tokens"] for row in result["models"]) == result["summary"]["total_tokens"]
    assert round(sum(row["est_cost_cached_usd"] for row in result["models"]), 6) == result["summary"]["cost_cached_usd"]
    for collection in (result["timeline"], result["hourly_timeline"], result["weekday_hour"]):
        assert sum(row["total_tokens"] for row in collection) == result["summary"]["total_tokens"]
        assert sum(row["call_count"] for row in collection) == result["summary"]["call_count"]
        assert round(sum(row["cost_cached_usd"] for row in collection), 6) == result["summary"]["cost_cached_usd"]


class DuplicateEventSource:
    key = "duplicate-source"
    aliases = ("duplicate",)
    provider = "codex"

    def extract_sessions(self, root=None):
        usage = TokenUsage(input_tokens=100, output_tokens=10)
        session_args = {
            "id": "same-thread",
            "tool": self.key,
            "provider": self.key,
            "model": "gpt-5.6-luna",
            "created_at": datetime.fromisoformat("2026-09-18T10:00:00+00:00"),
            "usage": usage,
        }
        event = UsageEvent(
            event_id="same-event",
            timestamp=datetime.fromisoformat("2026-09-18T10:00:00+00:00"),
            model="gpt-5.6-luna",
            usage=usage,
        )
        return [
            UsageSession(**session_args, events=[event]),
            UsageSession(**session_args, events=[event]),
        ]


def test_duplicate_session_event_is_counted_once() -> None:
    from src.parsers.aggregator import get_tool_usage

    result = get_tool_usage(
        "all",
        registry=SourceRegistry((DuplicateEventSource(),)),
    )

    assert result["summary"]["session_count"] == 1
    assert result["summary"]["call_count"] == 1
    assert result["summary"]["total_tokens"] == 110


def test_merging_reported_cost_sessions_preserves_reported_total() -> None:
    from src.parsers.aggregator import _merge_usage_sessions

    sessions = []
    for index in (1, 2):
        usage = TokenUsage(input_tokens=100, output_tokens=10)
        sessions.append(UsageSession(
            id="reported-thread",
            tool="reported-source",
            provider="reported-source",
            model="reported-model",
            usage=usage,
            events=[UsageEvent(
                event_id=f"reported-call-{index}",
                timestamp=datetime.fromisoformat(f"2026-09-18T10:0{index}:00+00:00"),
                model="reported-model",
                usage=usage,
            )],
            cost=CostEstimate(
                cached_usd=0.5,
                uncached_usd=0.75,
                savings_usd=0.25,
                reported_usd=0.5,
                source="reported",
            ),
        ))

    merged = _merge_usage_sessions(sessions)

    assert len(merged) == 1
    assert merged[0].cost is not None
    assert merged[0].cost.source == "reported"
    assert float(merged[0].cost.reported_usd) == 1.0


def test_mixed_reported_and_estimated_event_costs_are_not_dropped(monkeypatch) -> None:
    import src.parsers.aggregator as aggregator

    def fake_cost(*_args, **_kwargs):
        return {
            "status": "known",
            "cost_cached_usd": 0.4,
            "cost_uncached_usd": 0.4,
            "savings_usd": 0.0,
        }

    monkeypatch.setattr(aggregator, "calculate_cost_strict", fake_cost)
    usage = TokenUsage(input_tokens=1_000_000)
    session = UsageSession(
        id="mixed-cost",
        tool="codex",
        provider="codex",
        model="gpt-5.6-sol",
        usage=TokenUsage(input_tokens=2_000_000),
        events=[
            UsageEvent(
                event_id="reported",
                timestamp=datetime(2026, 9, 18, 10, tzinfo=timezone.utc),
                model="gpt-5.6-sol",
                usage=usage,
                cost=CostEstimate(cached_usd=0.2, reported_usd=0.2, source="reported"),
            ),
            UsageEvent(
                event_id="estimated",
                timestamp=datetime(2026, 9, 18, 10, 1, tzinfo=timezone.utc),
                model="gpt-5.6-sol",
                usage=usage,
                cost=CostEstimate(cached_usd=0.4, source="estimated"),
            ),
        ],
        cost=CostEstimate(cached_usd=0.2, reported_usd=0.2, source="reported"),
    )

    aggregator._refresh_estimated_session_cost(session)

    assert session.cost is not None
    assert session.cost.source == "mixed"
    assert round(float(session.cost.cached_usd), 6) == 0.6
    assert session.cost.reported_usd is None


def test_idless_duplicate_events_are_deduplicated() -> None:
    from src.parsers.aggregator import _merge_usage_sessions

    usage = TokenUsage(input_tokens=100, output_tokens=10)
    sessions = [
        UsageSession(
            id="idless-thread",
            tool="codex",
            provider="codex",
            model="gpt-5.6-luna",
            usage=usage,
            events=[UsageEvent(
                timestamp=datetime.fromisoformat("2026-09-18T10:00:00+00:00"),
                model="gpt-5.6-luna",
                usage=usage,
            )],
        )
        for _ in range(2)
    ]

    merged = _merge_usage_sessions(sessions)

    assert len(merged) == 1
    assert len(merged[0].events) == 1
    assert merged[0].usage.total_tokens == 110
    assert merged[0].call_count == 1


def test_partial_idless_overlap_counts_only_new_calls() -> None:
    from src.parsers.aggregator import _merge_usage_sessions

    def event(minute: int, tokens: int) -> UsageEvent:
        return UsageEvent(
            timestamp=datetime(2026, 9, 18, 10, minute, tzinfo=timezone.utc),
            model="gpt-5.6-luna",
            usage=TokenUsage(input_tokens=tokens, output_tokens=1),
        )

    first = UsageSession(
        id="partial-thread",
        tool="codex",
        provider="codex",
        model="gpt-5.6-luna",
        usage=TokenUsage(input_tokens=200, output_tokens=2),
        events=[event(1, 100), event(2, 100)],
    )
    second = UsageSession(
        id="partial-thread",
        tool="codex",
        provider="codex",
        model="gpt-5.6-luna",
        usage=TokenUsage(input_tokens=200, output_tokens=2),
        events=[event(2, 100), event(3, 100)],
    )

    merged = _merge_usage_sessions([first, second])

    assert len(merged) == 1
    assert len(merged[0].events) == 3
    assert merged[0].call_count == 3
    assert merged[0].usage.total_tokens == 303


def test_overlapping_reported_segments_do_not_double_charge() -> None:
    from src.parsers.aggregator import _merge_usage_sessions

    def event(minute: int) -> UsageEvent:
        usage = TokenUsage(input_tokens=100, output_tokens=1)
        return UsageEvent(
            timestamp=datetime(2026, 9, 18, 10, minute, tzinfo=timezone.utc),
            model="reported-model",
            usage=usage,
        )

    def session(minutes: tuple[int, ...]) -> UsageSession:
        events = [event(minute) for minute in minutes]
        return UsageSession(
            id="reported-overlap",
            tool="reported-source",
            provider="reported-source",
            model="reported-model",
            usage=TokenUsage(input_tokens=200, output_tokens=2),
            events=events,
            cost=CostEstimate(
                cached_usd=10.0,
                uncached_usd=12.0,
                savings_usd=2.0,
                reported_usd=10.0,
                source="reported",
            ),
        )

    merged = _merge_usage_sessions([session((1, 2)), session((2, 3))])

    assert len(merged) == 1
    assert merged[0].cost is not None
    assert float(merged[0].cost.reported_usd) == 15.0


def test_reported_partial_overlap_cost_is_order_independent() -> None:
    from src.parsers.aggregator import _merge_usage_sessions

    def event(minute: int) -> UsageEvent:
        return UsageEvent(
            timestamp=datetime(2026, 9, 18, 10, minute, tzinfo=timezone.utc),
            model="reported-model",
            usage=TokenUsage(input_tokens=100, output_tokens=1),
        )

    def session(minutes: tuple[int, ...]) -> UsageSession:
        return UsageSession(
            id="reported-order",
            tool="reported-source",
            provider="reported-source",
            model="reported-model",
            usage=TokenUsage(input_tokens=200, output_tokens=2),
            events=[event(minute) for minute in minutes],
            call_count=2,
            cost=CostEstimate(
                cached_usd=10.0,
                uncached_usd=12.0,
                savings_usd=2.0,
                reported_usd=10.0,
                source="reported",
            ),
        )

    forward = _merge_usage_sessions([session((1, 2)), session((2,))])[0]
    reverse = _merge_usage_sessions([session((2,)), session((1, 2))])[0]

    assert forward.cost is not None
    assert reverse.cost is not None
    assert float(forward.cost.reported_usd) == 10.0
    assert float(reverse.cost.reported_usd) == 10.0


class ReportedCostSource:
    key = "reported-source"
    aliases = ("reported",)
    provider = key

    def extract_sessions(self, root=None):
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=0)
        return [UsageSession(
            id="reported-session",
            tool=self.key,
            provider=self.key,
            model="unreported-model",
            usage=usage,
            events=[UsageEvent(
                event_id="reported-event",
                timestamp=datetime.fromisoformat("2026-09-18T10:00:00+00:00"),
                model="unreported-model",
                usage=usage,
            )],
            cost=CostEstimate(
                cached_usd=5.0,
                uncached_usd=6.0,
                savings_usd=1.0,
                reported_usd=5.0,
                source="reported",
            ),
        )]


def test_reported_cost_is_consistent_across_session_models_and_timeline() -> None:
    from src.parsers.aggregator import get_tool_usage

    result = get_tool_usage(
        "all",
        registry=SourceRegistry((ReportedCostSource(),)),
    )

    assert result["summary"]["cost_cached_usd"] == 5.0
    assert result["sessions"][0]["cost_cached_usd"] == 5.0
    assert result["models"][0]["est_cost_cached_usd"] == 5.0
    assert result["models"][0]["pricing_status"] == "reported"
    assert result["models"][0]["cost_available"] is True
    assert result["sessions"][0]["cost_available"] is True
    assert result["analytics"]["top_sessions"][0]["cost_available"] is True
    assert result["timeline"][0]["cost_cached_usd"] == 5.0


def test_public_pricing_keeps_generic_and_ttl_write_components_distinct() -> None:
    catalog = PricingCatalog()
    catalog.register(
        "test-provider",
        "cache-model",
        PricingRates(
            1.0,
            0.1,
            2.0,
            cache_write=12.0,
            cache_creation=30.0,
            cache_write_5m=3.0,
            cache_write_1h=6.0,
        ),
    )

    creation_only = calculate_cost_strict(
        "cache-model",
        0,
        0,
        0,
        provider="test-provider",
        cache_creation=1_000_000,
        catalog=catalog,
    )
    aggregate_with_split = calculate_cost_strict(
        "cache-model",
        0,
        0,
        0,
        provider="test-provider",
        cache_write=2_000_000,
        cache_write_5m=1_000_000,
        cache_write_1h=1_000_000,
        catalog=catalog,
    )

    assert creation_only["cost_cached_usd"] == 30.0
    assert aggregate_with_split["cost_cached_usd"] == 9.0


def test_idless_overlap_is_stable_when_session_totals_reconcile_events() -> None:
    from src.parsers.aggregator import _merge_usage_sessions

    def event(minute: int) -> UsageEvent:
        return UsageEvent(
            timestamp=datetime(2026, 9, 18, 10, minute, tzinfo=timezone.utc),
            model="gpt-5.6-luna",
            usage=TokenUsage(input_tokens=100, output_tokens=10),
        )

    first = UsageSession(
        id="authoritative-overlap",
        tool="codex",
        provider="codex",
        model="gpt-5.6-luna",
        usage=TokenUsage(input_tokens=300, output_tokens=30, total_tokens=330),
        events=[event(1), event(2)],
        call_count=2,
    )
    second = UsageSession(
        id="authoritative-overlap",
        tool="codex",
        provider="codex",
        model="gpt-5.6-luna",
        usage=TokenUsage(input_tokens=300, output_tokens=30, total_tokens=330),
        events=[event(2), event(3)],
        call_count=2,
    )

    merged = _merge_usage_sessions([first, second])

    assert len(merged) == 1
    assert len(merged[0].events) == 3
    assert merged[0].usage.total_tokens == 330
    assert merged[0].call_count == 3


def test_duplicate_authoritative_snapshot_does_not_add_phantom_residual_calls() -> None:
    from src.parsers.aggregator import _merge_usage_sessions

    def event(minute: int) -> UsageEvent:
        return UsageEvent(
            timestamp=datetime(2026, 9, 18, 10, minute, tzinfo=timezone.utc),
            model="gpt-5.6-luna",
            usage=TokenUsage(input_tokens=100, output_tokens=10),
        )

    def session() -> UsageSession:
        return UsageSession(
            id="duplicate-authoritative",
            tool="codex",
            provider="codex",
            model="gpt-5.6-luna",
            usage=TokenUsage(input_tokens=300, output_tokens=30, total_tokens=330),
            events=[event(1), event(2)],
            call_count=3,
        )

    merged = _merge_usage_sessions([session(), session()])

    assert len(merged) == 1
    assert len(merged[0].events) == 2
    assert merged[0].usage.total_tokens == 330
    assert merged[0].call_count == 3


class AuthoritativeOverlapSource:
    key = "authoritative-overlap-source"
    aliases = ("authoritative-overlap",)
    provider = "codex"

    def extract_sessions(self, root=None):
        def event(minute: int) -> UsageEvent:
            return UsageEvent(
                timestamp=datetime(2026, 9, 18, 10, minute, tzinfo=timezone.utc),
                model="gpt-5.6-luna",
                usage=TokenUsage(input_tokens=100, output_tokens=10),
            )

        common = {
            "id": "authoritative-api-overlap",
            "tool": self.key,
            "provider": self.key,
            "model": "gpt-5.6-luna",
            "call_count": 2,
        }
        return [
            UsageSession(
                **common,
                usage=TokenUsage(input_tokens=300, output_tokens=30, total_tokens=330),
                events=[event(1), event(2)],
            ),
            UsageSession(
                **common,
                usage=TokenUsage(input_tokens=300, output_tokens=30, total_tokens=330),
                events=[event(2), event(3)],
            ),
        ]


def test_authoritative_overlap_api_aggregates_match_summary() -> None:
    from src.parsers.aggregator import get_tool_usage

    result = get_tool_usage(
        "all",
        registry=SourceRegistry((AuthoritativeOverlapSource(),)),
    )

    summary = result["summary"]
    assert summary["total_tokens"] == 330
    for collection in (result["models"], result["sessions"], result["timeline"]):
        assert sum(row["total_tokens"] for row in collection) == summary["total_tokens"]
        assert sum(row["call_count"] for row in collection) == summary["call_count"]


class ZeroTokenReportedSource:
    key = "zero-reported-source"
    aliases = ("zero-reported",)
    provider = key

    def extract_sessions(self, root=None):
        return [UsageSession(
            id="zero-reported-session",
            tool=self.key,
            provider=self.key,
            model="gpt-5.6-sol",
            usage=TokenUsage(),
            events=[UsageEvent(
                timestamp=datetime(2026, 9, 18, 10, tzinfo=timezone.utc),
                model="gpt-5.6-sol",
                usage=TokenUsage(),
            )],
            cost=CostEstimate(
                cached_usd=5.0,
                uncached_usd=6.0,
                savings_usd=1.0,
                reported_usd=5.0,
                source="reported",
            ),
            call_count=2,
        )]


def test_reported_zero_token_session_preserves_cost_and_residual_calls() -> None:
    from src.parsers.aggregator import get_tool_usage

    result = get_tool_usage(
        "all",
        registry=SourceRegistry((ZeroTokenReportedSource(),)),
    )

    summary = result["summary"]
    assert summary["cost_cached_usd"] == 5.0
    assert summary["call_count"] == 2
    assert result["sessions"][0]["cost_cached_usd"] == 5.0
    assert result["models"][0]["est_cost_cached_usd"] == 5.0
    assert sum(row["call_count"] for row in result["timeline"]) == 2
    assert sum(row["cost_cached_usd"] for row in result["timeline"]) == 5.0


class TimezoneSource:
    key = "timezone-source"
    aliases = ("timezone",)
    provider = key

    def extract_sessions(self, root=None):
        usage = TokenUsage(input_tokens=200, output_tokens=20)
        return [UsageSession(
            id="local-day-event",
            tool=self.key,
            provider=self.key,
            model="gpt-5.6-luna",
            created_at=datetime.fromisoformat("2026-09-18T12:00:00+00:00"),
            usage=usage,
            events=[UsageEvent(
                event_id="local-day-call",
                timestamp=datetime.fromisoformat("2026-09-19T03:30:00+00:00"),
                model="gpt-5.6-luna",
                usage=usage,
            )],
        )]


def test_custom_range_uses_and_reports_dashboard_local_timezone(monkeypatch) -> None:
    from src.parsers.aggregator import get_tool_usage

    monkeypatch.setenv("AI_USAGE_TIMEZONE", "America/New_York")
    result = get_tool_usage(
        "all",
        time_range="custom",
        start="2026-09-18",
        end="2026-09-18",
        registry=SourceRegistry((TimezoneSource(),)),
    )

    assert result["timezone"] == "America/New_York"
    assert result["summary"]["total_tokens"] == 220
    assert [row["date"] for row in result["timeline"] if row["total_tokens"]] == ["2026-09-18"]
    assert result["analytics"]["active_days"] == 1


def test_open_ended_custom_range_preserves_dashboard_timezone(monkeypatch) -> None:
    from src.parsers.aggregator import _parse_custom_range

    monkeypatch.setenv("AI_USAGE_TIMEZONE", "America/New_York")
    now = datetime.fromisoformat("2026-09-18T12:00:00+00:00")
    start, end = _parse_custom_range("2026-09-01", None, now=now)

    assert start.isoformat() == "2026-09-01T00:00:00-04:00"
    assert end.isoformat() == "2026-09-18T08:00:00-04:00"
    assert getattr(start.tzinfo, "key", None) == "America/New_York"
    assert getattr(end.tzinfo, "key", None) == "America/New_York"


def test_dst_fallback_day_uses_elapsed_utc_duration() -> None:
    from src.parsers.aggregator import _filter_period_days

    new_york = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, tzinfo=new_york)
    end = datetime(2026, 11, 2, tzinfo=new_york)

    assert _filter_period_days(start, end, []) == 25 / 24


def test_activity_buckets_use_dst_aware_event_timezone() -> None:
    from src.parsers.aggregator import _build_usage_data

    new_york = ZoneInfo("America/New_York")
    session = {
        "id": "dst-event",
        "tool": "codex",
        "provider": "codex",
        "model": "gpt-5.6-luna",
        "created_at": "2026-07-01T02:30:00+00:00",
        "call_count": 1,
        "uncached_input": 200,
        "cached_input": 0,
        "total_input": 200,
        "output": 20,
        "reasoning_output": 0,
        "total_tokens": 220,
        "cost_cached_usd": 0.1,
        "cost_uncached_usd": 0.2,
        "savings_usd": 0.1,
        "usage_events": [{
            "timestamp": "2026-07-01T02:30:00+00:00",
            "model": "gpt-5.6-luna",
            "input_tokens": 200,
            "cached_input_tokens": 0,
            "output_tokens": 20,
            "reasoning_output_tokens": 0,
            "total_tokens": 220,
            "cost_cached_usd": 0.1,
            "cost_uncached_usd": 0.2,
            "savings_usd": 0.1,
        }],
    }

    result = _build_usage_data(
        [session],
        "codex",
        window_start=datetime(2026, 1, 1, tzinfo=new_york),
        window_end=datetime(2026, 12, 31, 23, 59, tzinfo=new_york),
    )

    assert result["timezone"] == "America/New_York"
    assert [row["date"] for row in result["timeline"] if row["total_tokens"]] == ["2026-06-30"]


def test_activity_buckets_keep_cache_writes_separate_from_output() -> None:
    from src.parsers.aggregator import _build_usage_data

    session = {
        "id": "cache-write-event",
        "tool": "claude-code",
        "provider": "claude",
        "model": "claude-sonnet-4-20250514",
        "created_at": "2026-09-18T10:00:00+00:00",
        "call_count": 1,
        "uncached_input": 100,
        "cached_input": 0,
        "total_input": 125,
        "cache_write": 25,
        "output": 7,
        "reasoning_output": 0,
        "total_tokens": 132,
        "cost_cached_usd": 0.1,
        "cost_uncached_usd": 0.2,
        "savings_usd": 0.1,
        "usage_events": [{
            "timestamp": "2026-09-18T10:00:00+00:00",
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "cache_write_tokens": 25,
            "output_tokens": 7,
            "reasoning_output_tokens": 0,
            "total_tokens": 132,
            "cost_cached_usd": 0.1,
            "cost_uncached_usd": 0.2,
            "savings_usd": 0.1,
        }],
    }

    result = _build_usage_data(
        [session],
        "claude-code",
        window_start=datetime(2026, 9, 18, tzinfo=ZoneInfo("UTC")),
        window_end=datetime(2026, 9, 18, 23, 59, tzinfo=ZoneInfo("UTC")),
    )

    row = next(row for row in result["timeline"] if row["total_tokens"])
    assert row["cache_write"] == 25
    assert row["output"] == 7
    assert row["total_input"] == 125
    assert row["total_tokens"] == 132
