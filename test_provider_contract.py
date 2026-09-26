"""Focused tests for the provider-neutral parser contracts."""

from datetime import datetime, timezone
import unittest

from src.parsers.contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession
from src.parsers.source_registry import SourceRegistry


class ProviderContractTests(unittest.TestCase):
    def test_token_and_session_legacy_round_trip(self) -> None:
        usage = TokenUsage(input_tokens=100, cached_input_tokens=25, output_tokens=10)
        self.assertEqual(usage.uncached_input_tokens, 75)
        self.assertEqual(usage.total_tokens, 110)

        session = UsageSession(
            id="s1",
            tool="claude-code",
            model="claude-sonnet",
            created_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
            usage=usage,
            events=[UsageEvent(timestamp="2026-09-08T00:01:00Z", usage=usage)],
        )
        legacy = session.to_legacy_dict()
        restored = UsageSession.from_legacy_dict(legacy)
        self.assertEqual(restored.id, session.id)
        self.assertEqual(restored.usage.total_tokens, 110)
        self.assertEqual(restored.events[0].timestamp, datetime(2026, 9, 8, 0, 1, tzinfo=timezone.utc))

    def test_cache_writes_are_included_in_total_input_and_tokens(self) -> None:
        usage = TokenUsage(
            input_tokens=100,
            output_tokens=10,
            total_tokens=110,
            cache_write_tokens=50,
        )

        self.assertEqual(usage.total_input, 150)
        self.assertEqual(usage.total_tokens, 160)

    def test_session_total_reconciles_corrected_event_totals(self) -> None:
        session = UsageSession(
            id="s2",
            tool="codex",
            usage=TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110),
            events=[UsageEvent(usage={
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 100,
            })],
        )

        self.assertEqual(session.events[0].usage.total_tokens, 110)
        self.assertEqual(session.usage.total_tokens, 110)

    def test_registry_normalizes_and_rejects_duplicates(self) -> None:
        registry = SourceRegistry()
        source = DummySource()
        registry.register(source)
        self.assertIs(registry.get("claude-code"), source)
        self.assertIs(registry.get("CC"), source)
        self.assertEqual(registry.keys(), ("claude-code",))
        with self.assertRaises(ValueError):
            registry.register(DummySource(), key="other", aliases=("claude",))

    def test_reported_cost_is_the_dashboard_actual_cost(self) -> None:
        cost = CostEstimate(
            cached_usd="0.50",
            uncached_usd="0.75",
            reported_usd="0.42",
            source="reported",
        )
        legacy = cost.to_legacy_dict()
        self.assertEqual(legacy["cost_cached_usd"], 0.42)
        self.assertEqual(legacy["reported_cost_usd"], 0.42)


class DummySource:
    key = "Claude Code"
    aliases = ("claude", "cc")

    def extract_sessions(self, root=None):
        return []

if __name__ == "__main__":
    unittest.main()
