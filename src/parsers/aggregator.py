"""Aggregator for multi-tool AI usage metrics."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..pricing import MODEL_PRICING, calculate_cost
from .agy import parse_agy_usage
from .codex import parse_codex_usage

_CANONICAL_MODELS: dict[str, str] = {k.lower(): k for k in MODEL_PRICING}
_TIME_RANGES = {"all", "month", "30d", "7d", "24h"}
_PARSER_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="usage-parser")


def _normalize_time_range(time_range: str | None) -> str:
    """Normalize and validate a usage time-range identifier."""
    normalized = str(time_range or "all").strip().lower()
    if normalized not in _TIME_RANGES:
        raise ValueError(
            f"Unsupported time range: {time_range!r}. Expected one of: all, month, 30d, 7d, 24h."
        )
    return normalized


def _coerce_timestamp(value: Any, local_tz) -> datetime | None:
    """Parse an epoch or ISO timestamp and normalize it to ``local_tz``."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 1e11:
                seconds /= 1000.0
            return datetime.fromtimestamp(seconds, timezone.utc).astimezone(local_tz)
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    raw = str(value).strip()
    if not raw:
        return None

    # Some local data sources serialize epoch values as strings.
    try:
        seconds = float(raw)
        if seconds > 1e11:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, timezone.utc).astimezone(local_tz)
    except (TypeError, ValueError, OSError, OverflowError):
        pass

    try:
        iso_value = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        parsed = datetime.fromisoformat(iso_value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(local_tz)
    except (TypeError, ValueError, OverflowError):
        return None


def _time_range_cutoff(time_range: str, now: datetime | None = None) -> tuple[datetime | None, datetime]:
    """Return the inclusive lower bound and current time for a range.

    Calendar-month boundaries use the machine's local timezone. Relative ranges
    are measured back from the current instant.
    """
    normalized = _normalize_time_range(time_range)
    if now is None:
        current = datetime.now().astimezone()
    elif now.tzinfo is None:
        current = now.replace(tzinfo=datetime.now().astimezone().tzinfo)
    else:
        current = now

    if normalized == "all":
        return None, current
    if normalized == "month":
        cutoff = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif normalized == "30d":
        cutoff = current - timedelta(days=30)
    elif normalized == "7d":
        cutoff = current - timedelta(days=7)
    else:  # 24h
        cutoff = current - timedelta(hours=24)
    return cutoff, current


def _session_timestamp(session: dict[str, Any], local_tz) -> datetime | None:
    """Get the best available timestamp for a session."""
    for key in ("created_at", "start_time", "end_time"):
        parsed = _coerce_timestamp(session.get(key), local_tz)
        if parsed is not None:
            return parsed
    return None


def _as_int(value: Any) -> int:
    """Convert a metric value to a non-negative integer."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _strip_usage_events(session: dict[str, Any]) -> dict[str, Any]:
    """Return a response-safe session without internal per-call records."""
    public_session = dict(session)
    public_session.pop("usage_events", None)
    return public_session


def _event_metrics(event: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Read normalized input/output metrics from one usage event."""
    input_tokens = _as_int(event.get("input_tokens"))
    cached_input = _as_int(event.get("cached_input_tokens") or event.get("cached_input"))
    uncached_input = _as_int(event.get("uncached_input_tokens") or event.get("uncached_input"))
    if input_tokens == 0:
        input_tokens = uncached_input + cached_input
    cached_input = min(input_tokens, cached_input)
    uncached_input = max(0, input_tokens - cached_input)
    output = _as_int(event.get("output_tokens") or event.get("output"))
    reasoning_output = _as_int(event.get("reasoning_output_tokens") or event.get("reasoning_output"))
    total_tokens = _as_int(event.get("total_tokens")) or (input_tokens + output)
    return uncached_input, cached_input, output, reasoning_output, total_tokens


def _event_timestamp(event: dict[str, Any], local_tz) -> datetime | None:
    """Parse the timestamp attached to a per-call usage event."""
    for key in ("timestamp", "created_at", "start_time"):
        timestamp = _coerce_timestamp(event.get(key), local_tz)
        if timestamp is not None:
            return timestamp
    return None


def _timestamp_in_bounds(
    timestamp: datetime,
    start: datetime,
    end: datetime,
    include_end: bool,
) -> bool:
    """Check a timestamp against a half-open or closed interval."""
    if timestamp < start:
        return False
    return timestamp <= end if include_end else timestamp < end


def _slice_session(
    session: dict[str, Any],
    start: datetime,
    end: datetime,
    include_end: bool,
) -> dict[str, Any] | None:
    """Slice a session to usage events inside a time interval."""
    local_tz = start.tzinfo
    raw_events = session.get("usage_events")
    has_events = isinstance(raw_events, list) and bool(raw_events)

    if not has_events:
        session_timestamp = _session_timestamp(session, local_tz)
        if session_timestamp is None or not _timestamp_in_bounds(session_timestamp, start, end, include_end):
            return None
        sliced = _strip_usage_events(session)
        sliced["activity_at"] = session_timestamp.isoformat()
        return sliced

    selected_events: list[tuple[dict[str, Any], datetime]] = []
    timestamped_event_count = 0
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        event_timestamp = _event_timestamp(event, local_tz)
        if event_timestamp is None:
            continue
        timestamped_event_count += 1
        if _timestamp_in_bounds(event_timestamp, start, end, include_end):
            selected_events.append((event, event_timestamp))

    # If a source gave us events but no usable event timestamps, retain the
    # session-level fallback rather than silently dropping otherwise valid data.
    if not selected_events:
        if timestamped_event_count:
            return None
        session_timestamp = _session_timestamp(session, local_tz)
        if session_timestamp is None or not _timestamp_in_bounds(session_timestamp, start, end, include_end):
            return None
        sliced = _strip_usage_events(session)
        sliced["activity_at"] = session_timestamp.isoformat()
        return sliced

    uncached_input = 0
    cached_input = 0
    output = 0
    reasoning_output = 0
    total_tokens = 0
    cost_cached = 0.0
    cost_uncached = 0.0
    savings = 0.0
    for event, _event_time in selected_events:
        event_uncached, event_cached, event_output, event_reasoning, event_total = _event_metrics(event)
        uncached_input += event_uncached
        cached_input += event_cached
        output += event_output
        reasoning_output += event_reasoning
        total_tokens += event_total
        event_cost = calculate_cost(session.get("model"), event_uncached, event_cached, event_output)
        cost_cached += event_cost["cost_cached_usd"]
        cost_uncached += event_cost["cost_uncached_usd"]
        savings += event_cost["savings_usd"]

    sliced = dict(session)
    sliced["usage_events"] = [event for event, _event_time in selected_events]
    sliced.update({
        "call_count": len(selected_events),
        "uncached_input": uncached_input,
        "cached_input": cached_input,
        "total_input": uncached_input + cached_input,
        "output": output,
        "reasoning_output": reasoning_output,
        "total_tokens": total_tokens,
        "cache_hit_rate": round((cached_input / (uncached_input + cached_input) * 100.0), 2)
        if uncached_input + cached_input > 0 else 0.0,
        "cost_cached_usd": round(cost_cached, 6),
        "cost_uncached_usd": round(cost_uncached, 6),
        "savings_usd": round(savings, 6),
        "activity_at": max(timestamp for _event, timestamp in selected_events).isoformat(),
    })
    return sliced


def _filter_sessions_between(
    sessions: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    include_end: bool = True,
) -> list[dict[str, Any]]:
    """Return session aggregates sliced to a specific interval."""
    filtered: list[dict[str, Any]] = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        sliced = _slice_session(session, start, end, include_end)
        if sliced is not None:
            filtered.append(sliced)
    return filtered


def _filter_sessions(
    sessions: list[dict[str, Any]],
    time_range: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Filter sessions by per-call activity when available."""
    cutoff, current = _time_range_cutoff(time_range, now)
    if cutoff is None:
        return list(sessions)

    return _filter_sessions_between(sessions, cutoff, current)


def _session_date(session: dict[str, Any], local_tz) -> str:
    """Return a YYYY-MM-DD timeline key for a session, if available."""
    for key in ("activity_at", "created_at", "start_time", "end_time"):
        raw = session.get(key)
        parsed = _coerce_timestamp(raw, local_tz)
        if parsed is not None:
            return parsed.date().isoformat()
        if isinstance(raw, str):
            value = raw.strip()
            if len(value) >= 10 and value[4] == "-" and value[7] == "-":
                return value[:10]
    return "unknown"


def _build_usage_data(sessions: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    """Rebuild dashboard aggregates from a session subset."""
    sessions_combined = [s for s in sessions if isinstance(s, dict)]
    sessions_combined.sort(
        key=lambda s: str(
            s.get("activity_at") or s.get("created_at") or s.get("start_time") or ""
        ),
        reverse=True,
    )

    models_map: dict[str, dict[str, Any]] = {}
    for session in sessions_combined:
        raw_model = str(session.get("model") or "unknown")
        model_key = _canonical_model_name(raw_model) if tool == "all" else raw_model
        tool_value = str(session.get("tool") or (tool if tool != "all" else "")).strip()

        uncached_input = int(session.get("uncached_input") or 0)
        cached_input = int(session.get("cached_input") or 0)
        total_input = int(session.get("total_input") or (uncached_input + cached_input))
        output = int(session.get("output") or 0)
        reasoning_output = int(session.get("reasoning_output") or 0)
        total_tokens = int(session.get("total_tokens") or 0)
        call_count = int(session.get("call_count") or 0)
        cost_cached = float(session.get("cost_cached_usd") or 0.0)
        cost_uncached = float(session.get("cost_uncached_usd") or 0.0)
        savings = float(session.get("savings_usd") or 0.0)

        if model_key not in models_map:
            models_map[model_key] = {
                "model": model_key,
                "tool": tool_value,
                "call_count": call_count,
                "session_count": 1,
                "uncached_input": uncached_input,
                "cached_input": cached_input,
                "total_input": total_input,
                "output": output,
                "reasoning_output": reasoning_output,
                "total_tokens": total_tokens,
                "cache_hit_rate": 0.0,
                "est_cost_cached_usd": cost_cached,
                "est_cost_uncached_usd": cost_uncached,
                "est_savings_usd": savings,
            }
        else:
            model = models_map[model_key]
            if not model["tool"]:
                model["tool"] = tool_value
            elif tool_value and model["tool"] != tool_value:
                model["tool"] = "all"
            model["call_count"] += call_count
            model["session_count"] += 1
            model["uncached_input"] += uncached_input
            model["cached_input"] += cached_input
            model["total_input"] += total_input
            model["output"] += output
            model["reasoning_output"] += reasoning_output
            model["total_tokens"] += total_tokens
            model["est_cost_cached_usd"] += cost_cached
            model["est_cost_uncached_usd"] += cost_uncached
            model["est_savings_usd"] += savings

    models_list: list[dict[str, Any]] = []
    local_tz = datetime.now().astimezone().tzinfo
    for model in models_map.values():
        total_input = int(model.get("total_input") or 0)
        cached_input = int(model.get("cached_input") or 0)
        model["cache_hit_rate"] = round((cached_input / total_input * 100.0), 2) if total_input > 0 else 0.0
        model["est_cost_cached_usd"] = round(float(model.get("est_cost_cached_usd") or 0.0), 6)
        model["est_cost_uncached_usd"] = round(float(model.get("est_cost_uncached_usd") or 0.0), 6)
        model["est_savings_usd"] = round(float(model.get("est_savings_usd") or 0.0), 6)
        if not model.get("tool"):
            model["tool"] = tool
        models_list.append(model)
    models_list.sort(key=lambda model: int(model.get("total_tokens") or 0), reverse=True)

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

    timeline_session_ids: dict[str, set[str]] = defaultdict(set)
    for session_index, session in enumerate(sessions_combined):
        session_key = str(session.get("id") or f"session-{session_index}")
        event_rows: list[tuple[dict[str, Any], datetime]] = []
        raw_events = session.get("usage_events")
        if isinstance(raw_events, list):
            for event in raw_events:
                if not isinstance(event, dict):
                    continue
                event_time = _event_timestamp(event, local_tz)
                if event_time is not None:
                    event_rows.append((event, event_time))

        if event_rows:
            for event, event_time in event_rows:
                date_key = event_time.date().isoformat()
                day = timeline_map[date_key]
                day["date"] = date_key
                event_uncached, event_cached, event_output, event_reasoning, event_total = _event_metrics(event)
                event_cost = calculate_cost(session.get("model"), event_uncached, event_cached, event_output)
                day["uncached_input"] += event_uncached
                day["cached_input"] += event_cached
                day["total_input"] += event_uncached + event_cached
                day["output"] += event_output
                day["reasoning_output"] += event_reasoning
                day["total_tokens"] += event_total
                day["call_count"] += 1
                day["cost_cached_usd"] += event_cost["cost_cached_usd"]
                day["cost_uncached_usd"] += event_cost["cost_uncached_usd"]
                day["savings_usd"] += event_cost["savings_usd"]
                timeline_session_ids[date_key].add(session_key)
            continue

        date_key = _session_date(session, local_tz)
        if date_key == "unknown":
            continue
        day = timeline_map[date_key]
        day["date"] = date_key
        day["uncached_input"] += _as_int(session.get("uncached_input"))
        day["cached_input"] += _as_int(session.get("cached_input"))
        day["total_input"] += _as_int(session.get("total_input"))
        day["output"] += _as_int(session.get("output"))
        day["reasoning_output"] += _as_int(session.get("reasoning_output"))
        day["total_tokens"] += _as_int(session.get("total_tokens"))
        day["call_count"] += _as_int(session.get("call_count"))
        day["cost_cached_usd"] += float(session.get("cost_cached_usd") or 0.0)
        day["cost_uncached_usd"] += float(session.get("cost_uncached_usd") or 0.0)
        day["savings_usd"] += float(session.get("savings_usd") or 0.0)
        timeline_session_ids[date_key].add(session_key)

    timeline_list: list[dict[str, Any]] = []
    for date_key in sorted(timeline_map.keys()):
        day = timeline_map[date_key]
        day["session_count"] = len(timeline_session_ids.get(date_key, set()))
        day["cost_cached_usd"] = round(float(day.get("cost_cached_usd") or 0.0), 6)
        day["cost_uncached_usd"] = round(float(day.get("cost_uncached_usd") or 0.0), 6)
        day["savings_usd"] = round(float(day.get("savings_usd") or 0.0), 6)
        timeline_list.append(day)

    uncached_input = sum(int(s.get("uncached_input") or 0) for s in sessions_combined)
    cached_input = sum(int(s.get("cached_input") or 0) for s in sessions_combined)
    total_input = uncached_input + cached_input
    output = sum(int(s.get("output") or 0) for s in sessions_combined)
    reasoning_output = sum(int(s.get("reasoning_output") or 0) for s in sessions_combined)
    total_tokens = sum(int(s.get("total_tokens") or 0) for s in sessions_combined)
    cost_cached = round(sum(float(s.get("cost_cached_usd") or 0.0) for s in sessions_combined), 6)
    cost_uncached = round(sum(float(s.get("cost_uncached_usd") or 0.0) for s in sessions_combined), 6)
    savings = round(sum(float(s.get("savings_usd") or 0.0) for s in sessions_combined), 6)

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
        "cache_hit_rate": round((cached_input / total_input * 100.0), 2) if total_input > 0 else 0.0,
        "session_count": len(sessions_combined),
        "call_count": sum(int(s.get("call_count") or 0) for s in sessions_combined),
    }

    return {
        "tool": tool,
        "summary": summary,
        "models": models_list,
        "timeline": timeline_list,
        "sessions": [_strip_usage_events(session) for session in sessions_combined],
    }


def _previous_time_bounds(
    time_range: str,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return the immediately preceding equivalent comparison window."""
    cutoff, current = _time_range_cutoff(time_range, now)
    if cutoff is None:
        return None, None
    if _normalize_time_range(time_range) == "month":
        previous_month_end = cutoff - timedelta(days=1)
        previous_month_start = previous_month_end.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return previous_month_start, cutoff

    window_length = current - cutoff
    return cutoff - window_length, cutoff


def _percentage_change(current: float, previous: float) -> float | None:
    """Calculate a percentage delta, leaving zero baselines undefined."""
    if previous == 0:
        return None
    return round((current - previous) / previous * 100.0, 2)


def _build_comparison(
    current_summary: dict[str, Any],
    previous_summary: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Build comparable current/previous period metrics."""
    current = {
        "total_tokens": _as_int(current_summary.get("total_tokens")),
        "cost_cached_usd": round(float(current_summary.get("cost_cached_usd") or 0.0), 6),
        "call_count": _as_int(current_summary.get("call_count")),
    }
    previous = {
        "total_tokens": _as_int(previous_summary.get("total_tokens")),
        "cost_cached_usd": round(float(previous_summary.get("cost_cached_usd") or 0.0), 6),
        "call_count": _as_int(previous_summary.get("call_count")),
    }
    return {
        "label": label,
        "current": current,
        "previous": previous,
        "delta": {
            "total_tokens": current["total_tokens"] - previous["total_tokens"],
            "cost_cached_usd": round(current["cost_cached_usd"] - previous["cost_cached_usd"], 6),
            "call_count": current["call_count"] - previous["call_count"],
        },
        "change_pct": {
            "total_tokens": _percentage_change(current["total_tokens"], previous["total_tokens"]),
            "cost_cached_usd": _percentage_change(current["cost_cached_usd"], previous["cost_cached_usd"]),
            "call_count": _percentage_change(current["call_count"], previous["call_count"]),
        },
    }


def _build_analytics(
    data: dict[str, Any],
    time_range: str,
    source_sessions: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build secondary analytics from the already-filtered dashboard data."""
    normalized = _normalize_time_range(time_range)
    cutoff, current = _time_range_cutoff(normalized, now)
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    timeline = [t for t in list(data.get("timeline") or []) if isinstance(t, dict)]
    sessions = [s for s in list(data.get("sessions") or []) if isinstance(s, dict)]

    top_sessions: list[dict[str, Any]] = []
    for session in sorted(
        sessions,
        key=lambda item: float(item.get("cost_cached_usd") or 0.0),
        reverse=True,
    )[:5]:
        top_sessions.append({
            "id": str(session.get("id") or ""),
            "title": str(session.get("title") or "Untitled Session"),
            "tool": str(session.get("tool") or ""),
            "model": str(session.get("model") or "unknown"),
            "total_tokens": _as_int(session.get("total_tokens")),
            "call_count": _as_int(session.get("call_count")),
            "cost_cached_usd": round(float(session.get("cost_cached_usd") or 0.0), 6),
            "activity_at": str(
                session.get("activity_at")
                or session.get("created_at")
                or session.get("start_time")
                or ""
            ),
        })

    peak_day = None
    if timeline:
        peak = max(timeline, key=lambda item: float(item.get("cost_cached_usd") or 0.0))
        peak_day = {
            "date": str(peak.get("date") or ""),
            "cost_cached_usd": round(float(peak.get("cost_cached_usd") or 0.0), 6),
            "total_tokens": _as_int(peak.get("total_tokens")),
            "call_count": _as_int(peak.get("call_count")),
        }

    daily_calls = [
        {
            "date": str(day.get("date") or ""),
            "call_count": _as_int(day.get("call_count")),
        }
        for day in timeline
    ]

    current_cost = round(float(summary.get("cost_cached_usd") or 0.0), 6)
    if normalized == "all":
        projection_start = current - timedelta(days=30)
        projection_sessions = _filter_sessions_between(source_sessions, projection_start, current)
        projection_cost = round(
            sum(float(session.get("cost_cached_usd") or 0.0) for session in projection_sessions),
            6,
        )
        monthly_projection = projection_cost
        projection_basis = "last_30_days"
    elif normalized == "month":
        elapsed_days = max((current - cutoff).total_seconds() / 86400.0, 1.0)
        days_in_month = monthrange(current.year, current.month)[1]
        monthly_projection = current_cost / elapsed_days * days_in_month
        projection_basis = "current_month_run_rate"
    else:
        period_days = {"30d": 30.0, "7d": 7.0, "24h": 1.0}[normalized]
        monthly_projection = current_cost / period_days * 30.0
        projection_basis = f"{normalized}_run_rate"

    comparison = None
    previous_start, previous_end = _previous_time_bounds(normalized, now)
    if previous_start is not None and previous_end is not None:
        previous_sessions = _filter_sessions_between(
            source_sessions,
            previous_start,
            previous_end,
            include_end=False,
        )
        previous_data = _build_usage_data(previous_sessions, str(data.get("tool") or "all"))
        labels = {
            "month": "previous calendar month",
            "30d": "previous 30 days",
            "7d": "previous 7 days",
            "24h": "previous 24 hours",
        }
        comparison = _build_comparison(
            summary,
            previous_data["summary"],
            labels[normalized],
        )

    return {
        "window_start": cutoff.isoformat() if cutoff is not None else None,
        "window_end": current.isoformat(),
        "active_days": len(timeline),
        "daily_calls": daily_calls,
        "avg_tokens_per_session": round(
            _as_int(summary.get("total_tokens")) / len(sessions), 2
        ) if sessions else 0.0,
        "avg_cost_per_session_usd": round(
            current_cost / len(sessions), 6
        ) if sessions else 0.0,
        "peak_day": peak_day,
        "top_sessions": top_sessions,
        "monthly_projection_usd": round(monthly_projection, 6),
        "projection_basis": projection_basis,
        "comparison": comparison,
    }


def _filter_usage_data(
    data: dict[str, Any],
    time_range: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply a time range and rebuild all derived metrics."""
    normalized = _normalize_time_range(time_range)
    source_sessions = [s for s in list(data.get("sessions") or []) if isinstance(s, dict)]
    if normalized == "all":
        result = _build_usage_data(source_sessions, str(data.get("tool") or "all"))
        result["time_range"] = normalized
        result["analytics"] = _build_analytics(result, normalized, source_sessions, now)
        return result

    filtered_sessions = _filter_sessions(source_sessions, normalized, now)
    result = _build_usage_data(filtered_sessions, str(data.get("tool") or "all"))
    result["time_range"] = normalized
    result["analytics"] = _build_analytics(result, normalized, source_sessions, now)
    return result


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
    time_range: str = "all",
) -> dict[str, Any]:
    """Retrieve usage metrics for a specific tool or aggregated across all tools.

    Args:
        tool: Target tool identifier. Supported values: 'all', 'codex', 'agy', 'antigravity'.
        codex_dir: Optional custom path for Codex directory.
        agy_dir: Optional custom path for Antigravity directory.
        time_range: Usage window. Supported values: 'all', 'month', '30d', '7d', '24h'.

    Returns:
        Standardized payload dictionary containing:
            - 'tool': selected tool name ('all', 'codex', or 'antigravity')
            - 'summary': combined odometer totals
            - 'models': breakdown per model
            - 'timeline': daily aggregated metrics list
            - 'sessions': combined sessions sorted by created_at desc
            - 'time_range': normalized selected usage window
    """
    tool_normalized = (tool or "all").strip().lower()
    time_range_normalized = _normalize_time_range(time_range)

    if tool_normalized == "codex":
        return _filter_usage_data(parse_codex_usage(codex_dir), time_range_normalized)

    if tool_normalized in ("agy", "antigravity"):
        return _filter_usage_data(parse_agy_usage(agy_dir), time_range_normalized)

    if tool_normalized != "all":
        raise ValueError(
            f"Unsupported tool: {tool!r}. Expected one of: 'all', 'codex', 'agy', 'antigravity'."
        )

    # Aggregating all tools
    codex_future = _PARSER_EXECUTOR.submit(parse_codex_usage, codex_dir)
    agy_future = _PARSER_EXECUTOR.submit(parse_agy_usage, agy_dir)
    codex_data = codex_future.result()
    agy_data = agy_future.result()
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

    return _filter_usage_data({
        "tool": "all",
        "summary": summary,
        "models": models_combined,
        "timeline": timeline_list,
        "sessions": sessions_combined,
    }, time_range_normalized)
