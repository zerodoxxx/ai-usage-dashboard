"""Integration tests for registration-driven usage aggregation."""

from datetime import datetime, timezone

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
