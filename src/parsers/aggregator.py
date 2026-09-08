"""Aggregator for multi-tool AI usage metrics."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .agy import parse_agy_usage
from .codex import parse_codex_usage


def get_tool_usage(
    tool: str = "all",
    codex_dir: str | Path | None = None,
    agy_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Retrieve usage metrics for a specific tool or aggregated across all tools.

    Args:
        tool: Target tool identifier. Supported values: 'all', 'codex', 'agy', 'antigravity'.
        codex_dir: Optional custom path for Codex directory.
        agy_dir: Optional custom path for Antigravity directory.

    Returns:
        Standardized payload dictionary containing:
            - 'tool': selected tool name ('all', 'codex', or 'antigravity')
            - 'summary': combined odometer totals
            - 'models': breakdown per model
            - 'timeline': daily aggregated metrics list
            - 'sessions': combined sessions sorted by created_at desc
    """
    tool_normalized = (tool or "all").strip().lower()

    if tool_normalized == "codex":
        return parse_codex_usage(codex_dir)

    if tool_normalized in ("agy", "antigravity"):
        return parse_agy_usage(agy_dir)

    if tool_normalized != "all":
        raise ValueError(
            f"Unsupported tool: {tool!r}. Expected one of: 'all', 'codex', 'agy', 'antigravity'."
        )

    # Aggregating all tools
    codex_data = parse_codex_usage(codex_dir)
    agy_data = parse_agy_usage(agy_dir)

    # 1. Merge summaries
    c_sum = codex_data["summary"]
    a_sum = agy_data["summary"]

    total_tokens = c_sum["total_tokens"] + a_sum["total_tokens"]
    uncached_input = c_sum["uncached_input"] + a_sum["uncached_input"]
    cached_input = c_sum["cached_input"] + a_sum["cached_input"]
    total_input = uncached_input + cached_input
    output = c_sum["output"] + a_sum["output"]
    reasoning_output = c_sum["reasoning_output"] + a_sum["reasoning_output"]
    cost_cached = round(c_sum["cost_cached_usd"] + a_sum["cost_cached_usd"], 6)
    cost_uncached = round(c_sum["cost_uncached_usd"] + a_sum["cost_uncached_usd"], 6)
    savings = round(c_sum["savings_usd"] + a_sum["savings_usd"], 6)
    session_count = c_sum["session_count"] + a_sum["session_count"]
    call_count = c_sum["call_count"] + a_sum["call_count"]
    cache_hit_rate = round((cached_input / total_input * 100.0), 2) if total_input > 0 else 0.0

    summary = {
        "total_tokens": total_tokens,
        "uncached_input": uncached_input,
        "cached_input": cached_input,
        "total_input": total_input,
        "output": output,
        "reasoning_output": reasoning_output,
        "cost_cached_usd": cost_cached,
        "cost_uncached_usd": cost_uncached,
        "savings_usd": savings,
        "total_cost_usd": cost_cached,
        "cache_hit_rate": cache_hit_rate,
        "session_count": session_count,
        "call_count": call_count,
    }

    # 2. Combine models list
    models_combined = list(codex_data["models"]) + list(agy_data["models"])
    models_combined.sort(key=lambda m: m["total_tokens"], reverse=True)

    # 3. Merge timeline entries by date YYYY-MM-DD
    timeline_map: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "date": "",
        "uncached_input": 0,
        "cached_input": 0,
        "total_input": 0,
        "output": 0,
        "reasoning_output": 0,
        "total_tokens": 0,
        "call_count": 0,
        "session_count": 0,
        "cost_cached_usd": 0.0,
        "cost_uncached_usd": 0.0,
        "savings_usd": 0.0,
    })

    for entry in list(codex_data["timeline"]) + list(agy_data["timeline"]):
        date_str = entry.get("date") or ""
        if not date_str or date_str == "unknown":
            continue
        day = timeline_map[date_str]
        day["date"] = date_str
        day["uncached_input"] += entry["uncached_input"]
        day["cached_input"] += entry["cached_input"]
        day["total_input"] += entry["total_input"]
        day["output"] += entry["output"]
        day["reasoning_output"] += entry["reasoning_output"]
        day["total_tokens"] += entry["total_tokens"]
        day["call_count"] += entry["call_count"]
        day["session_count"] += entry["session_count"]
        day["cost_cached_usd"] += entry["cost_cached_usd"]
        day["cost_uncached_usd"] += entry["cost_uncached_usd"]
        day["savings_usd"] += entry["savings_usd"]

    timeline_list = []
    for date_key in sorted(timeline_map.keys()):
        d = timeline_map[date_key]
        d["cost_cached_usd"] = round(d["cost_cached_usd"], 6)
        d["cost_uncached_usd"] = round(d["cost_uncached_usd"], 6)
        d["savings_usd"] = round(d["savings_usd"], 6)
        timeline_list.append(d)

    # 4. Combine and sort sessions by created_at descending
    sessions_combined = list(codex_data["sessions"]) + list(agy_data["sessions"])
    sessions_combined.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)

    return {
        "tool": "all",
        "summary": summary,
        "models": models_combined,
        "timeline": timeline_list,
        "sessions": sessions_combined,
    }
