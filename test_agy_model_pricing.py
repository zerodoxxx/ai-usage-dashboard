"""Tests for pricing agy per-call routing model ids through AGY_MODEL_ALIASES."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

import src.pricing as pricing_module
from src.litellm_pricing import AGY_MODEL_ALIASES, litellm_key_candidates
from src.pricing import (
    apply_used_model_rates,
    get_pricing_strict,
    pricing_metadata,
    refresh_pricing,
)

# Distinct per-million rates (input, cached, output) so a pass proves the target's
# LiteLLM rates were used, not some static-catalog fallback.
TARGET_RATES = {
    "gemini-3.7-flash": (1.1, 0.11, 5.5),
    "gemini-3.6-flash": (0.9, 0.09, 4.5),
    "gemini-3.1-pro-preview": (2.1, 0.21, 13.0),
}

ALIAS_CASES = [
    ("gemini-3.7-flash-tiered", "gemini-3.7-flash"),
    ("gemini-3.7-flash-control", "gemini-3.7-flash"),
    ("gemini-3.6-flash-tiered", "gemini-3.6-flash"),
    ("gemini-3.1-pro-low", "gemini-3.1-pro-preview"),
]


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

    monkeypatch.setenv("AI_USAGE_PRICING_CACHE", str(tmp_path / "default-pricing-cache.json"))

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


def _vertex_entry(rates: tuple[float, float, float]) -> dict[str, Any]:
    uncached, cached, output = rates
    return {
        "litellm_provider": "vertex_ai-language-models",
        "mode": "chat",
        "input_cost_per_token": uncached / 1e6,
        "output_cost_per_token": output / 1e6,
        "cache_read_input_token_cost": cached / 1e6,
    }


def make_agy_feed(*, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """Fake LiteLLM feed with the alias targets plus enough entries to validate."""
    feed: dict[str, Any] = {
        key: _vertex_entry(rates) for key, rates in TARGET_RATES.items() if key not in drop
    }
    feed["gemini-3.8-flash"] = _vertex_entry((0.75, 0.075, 3.75))
    feed["claude-opus-5-5"] = {
        "litellm_provider": "anthropic",
        "mode": "chat",
        "input_cost_per_token": 4e-6,
        "output_cost_per_token": 2e-5,
        "cache_read_input_token_cost": 2e-7,
    }
    feed["deepseek-v4-pro"] = {
        "litellm_provider": "deepseek",
        "mode": "chat",
        "input_cost_per_token": 1.32e-6,
        "output_cost_per_token": 3.96e-6,
        "cache_read_input_token_cost": 4.4e-8,
    }
    for i in range(1, 21):
        feed[f"dummy-openai-{i}"] = {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        }
    return feed


def _load_feed(tmp_path: Path, feed: dict[str, Any]) -> None:
    refresh_pricing(force=True, cache_path=tmp_path / "pricing.json", fetcher=lambda: feed)


def _assert_rates(rates: Any, expected: tuple[float, float, float]) -> None:
    assert rates is not None
    assert math.isclose(rates.uncached_input, expected[0])
    assert math.isclose(rates.cached_input, expected[1])
    assert math.isclose(rates.output, expected[2])


def test_alias_table_is_the_explicit_small_set() -> None:
    assert AGY_MODEL_ALIASES == dict(ALIAS_CASES)
    assert all(key == key.lower() for key in AGY_MODEL_ALIASES)


@pytest.mark.parametrize(("model", "target"), ALIAS_CASES)
def test_alias_target_follows_exact_key_candidates(model: str, target: str) -> None:
    candidates = litellm_key_candidates(model)
    assert candidates[0] == model
    assert target in candidates
    assert candidates.index(target) > candidates.index(model)


def test_alias_candidate_lookup_is_case_insensitive() -> None:
    assert litellm_key_candidates("Gemini-3.7-Flash-Tiered") == [
        "gemini-3.7-flash-tiered",
        "gemini-3-7-flash-tiered",
        "gemini-3.7-flash",
    ]
    assert "gemini-3.1-pro-preview" in litellm_key_candidates("GEMINI-3.1-PRO-LOW")
    assert "gemini-3.6-flash" in litellm_key_candidates("  gemini-3.6-flash-tiered ")


def test_unaliased_ids_candidates_are_unchanged() -> None:
    assert litellm_key_candidates("gemini-3.8-flash-tiered") == [
        "gemini-3.8-flash-tiered",
        "gemini-3-8-flash-tiered",
    ]
    assert litellm_key_candidates("gemini-default") == ["gemini-default"]


@pytest.mark.parametrize(("model", "target"), ALIAS_CASES)
def test_alias_resolves_known_with_target_rates(tmp_path: Path, model: str, target: str) -> None:
    _load_feed(tmp_path, make_agy_feed())

    assert get_pricing_strict(model, provider="antigravity").status == "unknown"

    apply_used_model_rates([("antigravity", model)])

    res = get_pricing_strict(model, provider="antigravity")
    assert res.status == "known"
    assert res.canonical_model == model
    _assert_rates(res.rates, TARGET_RATES[target])

    meta = pricing_metadata(cache_path=tmp_path / "pricing.json")
    assert meta["models"][model]["litellm_key"] == target
    assert meta["models"][model]["canonical_model"] == model


def test_alias_resolution_is_case_insensitive_end_to_end(tmp_path: Path) -> None:
    _load_feed(tmp_path, make_agy_feed())

    apply_used_model_rates([("antigravity", "Gemini-3.7-Flash-Tiered")])

    res = get_pricing_strict("Gemini-3.7-Flash-Tiered", provider="antigravity")
    assert res.status == "known"
    _assert_rates(res.rates, TARGET_RATES["gemini-3.7-flash"])


def test_alias_applies_regardless_of_provider(tmp_path: Path) -> None:
    _load_feed(tmp_path, make_agy_feed())

    apply_used_model_rates([(None, "gemini-3.6-flash-tiered")])

    res = get_pricing_strict("gemini-3.6-flash-tiered")
    assert res.status == "known"
    _assert_rates(res.rates, TARGET_RATES["gemini-3.6-flash"])


def test_exact_litellm_entry_wins_over_alias(tmp_path: Path) -> None:
    feed = make_agy_feed()
    feed["gemini-3.7-flash-tiered"] = _vertex_entry((1.7, 0.17, 7.5))
    _load_feed(tmp_path, feed)

    apply_used_model_rates([
        ("antigravity", "gemini-3.7-flash-tiered"),
        ("antigravity", "gemini-3.7-flash-control"),
    ])

    exact = get_pricing_strict("gemini-3.7-flash-tiered", provider="antigravity")
    assert exact.status == "known"
    assert exact.canonical_model == "gemini-3.7-flash-tiered"
    _assert_rates(exact.rates, (1.7, 0.17, 7.5))

    meta = pricing_metadata(cache_path=tmp_path / "pricing.json")
    assert meta["models"]["gemini-3.7-flash-tiered"]["litellm_key"] == "gemini-3.7-flash-tiered"

    # A sibling alias without its own entry still falls back to the target.
    sibling = get_pricing_strict("gemini-3.7-flash-control", provider="antigravity")
    assert sibling.status == "known"
    _assert_rates(sibling.rates, TARGET_RATES["gemini-3.7-flash"])


def test_alias_target_used_when_no_exact_litellm_entry(tmp_path: Path) -> None:
    feed = make_agy_feed()
    assert "gemini-3.7-flash-tiered" not in feed
    _load_feed(tmp_path, feed)

    apply_used_model_rates([("antigravity", "gemini-3.7-flash-tiered")])

    res = get_pricing_strict("gemini-3.7-flash-tiered", provider="antigravity")
    assert res.status == "known"
    assert res.canonical_model == "gemini-3.7-flash-tiered"
    _assert_rates(res.rates, TARGET_RATES["gemini-3.7-flash"])

    meta = pricing_metadata(cache_path=tmp_path / "pricing.json")
    assert meta["models"]["gemini-3.7-flash-tiered"]["litellm_key"] == "gemini-3.7-flash"


def test_alias_stays_unknown_when_target_missing_from_index(tmp_path: Path) -> None:
    _load_feed(tmp_path, make_agy_feed(drop=("gemini-3.1-pro-preview",)))

    apply_used_model_rates([("antigravity", "gemini-3.1-pro-low")])

    assert get_pricing_strict("gemini-3.1-pro-low", provider="antigravity").status == "unknown"


@pytest.mark.parametrize("model", ["gemini-default", "gemini-3-flash-a"])
def test_unlisted_routing_ids_stay_unknown(tmp_path: Path, model: str) -> None:
    _load_feed(tmp_path, make_agy_feed())

    assert litellm_key_candidates(model)[0] == model

    apply_used_model_rates([("antigravity", model)])

    assert get_pricing_strict(model, provider="antigravity").status == "unknown"


def test_gemini_38_flash_tiered_resolves_as_before(tmp_path: Path) -> None:
    before = get_pricing_strict("gemini-3.8-flash-tiered", provider="antigravity")
    assert before.status == "known"
    assert before.canonical_model == "Gemini 3.8 Flash (High)"

    _load_feed(tmp_path, make_agy_feed())
    apply_used_model_rates([
        ("antigravity", "gemini-3.8-flash-tiered"),
        ("antigravity", "gemini-3.7-flash-tiered"),
    ])

    after = get_pricing_strict("gemini-3.8-flash-tiered", provider="antigravity")
    assert after.status == "known"
    assert after.canonical_model == before.canonical_model
    assert after.rates == before.rates
