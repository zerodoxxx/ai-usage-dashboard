"""Focused tests for the provider-aware pricing catalog."""

from src.pricing import (
    MODEL_PRICING,
    PricingCatalog,
    PricingRates,
    calculate_cost,
    calculate_cost_strict,
    get_pricing,
    get_pricing_strict,
)


def test_catalog_resolves_provider_and_alias() -> None:
    catalog = PricingCatalog()
    catalog.register("claude-code", "claude-sonnet-4", {
        "uncached_input": 3.0,
        "cached_input": 0.3,
        "output": 15.0,
        "cache_creation": 3.75,
    }, aliases=("sonnet",))

    resolution = catalog.resolve("claude/sonnet")
    assert resolution.status == "known"
    assert resolution.provider == "claude"
    assert resolution.canonical_model == "claude-sonnet-4"
    assert resolution.rates == PricingRates(3.0, 0.3, 15.0, cache_creation=3.75)


def test_strict_api_distinguishes_unknown_and_unpriced() -> None:
    catalog = PricingCatalog()
    catalog.register("future-tool", "future-model", None)

    assert catalog.resolve("not-a-model").status == "unknown"
    unpriced = catalog.resolve("future-model", provider="future-tool")
    assert unpriced.status == "unpriced"
    assert unpriced.rates is None


def test_strict_cost_does_not_use_luna_for_unknown() -> None:
    result = calculate_cost_strict("made-up-model", 10, 20, 30)
    assert result["status"] == "unknown"
    assert result["cost_cached_usd"] is None
    assert result["cost_uncached_usd"] is None


def test_optional_cache_creation_is_included_in_strict_cost() -> None:
    catalog = PricingCatalog()
    catalog.register("claude", "claude-sonnet", PricingRates(3.0, 0.3, 15.0, cache_creation=3.75))
    result = calculate_cost_strict(
        "claude-sonnet", 1_000_000, 0, 0, cache_creation=1_000_000, catalog=catalog
    )
    assert result["status"] == "known"
    assert result["cost_cached_usd"] == 6.75

    normalized_result = calculate_cost_strict(
        "claude-sonnet", 1_000_000, 0, 0,
        cache_write=1_000_000,
        catalog=catalog,
    )
    assert normalized_result["cost_cached_usd"] == 6.75


def test_legacy_exports_and_fallbacks_remain_compatible() -> None:
    assert set(MODEL_PRICING["gpt-6-astra"]) == {"uncached_input", "cached_input", "output"}
    assert get_pricing("GPT-6-ASTRA") == MODEL_PRICING["gpt-6-astra"]
    assert get_pricing("unrecognized-model") == MODEL_PRICING["gpt-5.6-luna"]
    assert get_pricing_strict("unrecognized-model").status == "unknown"
    assert calculate_cost("gpt-6-astra", 1_000_000, 1_000_000, 1_000_000)["cost_cached_usd"] == 61.0
