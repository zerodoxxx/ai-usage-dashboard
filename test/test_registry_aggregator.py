"""Integration tests for registration-driven usage aggregation."""

from datetime import datetime, timezone

import pytest

from src.parsers.aggregator import DEFAULT_SOURCE_REGISTRY, get_tool_usage
from src.parsers.contracts import TokenUsage, UsageEvent, UsageSession
from src.parsers.source_registry import SourceRegistry
from src.parsers.store_source import StoreUsageSource


class DummySource:
    key = "future-tool"
    aliases = ("future",)

    def __init__(self) -> None:
        self.received_root = None

    def extract_sessions(self, root=None):
        self.received_root = root
        usage = TokenUsage(input_tokens=100, cached_input_tokens=25, output_tokens=10)
        return [UsageSession(
            id="future-session",
            tool=self.key,
            provider=self.key,
            model="unpriced-future-model",
            title="Future provider session",
            created_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
            usage=usage,
            events=[UsageEvent(
                timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc),
                usage=usage,
            )],
        )]


def test_registered_source_needs_no_aggregator_branch() -> None:
    source = DummySource()
    registry = SourceRegistry((source,))

    result = get_tool_usage(
        "future",
        time_range="all",
        source_dirs={"future": "/tmp/future-usage"},
        registry=registry,
    )

    assert source.received_root == "/tmp/future-usage"
    assert result["tool"] == "future-tool"
    assert result["summary"]["total_tokens"] == 110
    assert result["summary"]["session_count"] == 1
    assert result["sessions"][0]["pricing_status"] == "unknown"
    assert result["sessions"][0]["cost_cached_usd"] == 0.0


def test_all_uses_every_registered_source() -> None:
    registry = SourceRegistry((DummySource(),))
    result = get_tool_usage("all", registry=registry)
    assert result["tool"] == "all"
    assert result["summary"]["call_count"] == 1


def test_default_registry_uses_only_shared_store_sources() -> None:
    assert set(DEFAULT_SOURCE_REGISTRY.keys()) == {"antigravity", "claude-code", "codex"}
    assert all(
        isinstance(source, StoreUsageSource)
        for source in DEFAULT_SOURCE_REGISTRY.list_sources()
    )


class FixedSessionSource:
    """Registered provider returning pre-built sessions."""

    def __init__(self, key: str, sessions: list[UsageSession]) -> None:
        self.key = key
        self.aliases = ()
        self._sessions = sessions

    def extract_sessions(self, root=None):
        return self._sessions


def _single_call_session(
    tool: str,
    provider: str,
    model: str,
    session_id: str,
    timestamp: datetime,
    uncached: int,
    cached: int,
    cache_write: int,
) -> UsageSession:
    usage = TokenUsage(
        input_tokens=uncached + cached,
        cached_input_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=10,
    )
    return UsageSession(
        id=session_id,
        tool=tool,
        provider=provider,
        model=model,
        created_at=timestamp,
        usage=usage,
        events=[UsageEvent(timestamp=timestamp, usage=usage)],
    )


@pytest.mark.parametrize("window", [
    {"time_range": "all"},
    {"time_range": "custom", "start": "2026-09-01", "end": "2026-09-30"},
])
def test_cache_hit_rate_counts_cache_writes_on_every_row(window, monkeypatch) -> None:
    monkeypatch.setenv("AI_USAGE_TIMEZONE", "UTC")
    day_one = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
    claude = FixedSessionSource("claude-code", [
        # 10 uncached + 900 cache read + 90 cache write.
        _single_call_session("claude-code", "claude", "claude-sonnet-5-5", "claude-a", day_one, 10, 900, 90),
        # 100 uncached + 100 cache read + 800 cache write.
        _single_call_session("claude-code", "claude", "claude-sonnet-5-5", "claude-b", day_two, 100, 100, 800),
    ])
    codex = FixedSessionSource("codex", [
        # Codex never reports cache writes: 200 uncached + 800 cache read.
        _single_call_session("codex", "codex", "gpt-5", "codex-a", day_two, 200, 800, 0),
    ])

    result = get_tool_usage("all", registry=SourceRegistry((claude, codex)), **window)

    sessions = {s["id"]: s for s in result["sessions"]}
    assert sessions["claude-a"]["cache_hit_rate"] == 90.0
    assert sessions["claude-b"]["cache_hit_rate"] == 10.0
    assert sessions["codex-a"]["cache_hit_rate"] == 80.0

    models = {m["tool"]: m for m in result["models"]}
    assert models["claude-code"]["cached_input"] == 1_000
    assert models["claude-code"]["cache_write"] == 890
    assert models["claude-code"]["cache_hit_rate"] == 50.0
    assert models["codex"]["cache_hit_rate"] == 80.0

    timeline = {row["date"]: row for row in result["timeline"]}
    assert timeline["2026-09-08"]["cache_hit_rate"] == 90.0
    # claude-b + codex-a: 900 cache read of 300 uncached + 900 read + 800 written.
    assert timeline["2026-09-09"]["cache_hit_rate"] == 45.0
    hourly = {row["hour"]: row for row in result["hourly_timeline"]}
    assert hourly[10]["cache_hit_rate"] == 60.0

    # Overall: 1,800 cache read of 310 uncached + 1,800 read + 890 written.
    assert result["summary"]["cache_hit_rate"] == 60.0

    # Every level uses the same cached / total_input definition.
    rows = [
        *result["sessions"],
        *result["models"],
        *result["timeline"],
        *result["hourly_timeline"],
        result["summary"],
    ]
    for row in rows:
        if row["total_input"] == 0:
            assert row["cache_hit_rate"] == 0.0
            continue
        assert row["total_input"] == row["uncached_input"] + row["cached_input"] + row["cache_write"]
        assert row["cache_hit_rate"] == round(row["cached_input"] / row["total_input"] * 100.0, 2)
