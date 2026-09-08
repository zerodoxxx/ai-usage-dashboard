"""Aggregator for multi-tool AI usage metrics."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from ..pricing import MODEL_PRICING
from .agy import parse_agy_usage
from .codex import parse_codex_usage

_CANONICAL_MODELS: dict[str, str] = {k.lower(): k for k in MODEL_PRICING}


def _canonical_model_name(name: Any) -> str:
    """Resolve a raw model name to its canonical name if known."""
    raw = str(name or "").strip()
    if not raw:
        return ""
    return _CANONICAL_MODELS.get(raw.lower(), raw)



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
    codex_data = codex_data if isinstance(codex_data, dict) else {}
    agy_data = agy_data if isinstance(agy_data, dict) else {}

    # 1. Merge summaries
    c_sum = codex_data.get("summary") if isinstance(codex_data, dict) and isinstance(codex_data.get("summary"), dict) else {}
    a_sum = agy_data.get("summary") if isinstance(agy_data, dict) and isinstance(agy_data.get("summary"), dict) else {}

    total_tokens = int(c_sum.get("total_tokens") or 0) + int(a_sum.get("total_tokens") or 0)
    uncached_input = int(c_sum.get("uncached_input") or 0) + int(a_sum.get("uncached_input") or 0)
    cached_input = int(c_sum.get("cached_input") or 0) + int(a_sum.get("cached_input") or 0)
    total_input = uncached_input + cached_input
    if total_input == 0:
        total_input = int(c_sum.get("total_input") or 0) + int(a_sum.get("total_input") or 0)
    output = int(c_sum.get("output") or 0) + int(a_sum.get("output") or 0)
    reasoning_output = int(c_sum.get("reasoning_output") or 0) + int(a_sum.get("reasoning_output") or 0)
    cost_cached = round(float(c_sum.get("cost_cached_usd") or 0.0) + float(a_sum.get("cost_cached_usd") or 0.0), 6)
    cost_uncached = round(float(c_sum.get("cost_uncached_usd") or 0.0) + float(a_sum.get("cost_uncached_usd") or 0.0), 6)
    savings = round(float(c_sum.get("savings_usd") or 0.0) + float(a_sum.get("savings_usd") or 0.0), 6)
    session_count = int(c_sum.get("session_count") or 0) + int(a_sum.get("session_count") or 0)
    call_count = int(c_sum.get("call_count") or 0) + int(a_sum.get("call_count") or 0)
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

    # 2. Combine and deduplicate models list
    models_map: dict[str, dict[str, Any]] = {}
    for entry in list(codex_data.get("models") or []) + list(agy_data.get("models") or []):
        if not isinstance(entry, dict):
            continue
        canonical_name = _canonical_model_name(entry.get("model"))
        if not canonical_name:
            continue

        tool_val = str(entry.get("tool") or "").strip()
        call_count = int(entry.get("call_count") or 0)
        session_count = int(entry.get("session_count") or 0)
        uncached_input = int(entry.get("uncached_input") or 0)
        cached_input = int(entry.get("cached_input") or 0)
        total_input = int(entry.get("total_input") or 0)
        output = int(entry.get("output") or 0)
        reasoning_output = int(entry.get("reasoning_output") or 0)
        total_tokens = int(entry.get("total_tokens") or 0)
        est_cost_cached_usd = float(entry.get("est_cost_cached_usd") or 0.0)
        est_cost_uncached_usd = float(entry.get("est_cost_uncached_usd") or 0.0)
        est_savings_usd = float(entry.get("est_savings_usd") or 0.0)

        if canonical_name not in models_map:
            models_map[canonical_name] = {
                "model": canonical_name,
                "tool": tool_val,
                "call_count": call_count,
                "session_count": session_count,
                "uncached_input": uncached_input,
                "cached_input": cached_input,
                "total_input": total_input,
                "output": output,
                "reasoning_output": reasoning_output,
                "total_tokens": total_tokens,
                "cache_hit_rate": 0.0,
                "est_cost_cached_usd": est_cost_cached_usd,
                "est_cost_uncached_usd": est_cost_uncached_usd,
                "est_savings_usd": est_savings_usd,
            }
        else:
            m = models_map[canonical_name]
            if not m["tool"]:
                m["tool"] = tool_val
            elif tool_val and m["tool"] != tool_val:
                m["tool"] = "all"
            m["call_count"] += call_count
            m["session_count"] += session_count
            m["uncached_input"] += uncached_input
            m["cached_input"] += cached_input
            m["total_input"] += total_input
            m["output"] += output
            m["reasoning_output"] += reasoning_output
            m["total_tokens"] += total_tokens
            m["est_cost_cached_usd"] += est_cost_cached_usd
            m["est_cost_uncached_usd"] += est_cost_uncached_usd
            m["est_savings_usd"] += est_savings_usd

    models_combined: list[dict[str, Any]] = []
    for m in models_map.values():
        tot_in = int(m.get("total_input") or 0)
        c_in = int(m.get("cached_input") or 0)
        m["cache_hit_rate"] = round((c_in / tot_in * 100.0), 2) if tot_in > 0 else 0.0
        m["est_cost_cached_usd"] = round(float(m.get("est_cost_cached_usd") or 0.0), 6)
        m["est_cost_uncached_usd"] = round(float(m.get("est_cost_uncached_usd") or 0.0), 6)
        m["est_savings_usd"] = round(float(m.get("est_savings_usd") or 0.0), 6)
        if not m.get("tool"):
            m["tool"] = "all"
        models_combined.append(m)

    models_combined.sort(key=lambda m: int(m.get("total_tokens") or 0), reverse=True)

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

    for entry in list(codex_data.get("timeline") or []) + list(agy_data.get("timeline") or []):
        if not isinstance(entry, dict):
            continue
        date_str = str(entry.get("date") or "").strip()
        if not date_str or date_str == "unknown":
            continue
        day = timeline_map[date_str]
        day["date"] = date_str
        day["uncached_input"] += int(entry.get("uncached_input") or 0)
        day["cached_input"] += int(entry.get("cached_input") or 0)
        day["total_input"] += int(entry.get("total_input") or 0)
        day["output"] += int(entry.get("output") or 0)
        day["reasoning_output"] += int(entry.get("reasoning_output") or 0)
        day["total_tokens"] += int(entry.get("total_tokens") or 0)
        day["call_count"] += int(entry.get("call_count") or 0)
        day["session_count"] += int(entry.get("session_count") or 0)
        day["cost_cached_usd"] += float(entry.get("cost_cached_usd") or 0.0)
        day["cost_uncached_usd"] += float(entry.get("cost_uncached_usd") or 0.0)
        day["savings_usd"] += float(entry.get("savings_usd") or 0.0)

    timeline_list = []
    for date_key in sorted(timeline_map.keys()):
        d = timeline_map[date_key]
        d["cost_cached_usd"] = round(float(d.get("cost_cached_usd") or 0.0), 6)
        d["cost_uncached_usd"] = round(float(d.get("cost_uncached_usd") or 0.0), 6)
        d["savings_usd"] = round(float(d.get("savings_usd") or 0.0), 6)
        timeline_list.append(d)

    # 4. Combine and sort sessions by created_at descending
    sessions_combined = list(codex_data.get("sessions") or []) + list(agy_data.get("sessions") or [])
    sessions_combined.sort(
        key=lambda s: str((s.get("created_at") if isinstance(s, dict) else "") or ""),
        reverse=True,
    )

    return {
        "tool": "all",
        "summary": summary,
        "models": models_combined,
        "timeline": timeline_list,
        "sessions": sessions_combined,
    }
