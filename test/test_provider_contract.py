"""Focused tests for the provider-neutral parser contracts."""

from datetime import datetime, timezone
import unittest

from src.parsers.contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession, cache_hit_rate
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

    def test_cache_hit_rate_counts_cache_writes_in_the_denominator(self) -> None:
        # 10 uncached + 900 cache read + 90 cache write: 900 / 1000, not 900 / 910.
        usage = TokenUsage(input_tokens=910, cached_input_tokens=900, cache_write_tokens=90)
        self.assertEqual(usage.uncached_input_tokens, 10)
        self.assertEqual(usage.total_input, 1_000)
        self.assertEqual(cache_hit_rate(10, 900, 90), 90.0)

        legacy = UsageSession(id="s-cache-write", tool="claude-code", usage=usage).to_legacy_dict()
        self.assertEqual(legacy["uncached_input"], 10)
        self.assertEqual(legacy["cached_input"], 900)
        self.assertEqual(legacy["cache_write"], 90)
        self.assertEqual(legacy["cache_hit_rate"], 90.0)

    def test_cache_hit_rate_without_cache_writes_is_unchanged(self) -> None:
        # Codex and AGY never report cache writes; the rate stays cached / input.
        for uncached, cached in ((200, 800), (1, 2), (7, 0), (0, 5), (123_457, 987_651)):
            self.assertEqual(
                cache_hit_rate(uncached, cached, 0),
                round(cached / (uncached + cached) * 100.0, 2),
            )
            self.assertEqual(cache_hit_rate(uncached, cached), cache_hit_rate(uncached, cached, 0))

        usage = TokenUsage(input_tokens=1_000, cached_input_tokens=800, output_tokens=10)
        legacy = UsageSession(id="s-codex", tool="codex", usage=usage).to_legacy_dict()
        self.assertEqual(legacy["cache_write"], 0)
        self.assertEqual(legacy["cache_hit_rate"], 80.0)

    def test_cache_hit_rate_is_zero_without_input(self) -> None:
        self.assertEqual(cache_hit_rate(0, 0, 0), 0.0)
        self.assertEqual(cache_hit_rate(0, 0), 0.0)
        self.assertEqual(UsageSession(id="empty", tool="codex").to_legacy_dict()["cache_hit_rate"], 0.0)
        # Writes alone are input the cache never served.
        self.assertEqual(cache_hit_rate(0, 0, 50), 0.0)

    def test_cache_hit_rate_rounds_to_two_decimals(self) -> None:
        self.assertEqual(cache_hit_rate(28_087, 1_634_778_578, 68_786_057), 95.96)
        # 2/3 = 66.666...: rounds up to 66.67, truncation would give 66.66.
        self.assertEqual(cache_hit_rate(1, 2, 0), 66.67)

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
