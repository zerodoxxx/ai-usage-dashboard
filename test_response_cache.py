"""Response/session caching and pricing-resolution memoization."""

from __future__ import annotations

import src.app as app_module
from src.parsers import aggregator
from src.pricing import PricingCatalog, PricingRates
from src.usage_store import write_usage_sessions
from test_store_read_path import TestClient, _make_session


def _usage(client, query: str = "tool=codex&time_range=all") -> dict:
    response = client.get(f"/api/usage?{query}")
    assert response.status_code == 200
    return response.json()


def test_cache_hit_skips_recomputation_and_write_invalidates(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "refresh_pricing", lambda: None)
    app_module._USAGE_RESPONSE_CACHE.clear()
    aggregator._SESSION_CACHE.clear()
    first = _make_session("codex", "cache-1", "One", "gpt-6-luna",
                          input_tokens=1_000, cached_tokens=0, output_tokens=100, reported_cost=0.5)
    write_usage_sessions("codex", [first])

    calls = []
    real = aggregator.get_tool_usage

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module, "get_tool_usage", counting)
    with TestClient(app_module.app) as client:
        # The first read may create the -wal/-shm files, which legitimately
        # changes the DB signature, so warm up once before measuring hits.
        client.get("/api/usage?tool=codex&time_range=all")
        before = client.get("/api/usage?tool=codex&time_range=all")
        warm_calls = len(calls)
        again = client.get("/api/usage?tool=codex&time_range=all")
        assert again.content == before.content
        assert len(calls) == warm_calls  # the poll was served from the cache

        second = _make_session("codex", "cache-2", "Two", "gpt-6-luna",
                               input_tokens=2_000, cached_tokens=0, output_tokens=200, reported_cost=0.7)
        write_usage_sessions("codex", [second])
        after = _usage(client)
        assert len(calls) == warm_calls + 1
        assert len(after["sessions"]) == len(before.json()["sessions"]) + 1
        assert after["summary"]["total_cost_usd"] != before.json()["summary"]["total_cost_usd"]


def test_cache_is_keyed_by_database_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_module, "refresh_pricing", lambda: None)
    app_module._USAGE_RESPONSE_CACHE.clear()
    aggregator._SESSION_CACHE.clear()
    write_usage_sessions("codex", [_make_session("codex", "path-a", "A", "gpt-6-luna",
                                                 input_tokens=10, cached_tokens=0, output_tokens=1, reported_cost=0.1)])
    with TestClient(app_module.app) as client:
        assert len(_usage(client)["sessions"]) == 1
        monkeypatch.setenv("AI_USAGE_DB_PATH", str(tmp_path / "other.db"))
        assert _usage(client)["sessions"] == []


def test_resolve_memo_matches_uncached_and_tracks_catalog_changes() -> None:
    catalog = PricingCatalog()
    catalog.register("claude", "claude-sonnet-4", {"uncached_input": 3.0, "cached_input": 0.3, "output": 15.0},
                     aliases=("sonnet",))
    for name, provider in (("claude/sonnet", None), ("sonnet", "claude"), ("nope", None), ("", None)):
        memoized = catalog.resolve(name, provider)
        assert catalog.resolve(name, provider) == memoized
        assert memoized == catalog._resolve_uncached(name, provider) if name else True
    version = catalog.version
    assert catalog.resolve("brand-new").status == "unknown"
    catalog.register("claude", "brand-new", {"uncached_input": 1.0, "cached_input": 0.1, "output": 2.0})
    assert catalog.version != version
    assert catalog.resolve("brand-new").rates == PricingRates(1.0, 0.1, 2.0)
