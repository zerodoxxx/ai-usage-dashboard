"""Tests for LiteLLM pricing feed integration and catalog refresh."""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import src.pricing as pricing_module
from src.litellm_pricing import (
    LITELLM_ALLOWED_HOST,
    LITELLM_MAX_RESPONSE_BYTES,
    LITELLM_PRICING_URL,
    build_index,
    litellm_key_candidates,
    lookup,
    parse_feed,
    round_rate,
    validate_index,
)
from src.parsers.contracts import CostEstimate, TokenUsage, UsageSession
from src.pricing import (
    PricingCatalog,
    PricingRates,
    active_pricing_payload,
    apply_used_model_rates,
    calculate_cost_strict,
    get_pricing_strict,
    pricing_metadata,
    refresh_pricing,
)


@pytest.fixture(autouse=True)
def isolate_pricing_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate global pricing state and forbid network access during unit tests."""
    def forbidden_urlopen(*_args, **_kwargs):
        raise AssertionError("Network access is forbidden in unit tests")

    monkeypatch.setattr(pricing_module, "urlopen", forbidden_urlopen)

    orig_entries = dict(pricing_module.PRICING_CATALOG._entries)
    orig_model_pricing = dict(pricing_module.MODEL_PRICING)
    orig_pricing_states = {k: dict(v) for k, v in pricing_module._PRICING_STATES.items()}
    orig_index_by_key = {k: dict(v) for k, v in pricing_module._PRICING_INDEX_BY_KEY.items()}
    orig_active_key = pricing_module._ACTIVE_PRICING_CACHE_KEY
    orig_used_models = set(pricing_module._USED_MODELS)
    orig_applied_pairs_version = dict(pricing_module._APPLIED_PAIRS_VERSION)
    orig_version = pricing_module._PRICING_INDEX_VERSION

    default_cache = tmp_path / "default-pricing-cache.json"
    monkeypatch.setenv("AI_USAGE_PRICING_CACHE", str(default_cache))

    yield

    pricing_module.PRICING_CATALOG._entries = orig_entries
    pricing_module.MODEL_PRICING.clear()
    pricing_module.MODEL_PRICING.update(orig_model_pricing)
    pricing_module._PRICING_STATES.clear()
    pricing_module._PRICING_STATES.update(orig_pricing_states)
    pricing_module._PRICING_INDEX_BY_KEY.clear()
    pricing_module._PRICING_INDEX_BY_KEY.update(orig_index_by_key)
    pricing_module._ACTIVE_PRICING_CACHE_KEY = orig_active_key
    pricing_module._USED_MODELS.clear()
    pricing_module._USED_MODELS.update(orig_used_models)
    pricing_module._APPLIED_PAIRS_VERSION.clear()
    pricing_module._APPLIED_PAIRS_VERSION.update(orig_applied_pairs_version)
    pricing_module._PRICING_INDEX_VERSION = orig_version


def make_feed_fixture(**overrides: dict[str, Any]) -> dict[str, Any]:
    """Return a valid test feed with standard models, providers, and test junk."""
    feed: dict[str, Any] = {
        "sample_spec": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
        "openrouter/anthropic/claude-3": {
            "litellm_provider": "anthropic",
            "mode": "chat",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
        "bedrock/anthropic.claude-v2": {
            "litellm_provider": "anthropic",
            "mode": "chat",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
        "zero-cost": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
        },
        "missing-output": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 1e-6,
        },
        "nan-cost": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": float("nan"),
            "output_cost_per_token": 1e-6,
        },
        "negative-cost": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": -1e-6,
            "output_cost_per_token": 1e-6,
        },
        "bool-cost": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": True,
            "output_cost_per_token": 1e-6,
        },
        "gemini/gemini-3.8-flash": {
            "litellm_provider": "gemini",
            "mode": "chat",
            "input_cost_per_token": 8e-7,
            "output_cost_per_token": 4e-6,
        },
        "gemini-3.8-flash": {
            "litellm_provider": "vertex_ai-language-models",
            "mode": "chat",
            "input_cost_per_token": 7.5e-7,
            "output_cost_per_token": 3.75e-6,
            "cache_read_input_token_cost": 7.5e-8,
        },
        "claude-opus-5-5": {
            "litellm_provider": "anthropic",
            "mode": "chat",
            "input_cost_per_token": 4e-6,
            "output_cost_per_token": 2e-5,
            "cache_read_input_token_cost": 2e-7,
            "cache_creation_input_token_cost": 5e-6,
            "cache_creation_input_token_cost_above_1hr": 8e-6,
        },
        "claude-sonnet-5-5": {
            "litellm_provider": "anthropic",
            "mode": "chat",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 1e-5,
            "cache_read_input_token_cost": 2e-7,
            "cache_creation_input_token_cost": 2.5e-6,
        },
        "deepseek-v4-pro": {
            "litellm_provider": "deepseek",
            "mode": "chat",
            "input_cost_per_token": 1.32e-6,
            "output_cost_per_token": 3.96e-6,
            "cache_read_input_token_cost": 4.4e-8,
            "cache_creation_input_token_cost": 0.0,
        },
        "gpt-6.1-sol": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 1e-5,
            "cache_read_input_token_cost": 1e-7,
            "cache_creation_input_token_cost": 2.5e-6,
        },
        "gpt-5.6-luna": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 2e-7,
            "output_cost_per_token": 1.2e-6,
            "cache_read_input_token_cost": 2e-8,
            "cache_creation_input_token_cost": 2.5e-7,
        },
        "gpt-5.6-sol": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 4e-6,
            "output_cost_per_token": 2e-5,
            "cache_read_input_token_cost": 4e-7,
        },
        "gpt-fallback-no-cache": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
    }
    for i in range(1, 15):
        feed[f"dummy-openai-{i}"] = {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        }
    feed.update(overrides)
    return feed


def test_build_index_filtering_and_conversion() -> None:
    raw = make_feed_fixture()
    index = build_index(raw)

    assert "sample_spec" not in index
    assert "openrouter/anthropic/claude-3" not in index
    assert "bedrock/anthropic.claude-v2" not in index
    assert "zero-cost" not in index
    assert "missing-output" not in index
    assert "nan-cost" not in index
    assert "negative-cost" not in index
    assert "bool-cost" not in index

    # Bare key wins over prefix-stripped duplicate
    assert index["gemini-3.8-flash"]["litellm_provider"] == "vertex_ai-language-models"
    assert math.isclose(index["gemini-3.8-flash"]["uncached_input"], 0.75)
    assert math.isclose(index["gemini-3.8-flash"]["cached_input"], 0.075)
    assert math.isclose(index["gemini-3.8-flash"]["output"], 3.75)

    # Cached input falls back to uncached when missing
    assert math.isclose(
        index["gpt-fallback-no-cache"]["cached_input"],
        index["gpt-fallback-no-cache"]["uncached_input"],
    )

    # Per-million rate conversion and cache write rates
    opus = index["claude-opus-5-5"]
    assert math.isclose(opus["uncached_input"], 4.0)
    assert math.isclose(opus["cached_input"], 0.2)
    assert math.isclose(opus["output"], 20.0)
    assert math.isclose(opus["cache_write"], 5.0)
    assert math.isclose(opus["cache_write_5m"], 5.0)
    assert math.isclose(opus["cache_write_1h"], 8.0)

    validate_index(index)


def test_litellm_key_candidates_examples() -> None:
    cands_gemini = litellm_key_candidates("Gemini 3.8 Flash (High)")
    assert cands_gemini[0] == "gemini-3.8-flash"

    cands_opus = litellm_key_candidates("Claude Opus 4.6 (Thinking)")
    assert "claude-opus-4-6" in cands_opus

    cands_1m = litellm_key_candidates("claude-opus-5-5[1m]")
    assert cands_1m == ["claude-opus-5-5"]

    cands_sol = litellm_key_candidates("gpt-6.1-sol")
    assert cands_sol[0] == "gpt-6.1-sol"


def test_apply_used_model_rates_behavior(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture()
    refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)

    apply_used_model_rates([
        ("codex", "gpt-6.1-sol"),
        ("antigravity", "Gemini 3.8 Flash (High)"),
        ("codex", "codex-auto-review"),
        ("claude", "claude-opus-5-5"),
        ("deepseek", "deepseek-v4-pro"),
        ("codex", "gpt-4o"),  # absent from feed, present in bundled
        ("codex", "unknown-fantasy-model"),  # absent from both
    ])

    # gpt-6.1-sol gets registered as its own canonical with its LiteLLM rates
    sol_cost = calculate_cost_strict("gpt-6.1-sol", 1_000_000, 0, 1_000_000, provider="codex")
    assert sol_cost["canonical_model"] == "gpt-6.1-sol"
    assert math.isclose(sol_cost["rates"]["uncached_input"], 2.0)
    assert math.isclose(sol_cost["rates"]["cached_input"], 0.1)
    assert math.isclose(sol_cost["rates"]["output"], 10.0)

    # Gemini 3.8 Flash (High) updated from index
    gemini_cost = calculate_cost_strict("Gemini 3.8 Flash (High)", 1_000_000, 0, 1_000_000, provider="antigravity")
    assert gemini_cost["canonical_model"] == "Gemini 3.8 Flash (High)"
    assert math.isclose(gemini_cost["rates"]["uncached_input"], 0.75)

    # codex-auto-review gets gpt-5.6-luna rates via alias
    car_cost = calculate_cost_strict("codex-auto-review", 1_000_000, 0, 1_000_000, provider="codex")
    assert car_cost["canonical_model"] == "gpt-5.6-luna"
    assert math.isclose(car_cost["rates"]["uncached_input"], 0.2)

    # claude-opus-5-5 gets 4.0/0.2/20.0 with cache writes 5.0 and 8.0
    claude_cost = calculate_cost_strict("claude-opus-5-5", 1_000_000, 0, 1_000_000, provider="claude")
    assert math.isclose(claude_cost["rates"]["uncached_input"], 4.0)
    assert math.isclose(claude_cost["rates"]["cached_input"], 0.2)
    assert math.isclose(claude_cost["rates"]["output"], 20.0)
    assert math.isclose(claude_cost["rates"]["cache_write_5m"], 5.0)
    assert math.isclose(claude_cost["rates"]["cache_write_1h"], 8.0)

    # DeepSeek gets 50% off-peak rates during off-peak window
    off_peak_time = datetime(2026, 9, 14, 4, 30, tzinfo=timezone.utc)
    ds_off = calculate_cost_strict("deepseek-v4-pro", 1_000_000, 0, 1_000_000, provider="deepseek", timestamp=off_peak_time)
    assert math.isclose(ds_off["rates"]["uncached_input"], 1.32 * 0.5)
    assert math.isclose(ds_off["rates"]["output"], 3.96 * 0.5)

    # Model absent from LiteLLM keeps bundled rate
    gpt4o = get_pricing_strict("gpt-4o", provider="codex")
    assert gpt4o.status == "known"
    assert math.isclose(gpt4o.rates.uncached_input, 2.50)

    # Unknown model absent from both stays unknown
    unknown = get_pricing_strict("unknown-fantasy-model", provider="codex")
    assert unknown.status == "unknown"

    # Calling twice is idempotent
    apply_used_model_rates([("codex", "gpt-6.1-sol")])
    sol_cost_repeat = calculate_cost_strict("gpt-6.1-sol", 1_000_000, 0, 1_000_000, provider="codex")
    assert sol_cost_repeat["canonical_model"] == "gpt-6.1-sol"


def test_refresh_pricing_success_writes_cache_and_sets_live(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture()
    res = refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)

    assert res["source"] == "litellm"
    assert res["stale"] is False
    assert res["error"] is None
    assert cache_file.exists()

    disk_data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert disk_data["source"] == "litellm"
    assert disk_data["schema_version"] == 1
    assert disk_data["source_url"] == LITELLM_PRICING_URL
    assert "gpt-6.1-sol" in disk_data["index"]


def test_second_call_within_ttl_loads_from_disk_cache(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture()
    refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)

    # Clear memory state to simulate a second process/startup
    pricing_module._PRICING_STATES.clear()
    pricing_module._PRICING_INDEX_BY_KEY.clear()
    pricing_module._ACTIVE_PRICING_CACHE_KEY = None

    calls: list[int] = []

    def mock_fetcher():
        calls.append(1)
        return feed

    second = refresh_pricing(cache_path=cache_file, fetcher=mock_fetcher)
    assert second["source"] == "litellm-cache"
    assert second["stale"] is False
    assert len(calls) == 0


def test_refresh_pricing_fetch_failure_keeps_prior_rates_and_backs_off(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture()
    first = refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)
    assert first["source"] == "litellm"

    calls: list[int] = []

    def failing_fetch():
        calls.append(1)
        raise OSError("Connection refused")

    stale = refresh_pricing(force=True, cache_path=cache_file, fetcher=failing_fetch)
    assert stale["source"] == "litellm"
    assert stale["stale"] is True
    assert "Connection refused" in stale["error"]
    assert stale["next_retry_at"] is not None

    # Next call without force respects retry window and suppresses fetch
    suppressed = refresh_pricing(cache_path=cache_file, fetcher=failing_fetch)
    assert len(calls) == 1
    assert suppressed["next_retry_at"] == stale["next_retry_at"]


def test_invalid_feed_and_corrupt_cache_rejected(tmp_path: Path) -> None:
    cache_file = tmp_path / "bad-feed.json"
    too_small_feed = {
        "gpt-1": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}
    }
    result = refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: too_small_feed)
    assert result["stale"] is True
    assert "too few entries" in result["error"].lower()

    corrupt_cache = tmp_path / "corrupt.json"
    corrupt_cache.write_text(json.dumps({
        "source": "evil-source",
        "schema_version": 1,
        "source_url": "https://evil.example/pricing.json",
        "index": {},
    }), encoding="utf-8")
    assert pricing_module._read_pricing_cache(corrupt_cache) is None


def test_new_index_activation_reapplies_to_previously_used_models(tmp_path: Path) -> None:
    cache_a = tmp_path / "a.json"
    cache_b = tmp_path / "b.json"

    feed_a = make_feed_fixture()
    feed_b = make_feed_fixture(
        **{"gpt-6.1-sol": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 3e-6,
            "output_cost_per_token": 1.5e-5,
            "cache_read_input_token_cost": 2e-7,
        }}
    )

    refresh_pricing(force=True, cache_path=cache_a, fetcher=lambda: feed_a)
    apply_used_model_rates([("codex", "gpt-6.1-sol")])

    cost_a = calculate_cost_strict("gpt-6.1-sol", 1_000_000, 0, 1_000_000, provider="codex")
    assert math.isclose(cost_a["rates"]["uncached_input"], 2.0)

    # Activating feed B re-applies to gpt-6.1-sol
    refresh_pricing(force=True, cache_path=cache_b, fetcher=lambda: feed_b)
    cost_b = calculate_cost_strict("gpt-6.1-sol", 1_000_000, 0, 1_000_000, provider="codex")
    assert math.isclose(cost_b["rates"]["uncached_input"], 3.0)

    # Switching back to cache_a reactivates feed_a rates
    payload_a = active_pricing_payload(refresh=False, cache_path=cache_a)
    cost_a_restored = calculate_cost_strict("gpt-6.1-sol", 1_000_000, 0, 1_000_000, provider="codex")
    assert math.isclose(cost_a_restored["rates"]["uncached_input"], 2.0)
    assert payload_a["__meta__"]["source"] == "litellm"


def test_active_pricing_payload_exposes_models_provenance(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture()
    refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)

    apply_used_model_rates([
        ("codex", "gpt-6.1-sol"),
        ("codex", "codex-auto-review"),
    ])

    payload = active_pricing_payload(refresh=False, cache_path=cache_file)
    meta = payload["__meta__"]

    assert meta["source"] == "litellm"
    assert meta["models"]["gpt-6.1-sol"] == {
        "litellm_key": "gpt-6.1-sol",
        "canonical_model": "gpt-6.1-sol",
    }
    assert meta["models"]["codex-auto-review"] == {
        "litellm_key": "gpt-5.6-luna",
        "canonical_model": "gpt-5.6-luna",
    }
    assert "codex-auto-review" in payload
    assert "gpt-reserve" in payload
    assert payload["codex-auto-review"]["uncached_input"] == payload["gpt-5.6-luna"]["uncached_input"]


def test_conditional_get_and_304(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_file = tmp_path / "pricing.json"
    feed_json = json.dumps(make_feed_fixture()).encode("utf-8")

    class Response:
        headers = {"Content-Type": "application/json; charset=utf-8", "ETag": '"etag-123"', "Last-Modified": "Tue, 15 Sep 2026 12:00:00 GMT"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return LITELLM_PRICING_URL

        def read(self, _limit=None):
            return feed_json

    calls: list[dict[str, str]] = []

    def mock_urlopen(request, timeout=0):
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return Response()
        raise HTTPError(request.full_url, 304, "Not Modified", {}, None)

    monkeypatch.setattr(pricing_module, "urlopen", mock_urlopen)

    first = refresh_pricing(force=True, cache_path=cache_file)
    assert first["etag"] == '"etag-123"'
    assert first["source"] == "litellm"

    second = refresh_pricing(force=True, cache_path=cache_file)
    assert second["source"] == "litellm"
    assert second["stale"] is False
    assert calls[1]["If-none-match"] == '"etag-123"'
    assert calls[1]["If-modified-since"] == "Tue, 15 Sep 2026 12:00:00 GMT"


def test_network_host_redirect_content_type_and_size_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BadTypeResponse:
        headers = {"Content-Type": "text/html"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return LITELLM_PRICING_URL

        def read(self, _limit=None):
            return b"<html></html>"

    monkeypatch.setattr(pricing_module, "urlopen", lambda *_args, **_kwargs: BadTypeResponse())
    bad_type = refresh_pricing(force=True, cache_path=tmp_path / "bad-type.json")
    assert "JSON or text" in bad_type["error"]

    class RedirectResponse:
        headers = {"Content-Type": "application/json"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://evil.attacker.com/price.json"

        def read(self, _limit=None):
            return b"{}"

    monkeypatch.setattr(pricing_module, "urlopen", lambda *_args, **_kwargs: RedirectResponse())
    redirected = refresh_pricing(force=True, cache_path=tmp_path / "redirect.json")
    assert "redirected" in redirected["error"]

    class OversizedResponse:
        headers = {"Content-Type": "application/json"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return LITELLM_PRICING_URL

        def read(self, limit=None):
            return b"x" * (LITELLM_MAX_RESPONSE_BYTES + 1)

    monkeypatch.setattr(pricing_module, "urlopen", lambda *_args, **_kwargs: OversizedResponse())
    oversized = refresh_pricing(force=True, cache_path=tmp_path / "oversized.json")
    assert "size limit" in oversized["error"]


def test_parse_feed_duplicate_keys_and_non_object() -> None:
    non_obj = "[]"
    with pytest.raises(ValueError, match="JSON object"):
        parse_feed(non_obj)

    dup_json = '{"gpt-5.6-luna": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}, "gpt-5.6-luna": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}}'
    with pytest.raises(ValueError, match="Duplicate key"):
        parse_feed(dup_json)


def test_live_rates_remain_active_when_cache_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture()

    def fail_persist(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(pricing_module, "_write_pricing_cache", fail_persist)
    res = refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)
    assert res["source"] == "litellm"
    assert res["stale"] is False
    assert "read-only filesystem" in res["persistence_warning"]

    apply_used_model_rates([("codex", "gpt-6.1-sol")])
    cost = calculate_cost_strict("gpt-6.1-sol", 1_000_000, 0, 1_000_000, provider="codex")
    assert cost["canonical_model"] == "gpt-6.1-sol"
    assert math.isclose(cost["rates"]["uncached_input"], 2.0)


def test_estimated_costs_are_repriced_but_reported_cost_is_preserved(tmp_path: Path) -> None:
    from src.parsers.aggregator import _refresh_estimated_session_cost

    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture()
    refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)
    apply_used_model_rates([("codex", "gpt-6.1-sol")])

    session = UsageSession(
        id="est",
        tool="codex",
        provider="codex",
        model="gpt-6.1-sol",
        usage=TokenUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=1_000_000),
        cost=CostEstimate(cached_usd=24.0, uncached_usd=24.0, source="estimated"),
    )
    _refresh_estimated_session_cost(session)
    assert session.cost is not None
    assert math.isclose(float(session.cost.cached_usd), 12.0)

    reported = UsageSession(
        id="rep",
        tool="codex",
        provider="codex",
        model="gpt-6.1-sol",
        usage=TokenUsage(input_tokens=1_000_000),
        cost=CostEstimate(cached_usd=5.0, uncached_usd=5.0, reported_usd=0.42, source="reported"),
    )
    _refresh_estimated_session_cost(reported)
    assert reported.cost is not None
    assert math.isclose(float(reported.cost.reported_usd), 0.42)


def test_apply_used_model_rates_before_first_refresh_preserves_provenance(tmp_path: Path) -> None:
    cache_file = tmp_path / "litellm-cache.json"
    feed = make_feed_fixture(**{
        "gpt-6.1-sol": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 3.0e-6,
            "output_cost_per_token": 15.0e-6,
            "cache_read_input_token_cost": 0.5e-6,
        }
    })
    idx = build_index(feed)
    now_iso = datetime.now(timezone.utc).isoformat()
    pricing_module._write_pricing_cache(cache_file, idx, now_iso)

    # 1. apply_used_model_rates runs BEFORE the first refresh_pricing
    apply_used_model_rates([("codex", "gpt-6.1-sol")])

    # 2. First refresh_pricing loads the valid disk cache
    state = refresh_pricing(cache_path=cache_file)
    assert state["source"] == "litellm-cache"

    # 3. After the refresh, pricing_metadata(...)["models"] contains gpt-6.1-sol
    meta = pricing_metadata(cache_path=cache_file)
    assert "gpt-6.1-sol" in meta["models"]
    assert meta["models"]["gpt-6.1-sol"]["litellm_key"] == "gpt-6.1-sol"
    assert meta["models"]["gpt-6.1-sol"]["canonical_model"] == "gpt-6.1-sol"

    # 4. Catalog resolves it at the cache's rates
    res = pricing_module.PRICING_CATALOG.resolve("gpt-6.1-sol", "codex")
    assert res.status == "known"
    assert res.rates is not None
    assert math.isclose(res.rates.uncached_input, 3.0)
    assert math.isclose(res.rates.output, 15.0)
    assert math.isclose(res.rates.cached_input, 0.5)


def test_refresh_pricing_failure_with_cache_preserves_provenance(tmp_path: Path) -> None:
    cache_file = tmp_path / "litellm-cache.json"
    feed = make_feed_fixture(**{
        "gpt-6.1-sol": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 3.0e-6,
            "output_cost_per_token": 15.0e-6,
            "cache_read_input_token_cost": 0.5e-6,
        }
    })
    idx = build_index(feed)
    now_iso = datetime.now(timezone.utc).isoformat()
    pricing_module._write_pricing_cache(cache_file, idx, now_iso)

    apply_used_model_rates([("codex", "gpt-6.1-sol")])

    def failing_fetcher():
        raise RuntimeError("Network failure")

    state = refresh_pricing(force=True, cache_path=cache_file, fetcher=failing_fetcher)
    assert state["error"] == "Network failure"

    meta = pricing_metadata(cache_path=cache_file)
    assert "gpt-6.1-sol" in meta["models"]
    assert meta["models"]["gpt-6.1-sol"]["litellm_key"] == "gpt-6.1-sol"


def test_activate_pricing_never_leaves_catalog_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = make_feed_fixture(**{
        "gpt-6.1-sol": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 10e-6,
        }
    })
    idx = build_index(feed)
    pricing_module._USED_MODELS.add(("codex", "gpt-6.1-sol"))

    calls = 0
    orig_add_entry = pricing_module.PricingCatalog.add_entry

    def tracked_add_entry(self, entry):
        nonlocal calls
        calls += 1
        # Whenever an entry is added/staged during activation,
        # the live catalog must remain non-empty and resolvable.
        assert len(pricing_module.PRICING_CATALOG._entries) > 0, "Live PRICING_CATALOG was empty during activation!"
        assert len(pricing_module.MODEL_PRICING) > 0, "Live MODEL_PRICING was empty during activation!"
        res = pricing_module.PRICING_CATALOG.resolve("gpt-5.6-luna", "codex")
        assert res.status == "known", "Live catalog resolve failed during activation!"
        return orig_add_entry(self, entry)

    monkeypatch.setattr(pricing_module.PricingCatalog, "add_entry", tracked_add_entry)

    orig_register = pricing_module.PricingCatalog.register

    def tracked_register(self, *args, **kwargs):
        assert len(pricing_module.PRICING_CATALOG._entries) > 0, "Live PRICING_CATALOG was empty during register!"
        assert len(pricing_module.MODEL_PRICING) > 0, "Live MODEL_PRICING was empty during register!"
        return orig_register(self, *args, **kwargs)

    monkeypatch.setattr(pricing_module.PricingCatalog, "register", tracked_register)

    old_entries = pricing_module.PRICING_CATALOG._entries
    old_mp_id = id(pricing_module.MODEL_PRICING)

    pricing_module._activate_pricing("test-key", idx)

    assert calls > 0
    # Live catalog was swapped atomically to the staged dict
    assert pricing_module.PRICING_CATALOG._entries is not old_entries
    assert len(pricing_module.PRICING_CATALOG._entries) > 0
    # Live MODEL_PRICING dict was mutated in-place (identity preserved)
    assert id(pricing_module.MODEL_PRICING) == old_mp_id
    assert len(pricing_module.MODEL_PRICING) > 0
    # Newly activated model is resolvable
    res = pricing_module.PRICING_CATALOG.resolve("gpt-6.1-sol", "codex")
    assert res.status == "known"


def test_rate_conversion_eliminates_float_artefacts() -> None:
    feed = {
        "deepseek/deepseek-v4-pro": {
            "litellm_provider": "deepseek",
            "mode": "chat",
            "input_cost_per_token": 1.32e-06,
            "output_cost_per_token": 3.96e-06,
            "cache_read_input_token_cost": 1e-07,
            "cache_creation_input_token_cost": 0.0,
        },
        "anthropic/claude-sonnet-5-5": {
            "litellm_provider": "anthropic",
            "mode": "chat",
            "input_cost_per_token": 2e-06,
            "output_cost_per_token": 1e-05,
            "cache_read_input_token_cost": 1e-07,
        },
    }
    index = build_index(feed)

    # A feed entry with cache_read 1e-07 yields cached_input == 0.1 exactly
    assert index["deepseek-v4-pro"]["cached_input"] == 0.1
    # output 3.96e-06 yields 3.96 exactly
    assert index["deepseek-v4-pro"]["output"] == 3.96
    assert index["deepseek-v4-pro"]["uncached_input"] == 1.32

    # Derived Claude defaults and DeepSeek off-peak rates also eliminate float artefacts
    pricing_module._USED_MODELS.add(("deepseek", "deepseek-v4-pro"))
    pricing_module._USED_MODELS.add(("claude", "claude-sonnet-5-5"))
    pricing_module._activate_pricing("float-test", index)

    res_deepseek = pricing_module.PRICING_CATALOG.resolve("deepseek-v4-pro", "deepseek")
    assert res_deepseek.status == "known"
    assert res_deepseek.rates is not None
    assert res_deepseek.rates.cached_input == 0.1
    assert res_deepseek.rates.output == 3.96
    deepseek_entry = pricing_module.PRICING_CATALOG.get_entry(res_deepseek.provider, res_deepseek.canonical_model)
    assert deepseek_entry is not None
    assert deepseek_entry.off_peak_rates is not None
    assert deepseek_entry.off_peak_rates.output == 1.98
    assert deepseek_entry.off_peak_rates.cached_input == 0.05

    res_claude = pricing_module.PRICING_CATALOG.resolve("claude-sonnet-5-5", "claude")
    assert res_claude.status == "known"
    assert res_claude.rates is not None
    assert res_claude.rates.cached_input == 0.1
    assert res_claude.rates.cache_write == 2.5
    assert res_claude.rates.cache_write_1h == 4.0


def test_direct_feed_hit_does_not_overwrite_alias_target(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture(**{
        "o3": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 8e-6,
        },
        "claude-opus-5-5": {
            "litellm_provider": "anthropic",
            "mode": "chat",
            "input_cost_per_token": 15e-6,
            "output_cost_per_token": 75e-6,
        },
    })
    refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)

    # 1. Direct hit on 'o3' must NOT overwrite legacy alias target 'o3-mini'
    apply_used_model_rates([("codex", "o3")])
    o3_mini_entry = pricing_module.PRICING_CATALOG.get_entry("codex", "o3-mini")
    assert o3_mini_entry is not None
    assert o3_mini_entry.rates is not None
    assert math.isclose(o3_mini_entry.rates.uncached_input, 1.10)
    assert math.isclose(o3_mini_entry.rates.output, 4.40)

    # 'o3' is registered as its own entry
    o3_entry = pricing_module.PRICING_CATALOG.get_entry("codex", "o3")
    assert o3_entry is not None
    assert o3_entry.rates is not None
    assert math.isclose(o3_entry.rates.uncached_input, 2.0)
    assert math.isclose(o3_entry.rates.output, 8.0)

    # 2. Underlying same model ('claude-opus-5-5' <-> 'Claude Opus 5.5') DOES update
    apply_used_model_rates([("claude", "claude-opus-5-5")])
    opus_entry = pricing_module.PRICING_CATALOG.get_entry("claude", "Claude Opus 5.5")
    assert opus_entry is not None
    assert opus_entry.rates is not None
    assert math.isclose(opus_entry.rates.uncached_input, 15.0)
    assert math.isclose(opus_entry.rates.output, 75.0)

    # 3. Model with no direct hit ('codex-auto-review') follows legacy alias to 'gpt-5.6-luna'
    apply_used_model_rates([("codex", "codex-auto-review")])
    meta = pricing_metadata(cache_path=cache_file)
    assert meta["models"]["codex-auto-review"] == {
        "litellm_key": "gpt-5.6-luna",
        "canonical_model": "gpt-5.6-luna",
    }


def test_pricing_catalog_copy_on_write() -> None:
    cat = PricingCatalog()
    cat.register("codex", "m1", PricingRates(1.0, 0.5, 2.0))
    cat.register("codex", "m2", PricingRates(2.0, 1.0, 4.0))

    initial_entries_dict = cat._entries
    initial_id = id(initial_entries_dict)

    # Mutating the catalog via add_entry replaces _entries without mutating the old dict
    iterated_keys: list[tuple[str, str]] = []
    for k in initial_entries_dict:
        iterated_keys.append(k)
        cat.register("codex", f"iter-added-{k[1]}", PricingRates(3.0, 1.5, 6.0))

    assert len(iterated_keys) == 2
    assert id(cat._entries) != initial_id
    assert len(initial_entries_dict) == 2
    assert len(cat._entries) == 4

    # Remove also does copy-on-write
    pre_remove_dict = cat._entries
    pre_remove_id = id(pre_remove_dict)
    cat.remove("codex", "m1")
    assert id(cat._entries) != pre_remove_id
    assert "m1" in [k[1] for k in pre_remove_dict]
    assert "m1" not in [k[1] for k in cat._entries]


def test_feed_registered_new_entries_are_exact_only(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture(**{
        "claude-fable-5": {
            "litellm_provider": "anthropic",
            "mode": "chat",
            "input_cost_per_token": 3e-6,
            "output_cost_per_token": 15e-6,
        }
    })
    refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)
    apply_used_model_rates([("claude", "claude-fable-5")])

    # Exact match resolves
    res_exact = pricing_module.PRICING_CATALOG.resolve("claude-fable-5", "claude")
    assert res_exact.status == "known"
    assert res_exact.canonical_model == "claude-fable-5"

    # Fuzzy substring does not match exact-only entry
    res_fuzzy = pricing_module.PRICING_CATALOG.resolve("claude-fable-5-2", "claude")
    assert res_fuzzy.status == "unknown"


def test_provider_prefixed_raw_names(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture(**{
        "gpt-6.1-sol": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 10e-6,
        }
    })
    refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)
    apply_used_model_rates([("openai", "openai/gpt-6.1-sol")])

    cost = calculate_cost_strict("openai/gpt-6.1-sol", 1_000_000, 0, 1_000_000, provider="openai")
    assert cost["status"] == "known"
    assert cost["canonical_model"] == "gpt-6.1-sol"
    assert math.isclose(cost["rates"]["uncached_input"], 2.0)
    assert math.isclose(cost["rates"]["output"], 10.0)

    # Also resolves without explicit provider
    cost_no_prov = calculate_cost_strict("openai/gpt-6.1-sol", 1_000_000, 0, 1_000_000)
    assert cost_no_prov["status"] == "known"
    assert cost_no_prov["canonical_model"] == "gpt-6.1-sol"
    assert math.isclose(cost_no_prov["rates"]["uncached_input"], 2.0)
    assert math.isclose(cost_no_prov["rates"]["output"], 10.0)


def test_cache_validation_rejects_null_and_corrupt_rates(tmp_path: Path) -> None:
    valid_cache = tmp_path / "valid_cache.json"
    idx = build_index(make_feed_fixture())
    now_iso = datetime.now(timezone.utc).isoformat()
    pricing_module._write_pricing_cache(valid_cache, idx, now_iso)

    # First assert _read_pricing_cache accepts the valid cache
    valid_loaded = pricing_module._read_pricing_cache(valid_cache)
    assert valid_loaded is not None
    loaded_index, _ = valid_loaded
    assert len(loaded_index) >= 20

    base_payload = json.loads(valid_cache.read_text(encoding="utf-8"))
    target_model = "gpt-6.1-sol"
    assert target_model in base_payload["index"]

    corrupted_cases = [
        ("uncached_input", None),
        ("uncached_input", True),
        ("uncached_input", -1.0),
        ("uncached_input", float("inf")),
        ("cached_input", None),
        ("cached_input", True),
        ("cached_input", -0.5),
        ("cached_input", float("inf")),
        ("output", None),
        ("output", True),
        ("output", -2.0),
        ("output", float("inf")),
        ("cache_write", "invalid"),
        ("cache_write", -5.0),
        ("cache_write", True),
        ("cache_write", float("inf")),
    ]

    for i, (field, bad_val) in enumerate(corrupted_cases):
        corrupt_payload = copy.deepcopy(base_payload)
        corrupt_payload["index"][target_model][field] = bad_val
        cache_file = tmp_path / f"corrupt_{i}_{field}.json"
        cache_file.write_text(json.dumps(corrupt_payload), encoding="utf-8")
        assert pricing_module._read_pricing_cache(cache_file) is None, f"Expected reject for {field}={bad_val}"

    # Regression: a cache with "uncached_input": null is ignored, and refresh falls back without raising
    null_payload = copy.deepcopy(base_payload)
    null_payload["index"][target_model]["uncached_input"] = None
    null_cache = tmp_path / "null_cache.json"
    null_cache.write_text(json.dumps(null_payload), encoding="utf-8")
    assert pricing_module._read_pricing_cache(null_cache) is None

    result = refresh_pricing(cache_path=null_cache, fetcher=lambda: {"sample_spec": {}})
    assert result["source"] == "bundled"
    assert result["stale"] is True


def test_overflow_rates_not_indexed() -> None:
    feed = {
        "model-overflow": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 1e308,
            "output_cost_per_token": 2e-6,
        },
        "model-opt-overflow": {
            "litellm_provider": "anthropic",
            "mode": "chat",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
            "cache_creation_input_token_cost": 1e308,
            "cache_creation_input_token_cost_above_1hr": 1e308,
        },
    }
    index = build_index(feed)
    # Mandatory rate overflow -> entry is not indexed
    assert "model-overflow" not in index
    # Optional rate overflow -> optional rate is dropped while entry is indexed
    assert "model-opt-overflow" in index
    assert "cache_creation" not in index["model-opt-overflow"]
    assert "cache_write" not in index["model-opt-overflow"]
    assert "cache_write_1h" not in index["model-opt-overflow"]


def test_fallback_rejects_fuzzy_matches_when_no_direct_hit(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    feed = make_feed_fixture(**{
        "gpt-5.6-sol": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 4e-6,
            "output_cost_per_token": 20e-6,
        }
    })
    refresh_pricing(force=True, cache_path=cache_file, fetcher=lambda: feed)

    orig_entries_count = len(pricing_module.PRICING_CATALOG._entries)
    orig_sol_entry = pricing_module.PRICING_CATALOG.get_entry("codex", "gpt-5.6-sol")

    # gpt-7-sol has no direct index hit, but would fuzzy match gpt-5.6-sol (via alias 'sol')
    apply_used_model_rates([("codex", "gpt-7-sol")])

    meta = pricing_metadata(cache_path=cache_file)
    # Must not appear in state["models"]
    assert "gpt-7-sol" not in meta.get("models", {})

    # Catalog must be untouched for it
    assert pricing_module.PRICING_CATALOG.get_entry("codex", "gpt-7-sol") is None
    assert len(pricing_module.PRICING_CATALOG._entries) == orig_entries_count
    assert pricing_module.PRICING_CATALOG.get_entry("codex", "gpt-5.6-sol") == orig_sol_entry





