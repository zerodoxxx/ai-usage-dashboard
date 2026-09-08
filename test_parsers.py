#!/usr/bin/env python3
"""Verification test suite for AI Usage Dashboard parsers and pricing."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.pricing import MODEL_PRICING, calculate_cost, get_pricing
from src.parsers.codex import _parse_rollout_file, parse_codex_usage
from src.parsers.agy import parse_agy_usage
from src.parsers.aggregator import _filter_usage_data, get_tool_usage


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


def test_codex_event_deduplication() -> None:
    print("\n--- 3. Testing Codex Per-Call Events ---")
    first_usage = {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "output_tokens": 10,
        "reasoning_output_tokens": 2,
        "total_tokens": 110,
    }
    second_usage = {
        "input_tokens": 200,
        "cached_input_tokens": 100,
        "output_tokens": 20,
        "reasoning_output_tokens": 3,
        "total_tokens": 220,
    }
    cumulative = {
        "input_tokens": 300,
        "cached_input_tokens": 120,
        "output_tokens": 30,
        "reasoning_output_tokens": 5,
        "total_tokens": 330,
    }

    def token_count(timestamp: str, last_usage: dict, total_usage: dict) -> dict:
        return {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": last_usage,
                    "total_token_usage": total_usage,
                },
            },
        }

    with TemporaryDirectory() as temp_dir:
        rollout_path = Path(temp_dir) / "rollout-test.jsonl"
        lines = [
            {"timestamp": "2026-09-08T10:00:00Z", "type": "session_meta", "payload": {"model": "gpt-5.6-luna"}},
            {"timestamp": "2026-09-08T10:00:01Z", "type": "token_usage_record", "payload": {"usage": first_usage}},
            token_count("2026-09-08T10:00:01.010Z", first_usage, first_usage),
            {"timestamp": "2026-09-08T10:01:00Z", "type": "token_usage_record", "payload": {"usage": second_usage}},
            token_count("2026-09-08T10:01:00.010Z", second_usage, cumulative),
        ]
        rollout_path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
        parsed = _parse_rollout_file(rollout_path)

    assert parsed["call_count"] == 2
    assert len(parsed["usage_events"]) == 2
    assert sum(event["total_tokens"] for event in parsed["usage_events"]) == 330
    assert sum(event["cached_input_tokens"] for event in parsed["usage_events"]) == 120
    print("✓ Duplicate token record/status messages are counted once per API call.")


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

    # Verify odometer math (re-sync if live telemetry write occurred during test)
    if s["total_tokens"] != c_sum["total_tokens"] + a_sum["total_tokens"]:
        codex_data = parse_codex_usage()
        agy_data = parse_agy_usage()
        c_sum = codex_data["summary"]
        a_sum = agy_data["summary"]
        all_data = get_tool_usage("all")
        s = all_data["summary"]

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


def test_time_filters() -> None:
    print("\n--- 5. Testing Time Filters ---")
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

    def make_session(identifier: str, created_at: str, tokens: int) -> dict:
        return {
            "id": identifier,
            "tool": "codex",
            "model": "gpt-5.6-luna",
            "created_at": created_at,
            "start_time": created_at,
            "call_count": 1,
            "uncached_input": tokens,
            "cached_input": 0,
            "total_input": tokens,
            "output": 0,
            "reasoning_output": 0,
            "total_tokens": tokens,
            "cost_cached_usd": 0.1,
            "cost_uncached_usd": 0.2,
            "savings_usd": 0.1,
        }

    data = {
        "tool": "codex",
        "summary": {},
        "models": [],
        "timeline": [],
        "sessions": [
            make_session("today", "2026-09-08T12:00:00+00:00", 100),
            make_session("month", "2026-09-01T00:00:00+00:00", 200),
            make_session("old", "2026-08-31T23:59:59+00:00", 400),
        ],
    }

    expected_tokens = {"month": 300, "30d": 700, "7d": 100, "24h": 100}
    for time_range, expected in expected_tokens.items():
        result = _filter_usage_data(data, time_range, now=now)
        assert result["summary"]["total_tokens"] == expected, (time_range, result["summary"])
        assert result["summary"]["session_count"] == (2 if time_range == "month" else 1 if time_range in ("7d", "24h") else 3)
        assert result["time_range"] == time_range

    # A long-running session can span the boundary of a rolling window. The
    # filter must use its per-call records instead of excluding the whole
    # session based on its original creation date.
    event_data = {
        "tool": "codex",
        "summary": {},
        "models": [],
        "timeline": [],
        "sessions": [{
            **make_session("long-running", "2026-08-01T00:00:00+00:00", 330),
            "call_count": 2,
            "uncached_input": 180,
            "cached_input": 120,
            "total_input": 300,
            "output": 30,
            "total_tokens": 330,
            "usage_events": [
                {
                    "timestamp": "2026-09-01T00:00:00+00:00",
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 10,
                    "total_tokens": 110,
                },
                {
                    "timestamp": "2026-09-08T12:00:00+00:00",
                    "input_tokens": 200,
                    "cached_input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 220,
                },
            ],
        }],
    }

    rolling_result = _filter_usage_data(event_data, "7d", now=now)
    assert rolling_result["summary"]["total_tokens"] == 220
    assert rolling_result["summary"]["call_count"] == 1
    assert rolling_result["sessions"][0]["activity_at"] == "2026-09-08T12:00:00+00:00"
    assert "usage_events" not in rolling_result["sessions"][0]
    assert [(day["date"], day["total_tokens"], day["call_count"]) for day in rolling_result["timeline"]] == [
        ("2026-09-08", 220, 1)
    ]

    month_result = _filter_usage_data(event_data, "month", now=now)
    assert month_result["summary"]["total_tokens"] == 330
    assert month_result["summary"]["call_count"] == 2
    assert [(day["date"], day["total_tokens"], day["call_count"]) for day in month_result["timeline"]] == [
        ("2026-09-01", 110, 1),
        ("2026-09-08", 220, 1),
    ]

    all_result = _filter_usage_data(event_data, "all", now=now)
    assert [(day["date"], day["total_tokens"]) for day in all_result["timeline"]] == [
        ("2026-09-01", 110),
        ("2026-09-08", 220),
    ]

    print("✓ Calendar-month, rolling, and per-call time-range boundaries verified.")


if __name__ == "__main__":
    test_pricing()
    codex_res = test_codex()
    test_codex_event_deduplication()
    agy_res = test_agy()
    test_aggregator(codex_res, agy_res)
    test_time_filters()
    print("\n========================================")
    print("  ALL PARSER & PRICING TESTS PASSED!  ")
    print("========================================\n")
