#!/usr/bin/env python3
"""Verification test suite for AI Usage Dashboard parsers and pricing."""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.pricing import MODEL_PRICING, calculate_cost, get_pricing
from src.parsers.codex import parse_codex_usage
from src.parsers.agy import parse_agy_usage
from src.parsers.aggregator import get_tool_usage


def test_pricing() -> None:
    print("\n--- 1. Testing Pricing & Model Normalization ---")

    # Canonical models check
    expected_models = [
        "gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
        "gpt-4o", "gpt-4o-mini", "o1", "o3-mini",
        "Gemini 3.8 Flash (High)", "Gemini 2.5 Flash", "Gemini 2.5 Pro",
        "Gemini 1.5 Flash", "Gemini 1.5 Pro"
    ]
    for model in expected_models:
        assert model in MODEL_PRICING, f"Missing expected model in MODEL_PRICING: {model}"
    print("✓ All 13 specified models found in MODEL_PRICING.")

    # Normalization tests
    cases = [
        ("gpt-6-astra", 10.0, 1.0, 50.0),
        ("ASTRA", 10.0, 1.0, 50.0),
        ("gpt-5.6-luna", 0.20, 0.02, 1.20),
        ("Luna", 0.20, 0.02, 1.20),
        ("sol", 0.50, 0.05, 2.50),
        ("terra", 0.80, 0.08, 4.00),
        ("o1", 15.0, 7.50, 60.0),
        ("o3-mini", 1.10, 0.55, 4.40),
        ("Gemini 3.8 Flash (High)", 0.10, 0.025, 0.40),
        ("gemini 3.8 flash", 0.10, 0.025, 0.40),
        ("gemini-2.5-flash", 0.30, 0.075, 2.50),
        ("gemini-2.5-pro", 1.25, 0.3125, 10.0),
        ("gemini-1.5-flash", 0.075, 0.01875, 0.30),
        ("gemini-1.5-pro", 1.25, 0.3125, 5.0),
        (None, 0.20, 0.02, 1.20),  # Fallback to luna
        ("unknown-future-model", 0.20, 0.02, 1.20),  # Fallback
    ]

    for model_input, uncached, cached, output in cases:
        rates = get_pricing(model_input)
        assert rates["uncached_input"] == uncached, f"Failed uncached rate for {model_input}: {rates}"
        assert rates["cached_input"] == cached, f"Failed cached rate for {model_input}: {rates}"
        assert rates["output"] == output, f"Failed output rate for {model_input}: {rates}"
    print("✓ Smart model normalization and fallback verified across all aliases.")

    # Cost calculation tests
    # 1M uncached input + 1M cached input + 1M output for gpt-6-astra
    # uncached: 10.0, cached: 1.0, output: 50.0 -> cost_cached = 61.0, cost_uncached = 70.0, savings = 9.0
    c = calculate_cost("gpt-6-astra", 1_000_000, 1_000_000, 1_000_000)
    assert c["cost_cached_usd"] == 61.0, f"Expected 61.0, got {c['cost_cached_usd']}"
    assert c["cost_uncached_usd"] == 70.0, f"Expected 70.0, got {c['cost_uncached_usd']}"
    assert c["savings_usd"] == 9.0, f"Expected 9.0, got {c['savings_usd']}"
    print(f"✓ Cost calculation verified: {c}")


def test_codex() -> dict:
    print("\n--- 2. Testing Codex Parser ---")
    data = parse_codex_usage()
    assert data["tool"] == "codex", f"Expected tool 'codex', got {data['tool']}"
    assert "summary" in data and "models" in data and "timeline" in data and "sessions" in data

    s = data["summary"]
    print(f"✓ Codex Sessions Parsed: {s['session_count']}")
    print(f"✓ Codex Total Tokens:   {s['total_tokens']:,}")
    print(f"✓ Codex Cache Hit Rate: {s['cache_hit_rate']}%")
    print(f"✓ Codex Est Cost:       ${s['cost_cached_usd']:.4f} (Saved: ${s['savings_usd']:.4f})")
    print(f"✓ Codex Models Found:   {len(data['models'])}")
    for m in data["models"][:3]:
        print(f"    - {m['model']}: {m['total_tokens']:,} tokens, ${m['est_cost_cached_usd']:.4f}")
    return data


def test_agy() -> dict:
    print("\n--- 3. Testing Antigravity (AGY) Parser ---")
    data = parse_agy_usage()
    assert data["tool"] == "antigravity", f"Expected tool 'antigravity', got {data['tool']}"
    assert "summary" in data and "models" in data and "timeline" in data and "sessions" in data

    s = data["summary"]
    print(f"✓ AGY Sessions Parsed: {s['session_count']}")
    print(f"✓ AGY Total Tokens:   {s['total_tokens']:,}")
    print(f"✓ AGY Cache Hit Rate: {s['cache_hit_rate']}%")
    print(f"✓ AGY Est Cost:       ${s['cost_cached_usd']:.4f} (Saved: ${s['savings_usd']:.4f})")
    print(f"✓ AGY Models Found:   {len(data['models'])}")
    for m in data["models"]:
        print(f"    - {m['model']}: {m['total_tokens']:,} tokens, ${m['est_cost_cached_usd']:.4f}")
    return data


def test_aggregator(codex_data: dict, agy_data: dict) -> None:
    print("\n--- 4. Testing Aggregator (All Tools) ---")
    all_data = get_tool_usage("all")
    assert all_data["tool"] == "all"
    s = all_data["summary"]

    c_sum = codex_data["summary"]
    a_sum = agy_data["summary"]

    # Verify odometer math
    assert s["total_tokens"] == c_sum["total_tokens"] + a_sum["total_tokens"]
    assert s["session_count"] == c_sum["session_count"] + a_sum["session_count"]
    assert s["call_count"] == c_sum["call_count"] + a_sum["call_count"]
    assert abs(s["cost_cached_usd"] - (c_sum["cost_cached_usd"] + a_sum["cost_cached_usd"])) < 1e-4

    print(f"✓ Total Combined Sessions: {s['session_count']}")
    print(f"✓ Total Combined Tokens:   {s['total_tokens']:,}")
    print(f"✓ Total Cache Hit Rate:    {s['cache_hit_rate']}%")
    print(f"✓ Total Est Cost (Cached): ${s['cost_cached_usd']:.4f}")
    print(f"✓ Total Est Uncached Cost: ${s['cost_uncached_usd']:.4f}")
    print(f"✓ Total Savings:           ${s['savings_usd']:.4f}")
    print(f"✓ Total Timeline Days:     {len(all_data['timeline'])}")
    print(f"✓ Total Models Tracked:    {len(all_data['models'])}")

    # Verify single-tool routing
    single_c = get_tool_usage("codex")
    assert single_c["tool"] == "codex"
    single_a = get_tool_usage("antigravity")
    assert single_a["tool"] == "antigravity"
    print("✓ Direct tool dispatch ('codex', 'antigravity') verified.")


if __name__ == "__main__":
    test_pricing()
    codex_res = test_codex()
    agy_res = test_agy()
    test_aggregator(codex_res, agy_res)
    print("\n========================================")
    print("  ALL PARSER & PRICING TESTS PASSED!  ")
    print("========================================\n")
