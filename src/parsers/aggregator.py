"""Aggregator for multi-tool AI usage metrics."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Iterator

from ..pricing import MODEL_PRICING, PRICING_CATALOG, calculate_cost_strict
from .agy import AntigravitySource
from .claude import ClaudeCodeSource
from .codex import CodexSource
from .contracts import CostEstimate, UsageSession
from .source_registry import SOURCE_REGISTRY, SourceRegistry, normalize_source_key

_CANONICAL_MODELS: dict[str, str] = {k.lower(): k for k in MODEL_PRICING}
_TIME_RANGES = {"all", "month", "30d", "7d", "24h", "custom"}
_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_PARSER_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="usage-parser")
DEFAULT_SOURCE_REGISTRY = SOURCE_REGISTRY
for _builtin_source in (CodexSource(), AntigravitySource(), ClaudeCodeSource()):
    if DEFAULT_SOURCE_REGISTRY.lookup(_builtin_source.key) is None:
        DEFAULT_SOURCE_REGISTRY.register(_builtin_source)


def _normalize_time_range(time_range: str | None) -> str:
    """Normalize and validate a usage time-range identifier."""
    normalized = str(time_range or "all").strip().lower()
    if normalized not in _TIME_RANGES:
        raise ValueError(
            f"Unsupported time range: {time_range!r}. Expected one of: all, month, 30d, 7d, 24h, custom."
        )
    return normalized


def _parse_custom_range(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    """Parse inclusive UTC YYYY-MM-DD bounds for ``time_range=custom``."""
    if not start or not end:
        raise ValueError(
            "Custom time range requires both 'start' and 'end' query parameters (YYYY-MM-DD)."
        )
    try:
        start_date = datetime.strptime(str(start).strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid custom start date: {start!r}. Expected format YYYY-MM-DD."
        )
    try:
        end_date = datetime.strptime(str(end).strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid custom end date: {end!r}. Expected format YYYY-MM-DD."
        )
    start_dt = start_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end_dt = end_date.replace(
        hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
    )
    if start_dt > end_dt:
        raise ValueError(
            f"Invalid custom range: start date {start!r} is after end date {end!r}."
        )
    return start_dt, end_dt


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


def _time_range_cutoff(
    time_range: str,
    now: datetime | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[datetime | None, datetime]:
    """Return the inclusive lower bound and current time for a range.

    Calendar-month boundaries use the machine's local timezone. Relative ranges
    are measured back from the current instant. Custom ranges use inclusive UTC
    day bounds derived from ``start``/``end`` (YYYY-MM-DD).
    """
    normalized = _normalize_time_range(time_range)
    if normalized == "custom":
        custom_start, custom_end = _parse_custom_range(start, end)
        return custom_start, custom_end
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


def _event_cost(
    session: Mapping[str, Any],
    event: Mapping[str, Any],
    uncached_input: int,
    cached_input: int,
    output: int,
) -> dict[str, float]:
    """Use reported/estimated event cost, or price tokens without fallback."""
    if event.get("reported_cost_usd") is not None:
        actual = float(event.get("reported_cost_usd") or 0.0)
        baseline = float(event.get("cost_uncached_usd") or actual)
        return {
            "cost_cached_usd": actual,
            "cost_uncached_usd": baseline,
            "savings_usd": float(
                event.get("savings_usd")
                if event.get("savings_usd") is not None
                else max(0.0, baseline - actual)
            ),
        }
    event_model = str(event.get("model") or session.get("model") or "")
    event_provider = str(session.get("provider") or session.get("tool") or "") or None
    if event_model.casefold().startswith("deepseek"):
        event_provider = "deepseek"
    event_ts = event.get("timestamp") or session.get("created_at") or session.get("start_time")
    result = calculate_cost_strict(
        event_model,
        uncached_input,
        cached_input,
        output,
        provider=event_provider,
        cache_write=_as_int(
            event.get("cache_write_tokens")
            or event.get("cache_creation_tokens")
        ),
        timestamp=event_ts,
    )
    return {
        "cost_cached_usd": float(result.get("cost_cached_usd") or 0.0),
        "cost_uncached_usd": float(result.get("cost_uncached_usd") or 0.0),
        "savings_usd": float(result.get("savings_usd") or 0.0),
    }


def _refresh_estimated_session_cost(session: UsageSession) -> None:
    """Reprice estimated data against the current catalog.

    Parser snapshots can outlive a pricing refresh. Repricing here keeps the
    dashboard current while preserving provider-reported costs verbatim.
    """
    if session.cost is not None and (
        session.cost.source == "reported" or session.cost.reported_usd is not None
    ):
        return

    provider = session.provider or session.tool
    if str(session.model or "").casefold().startswith("deepseek"):
        provider = "deepseek"
    usage = session.usage
    resolved = calculate_cost_strict(
        session.model,
        usage.uncached_input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        provider=provider,
        cache_write=usage.cache_write_tokens,
        timestamp=session.created_at or session.start_time or session.activity_at,
    )
    if resolved.get("status") == "known":
        session.cost = CostEstimate(
            cached_usd=resolved.get("cost_cached_usd") or 0.0,
            uncached_usd=resolved.get("cost_uncached_usd") or 0.0,
            savings_usd=resolved.get("savings_usd") or 0.0,
            source="estimated",
        )
    else:
        # Keep unknown models explicitly unpriced instead of retaining an old
        # legacy fallback amount (historically Luna).
        session.cost = None

    for event in session.events:
        if event.cost is not None and (
            event.cost.source == "reported" or event.cost.reported_usd is not None
        ):
            continue
        event_model = event.model or session.model
        event_usage = event.usage
        event_provider = "deepseek" if str(event_model or "").casefold().startswith("deepseek") else provider
        event_result = calculate_cost_strict(
            event_model,
            event_usage.uncached_input_tokens,
            event_usage.cached_input_tokens,
            event_usage.output_tokens,
            provider=event_provider,
            cache_write=event_usage.cache_write_tokens,
            timestamp=event.timestamp or session.created_at,
        )
        if event_result.get("status") == "known":
            event.cost = CostEstimate(
                cached_usd=event_result.get("cost_cached_usd") or 0.0,
                uncached_usd=event_result.get("cost_uncached_usd") or 0.0,
                savings_usd=event_result.get("savings_usd") or 0.0,
                source="estimated",
            )
        else:
            event.cost = None


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
        event_cost = _event_cost(
            session,
            event,
            event_uncached,
            event_cached,
            event_output,
        )
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
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """Filter sessions by per-call activity when available."""
    cutoff, current = _time_range_cutoff(time_range, now, start, end)
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


def _session_activity_time(session: dict[str, Any], local_tz) -> datetime | None:
    """Best timestamp for bucketing a session when it has no usage events."""
    for key in ("activity_at", "created_at", "start_time", "end_time"):
        parsed = _coerce_timestamp(session.get(key), local_tz)
        if parsed is not None:
            return parsed
    return None


def _blank_day_row(date_key: str = "") -> dict[str, Any]:
    """Zeroed daily timeline row used for quiet days in a selected window."""
    return {
        "date": date_key,
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
    }


def _blank_hour_row(hour: int) -> dict[str, Any]:
    """Zeroed local-hour bucket; the hourly chart always receives 24 rows."""
    return {
        "hour": hour,
        "label": f"{hour:02d}:00",
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
    }


def _blank_weekday_hour_row(weekday: int, hour: int) -> dict[str, Any]:
    """Zeroed weekday×hour cell (Monday=0) for the activity heatmap."""
    return {
        "weekday": weekday,
        "weekday_label": _WEEKDAY_LABELS[weekday],
        "hour": hour,
        "total_tokens": 0,
        "call_count": 0,
        "cost_cached_usd": 0.0,
        "session_count": 0,
    }


def _round_cost_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Round monetary fields on an aggregate row to the dashboard precision."""
    for key in ("cost_cached_usd", "cost_uncached_usd", "savings_usd"):
        if key in row:
            row[key] = round(float(row.get(key) or 0.0), 6)
    return row


def _add_token_metrics(
    row: dict[str, Any],
    uncached_input: int,
    cached_input: int,
    output: int,
    reasoning_output: int,
    total_tokens: int,
    call_count: int,
    cost: Mapping[str, float],
) -> None:
    """Add one usage point into a daily, hourly, or weekday-hour bucket."""
    row["total_tokens"] = int(row.get("total_tokens") or 0) + total_tokens
    row["call_count"] = int(row.get("call_count") or 0) + call_count
    row["cost_cached_usd"] = float(row.get("cost_cached_usd") or 0.0) + float(
        cost.get("cost_cached_usd") or 0.0
    )
    if "uncached_input" in row:
        row["uncached_input"] = int(row.get("uncached_input") or 0) + uncached_input
        row["cached_input"] = int(row.get("cached_input") or 0) + cached_input
        row["total_input"] = int(row.get("total_input") or 0) + uncached_input + cached_input
        row["output"] = int(row.get("output") or 0) + output
        row["reasoning_output"] = int(row.get("reasoning_output") or 0) + reasoning_output
        row["cost_uncached_usd"] = float(row.get("cost_uncached_usd") or 0.0) + float(
            cost.get("cost_uncached_usd") or 0.0
        )
        row["savings_usd"] = float(row.get("savings_usd") or 0.0) + float(cost.get("savings_usd") or 0.0)


def _local_calendar_date(value: datetime, local_tz) -> date:
    """Normalize a timestamp to the aggregator's local calendar date."""
    if value.tzinfo is None:
        localized = value.replace(tzinfo=local_tz)
    else:
        localized = value.astimezone(local_tz)
    return localized.date()


def _iter_dates(start_day: date, end_day: date) -> Iterator[date]:
    """Yield inclusive calendar dates from ``start_day`` through ``end_day``."""
    cursor = start_day
    while cursor <= end_day:
        yield cursor
        cursor += timedelta(days=1)


def _timeline_span(
    timeline_map: Mapping[str, Any],
    window_start: datetime | None,
    window_end: datetime | None,
    local_tz,
) -> tuple[date, date] | None:
    """Choose the inclusive local date range that the daily timeline should cover."""
    activity_dates: list[date] = []
    for key in timeline_map:
        try:
            activity_dates.append(date.fromisoformat(str(key)))
        except ValueError:
            continue

    if window_start is not None and window_end is not None:
        start_day = _local_calendar_date(window_start, local_tz)
        end_day = _local_calendar_date(window_end, local_tz)
        if start_day > end_day:
            start_day, end_day = end_day, start_day
        return start_day, end_day

    if not activity_dates:
        return None
    start_day = min(activity_dates)
    end_day = (
        _local_calendar_date(window_end, local_tz) if window_end is not None else max(activity_dates)
    )
    if start_day > end_day:
        end_day = max(activity_dates)
    return start_day, end_day


def _day_has_usage(day: Mapping[str, Any]) -> bool:
    """True when a timeline day contains tokens, calls, or spend."""
    return (
        _as_int(day.get("total_tokens")) > 0
        or _as_int(day.get("call_count")) > 0
        or float(day.get("cost_cached_usd") or 0.0) > 0.0
    )


def _build_usage_data(
    sessions: list[dict[str, Any]],
    tool: str,
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, Any]:
    """Rebuild dashboard aggregates from a session subset."""
    sessions_combined = [s for s in sessions if isinstance(s, dict)]
    sessions_combined.sort(
        key=lambda s: str(
            s.get("activity_at") or s.get("created_at") or s.get("start_time") or ""
        ),
        reverse=True,
    )

    models_map: dict[str, dict[str, Any]] = {}
    # Track pricing provenance per aggregated model so rows with no catalog
    # rate can be surfaced as unpriced instead of silent $0.
    model_statuses: dict[str, set[str]] = defaultdict(set)
    model_providers: dict[str, set[str]] = defaultdict(set)
    # Token provenance per aggregated model: "estimated" when every
    # contributing session carries estimated token counts (AGY heuristic),
    # "reported" when all counts are provider-reported (Codex/Claude),
    # "mixed" when a canonical model merges both (tool=all view).
    model_token_sources: dict[str, set[str]] = defaultdict(set)
    for session in sessions_combined:
        raw_model = str(session.get("model") or "unknown")
        model_key = _canonical_model_name(raw_model) if tool == "all" else raw_model
        tool_value = str(session.get("tool") or (tool if tool != "all" else "")).strip()
        provider_hint = (
            str(session.get("provider") or tool_value or "").strip() or None
        )
        status = str(session.get("pricing_status") or "").strip()
        resolved = PRICING_CATALOG.resolve(raw_model, provider_hint)
        if status not in ("reported", "estimated", "known", "unpriced", "unknown", "ambiguous"):
            status = resolved.status
        canonical_model = resolved.canonical_model or model_key
        model_statuses[model_key].add(status)
        session_token_source = str(session.get("token_source") or ("estimated" if session.get("estimated") else "reported"))
        model_token_sources[model_key].add(session_token_source)
        if tool_value:
            model_providers[model_key].add(tool_value)
        elif provider_hint:
            model_providers[model_key].add(provider_hint)

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
                "canonical_model": canonical_model,
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
        # Flag models with no catalog rate. Never invent fallback rates:
        # unpriced rows keep cost 0 and carry provider/model identity.
        statuses = model_statuses.get(str(model.get("model")), set())
        priced = any(s in ("reported", "estimated", "known") for s in statuses)
        if not priced and not statuses:
            resolved = PRICING_CATALOG.resolve(
                str(model.get("model")), str(model.get("tool") or "") or None
            )
            statuses = {resolved.status}
            priced = resolved.priced
        unpriced = not priced
        if unpriced:
            model["est_cost_cached_usd"] = 0.0
            model["est_cost_uncached_usd"] = 0.0
            model["est_savings_usd"] = 0.0
        if "reported" in statuses:
            pricing_status = "reported" if priced else "unknown"
        elif "estimated" in statuses or "known" in statuses:
            pricing_status = "known"
        elif "unpriced" in statuses:
            pricing_status = "unpriced"
        elif "ambiguous" in statuses:
            pricing_status = "ambiguous"
        else:
            pricing_status = "unknown"
        model["provider"] = str(model.get("tool") or "")
        model["pricing_status"] = pricing_status
        model["priced"] = priced
        model["unpriced"] = unpriced
        token_sources = model_token_sources.get(str(model.get("model")), set())
        if token_sources == {"estimated"}:
            model["token_source"] = "estimated"
            model["estimated"] = True
        elif token_sources == {"reported"}:
            model["token_source"] = "reported"
            model["estimated"] = False
        elif token_sources:
            model["token_source"] = "mixed"
            model["estimated"] = False
        else:
            model["token_source"] = "reported"
            model["estimated"] = False
        models_list.append(model)
    models_list.sort(key=lambda model: int(model.get("total_tokens") or 0), reverse=True)

    timeline_map: dict[str, dict[str, Any]] = defaultdict(_blank_day_row)
    timeline_session_ids: dict[str, set[str]] = defaultdict(set)
    hourly_map = {hour: _blank_hour_row(hour) for hour in range(24)}
    hourly_session_ids: dict[int, set[str]] = defaultdict(set)
    weekday_hour_map = {
        (weekday, hour): _blank_weekday_hour_row(weekday, hour)
        for weekday in range(7)
        for hour in range(24)
    }
    weekday_session_ids: dict[tuple[int, int], set[str]] = defaultdict(set)
    reference_now = window_end if window_end is not None else datetime.now().astimezone()
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=local_tz)

    def record_point(
        when: datetime,
        session_key: str,
        uncached_input: int,
        cached_input: int,
        output: int,
        reasoning_output: int,
        total_tokens: int,
        call_count: int,
        cost: Mapping[str, float],
    ) -> None:
        if not _is_plausible_usage_time(when, reference_now):
            return
        date_key = when.date().isoformat()
        day = timeline_map[date_key]
        day["date"] = date_key
        _add_token_metrics(
            day,
            uncached_input,
            cached_input,
            output,
            reasoning_output,
            total_tokens,
            call_count,
            cost,
        )
        timeline_session_ids[date_key].add(session_key)

        hour = when.hour
        weekday = when.weekday()
        _add_token_metrics(
            hourly_map[hour],
            uncached_input,
            cached_input,
            output,
            reasoning_output,
            total_tokens,
            call_count,
            cost,
        )
        hourly_session_ids[hour].add(session_key)
        weekday_key = (weekday, hour)
        _add_token_metrics(
            weekday_hour_map[weekday_key],
            uncached_input,
            cached_input,
            output,
            reasoning_output,
            total_tokens,
            call_count,
            cost,
        )
        weekday_session_ids[weekday_key].add(session_key)

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
                event_uncached, event_cached, event_output, event_reasoning, event_total = _event_metrics(event)
                event_cost = _event_cost(
                    session,
                    event,
                    event_uncached,
                    event_cached,
                    event_output,
                )
                record_point(
                    event_time,
                    session_key,
                    event_uncached,
                    event_cached,
                    event_output,
                    event_reasoning,
                    event_total,
                    1,
                    event_cost,
                )
            continue

        activity_time = _session_activity_time(session, local_tz)
        session_cost = {
            "cost_cached_usd": float(session.get("cost_cached_usd") or 0.0),
            "cost_uncached_usd": float(session.get("cost_uncached_usd") or 0.0),
            "savings_usd": float(session.get("savings_usd") or 0.0),
        }
        if activity_time is not None:
            record_point(
                activity_time,
                session_key,
                _as_int(session.get("uncached_input")),
                _as_int(session.get("cached_input")),
                _as_int(session.get("output")),
                _as_int(session.get("reasoning_output")),
                _as_int(session.get("total_tokens")),
                _as_int(session.get("call_count")),
                session_cost,
            )
            continue

        date_key = _session_date(session, local_tz)
        if date_key == "unknown":
            continue
        try:
            parsed_day = date.fromisoformat(date_key)
            dated = datetime(parsed_day.year, parsed_day.month, parsed_day.day, tzinfo=local_tz)
        except ValueError:
            continue
        if not _is_plausible_usage_time(dated, reference_now):
            continue
        day = timeline_map[date_key]
        day["date"] = date_key
        _add_token_metrics(
            day,
            _as_int(session.get("uncached_input")),
            _as_int(session.get("cached_input")),
            _as_int(session.get("output")),
            _as_int(session.get("reasoning_output")),
            _as_int(session.get("total_tokens")),
            _as_int(session.get("call_count")),
            session_cost,
        )
        timeline_session_ids[date_key].add(session_key)

    for date_key, day in timeline_map.items():
        day["date"] = date_key
        day["session_count"] = len(timeline_session_ids.get(date_key, set()))
        _round_cost_fields(day)

    span = _timeline_span(timeline_map, window_start, window_end, local_tz)
    timeline_list: list[dict[str, Any]] = []
    if span is not None:
        start_day, end_day = span
        for day in _iter_dates(start_day, end_day):
            date_key = day.isoformat()
            row = timeline_map.get(date_key)
            timeline_list.append(_round_cost_fields(row) if row is not None else _blank_day_row(date_key))

    hourly_timeline = []
    for hour in range(24):
        row = hourly_map[hour]
        row["session_count"] = len(hourly_session_ids.get(hour, set()))
        hourly_timeline.append(_round_cost_fields(row))

    weekday_hour = []
    for weekday in range(7):
        for hour in range(24):
            row = weekday_hour_map[(weekday, hour)]
            row["session_count"] = len(weekday_session_ids.get((weekday, hour), set()))
            weekday_hour.append(_round_cost_fields(row))

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
        "unpriced_model_count": sum(1 for m in models_list if m.get("unpriced")),
        "unpriced_models": sorted(
            str(m.get("model") or "unknown") for m in models_list if m.get("unpriced")
        ),
    }

    unpriced_models = [
        {
            "model": str(m.get("model") or "unknown"),
            "provider": str(m.get("provider") or m.get("tool") or ""),
            "pricing_status": str(m.get("pricing_status") or "unknown"),
        }
        for m in models_list
        if m.get("unpriced")
    ]

    return {
        "tool": tool,
        "summary": summary,
        "models": models_list,
        "timeline": timeline_list,
        "hourly_timeline": hourly_timeline,
        "weekday_hour": weekday_hour,
        "sessions": [_strip_usage_events(session) for session in sessions_combined],
        "unpriced_models": unpriced_models,
    }


def _previous_time_bounds(
    time_range: str,
    now: datetime | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return the immediately preceding equivalent comparison window."""
    cutoff, current = _time_range_cutoff(time_range, now, start, end)
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


_MIN_PLAUSIBLE_USAGE_YEAR = 2020
_MAX_USAGE_AGE_DAYS = 3650


def _is_plausible_usage_time(parsed: datetime, current: datetime) -> bool:
    """Reject sentinel/corrupt timestamps that would distort all-time averages."""
    if parsed.year < _MIN_PLAUSIBLE_USAGE_YEAR:
        return False
    if parsed > current + timedelta(days=1):
        return False
    return (current - parsed).days <= _MAX_USAGE_AGE_DAYS


def _session_span_start(
    sessions: list[dict[str, Any]],
    local_tz,
    current: datetime,
) -> datetime | None:
    """Return the earliest plausible timestamp present on the filtered sessions."""
    earliest: datetime | None = None
    for session in sessions:
        if not isinstance(session, dict):
            continue
        for key in ("created_at", "start_time", "activity_at", "end_time"):
            parsed = _coerce_timestamp(session.get(key), local_tz)
            if parsed is None or not _is_plausible_usage_time(parsed, current):
                continue
            if earliest is None or parsed < earliest:
                earliest = parsed
    return earliest


def _filter_period_days(
    cutoff: datetime | None,
    current: datetime,
    sessions: list[dict[str, Any]],
) -> float:
    """Length of the selected filter in days, used for a 30-day run-rate.

    Bounded ranges use the filter window, including quiet days. All-time uses
    the span from the first plausible session to the end of the window so the
    average reflects actual history rather than an unbounded calendar.
    """
    start = cutoff
    if start is None:
        start = _session_span_start(
            sessions,
            current.tzinfo or datetime.now().astimezone().tzinfo,
            current,
        )
    if start is None:
        return 30.0 if sessions else 1.0
    return max((current - start).total_seconds() / 86400.0, 1.0)


def _projected_30d_cost(total_cost: float, period_days: float) -> float:
    """Extrapolate filter spend to 30 days from the average daily cost."""
    days = max(float(period_days), 1.0)
    return round(float(total_cost) / days * 30.0, 6)


def _build_analytics(
    data: dict[str, Any],
    time_range: str,
    source_sessions: list[dict[str, Any]],
    now: datetime | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Build secondary analytics from the already-filtered dashboard data."""
    normalized = _normalize_time_range(time_range)
    cutoff, current = _time_range_cutoff(normalized, now, start, end)
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
            "estimated": bool(session.get("estimated")),
            "token_source": str(session.get("token_source") or ("estimated" if session.get("estimated") else "reported")),
            "activity_at": str(
                session.get("activity_at")
                or session.get("created_at")
                or session.get("start_time")
                or ""
            ),
        })

    usage_days = [day for day in timeline if _day_has_usage(day)]
    peak_day = None
    peak_source = usage_days or timeline
    if peak_source:
        peak = max(peak_source, key=lambda item: float(item.get("cost_cached_usd") or 0.0))
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
    period_days = _filter_period_days(cutoff, current, sessions)
    projected_30d = _projected_30d_cost(current_cost, period_days)
    if normalized == "all":
        projection_basis = "all_run_rate"
    elif normalized == "month":
        projection_basis = "current_month_run_rate"
    elif normalized == "custom":
        projection_basis = "custom_run_rate"
    else:
        projection_basis = f"{normalized}_run_rate"

    comparison = None
    previous_start, previous_end = _previous_time_bounds(normalized, now, start, end)
    if previous_start is not None and previous_end is not None:
        previous_sessions = _filter_sessions_between(
            source_sessions,
            previous_start,
            previous_end,
            include_end=False,
        )
        previous_data = _build_usage_data(
            previous_sessions,
            str(data.get("tool") or "all"),
            window_start=previous_start,
            window_end=previous_end,
        )
        labels = {
            "month": "previous calendar month",
            "30d": "previous 30 days",
            "7d": "previous 7 days",
            "24h": "previous 24 hours",
        }
        if normalized == "custom":
            inclusive_days = max((current.date() - cutoff.date()).days + 1, 1)
            unit = "day" if inclusive_days == 1 else "days"
            labels["custom"] = f"previous {inclusive_days} {unit}"
        comparison = _build_comparison(
            summary,
            previous_data["summary"],
            labels[normalized],
        )

    return {
        "window_start": cutoff.isoformat() if cutoff is not None else None,
        "window_end": current.isoformat(),
        "active_days": len(usage_days),
        "daily_calls": daily_calls,
        "avg_tokens_per_session": round(
            _as_int(summary.get("total_tokens")) / len(sessions), 2
        ) if sessions else 0.0,
        "avg_cost_per_session_usd": round(
            current_cost / len(sessions), 6
        ) if sessions else 0.0,
        "peak_day": peak_day,
        "top_sessions": top_sessions,
        "projected_30d_usd": projected_30d,
        "monthly_projection_usd": projected_30d,
        "projection_basis": projection_basis,
        "comparison": comparison,
    }


def _filter_usage_data(
    data: dict[str, Any],
    time_range: str,
    now: datetime | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Apply a time range and rebuild all derived metrics."""
    normalized = _normalize_time_range(time_range)
    source_sessions = [s for s in list(data.get("sessions") or []) if isinstance(s, dict)]
    cutoff, current = _time_range_cutoff(normalized, now, start, end)
    if normalized == "all":
        result = _build_usage_data(
            source_sessions,
            str(data.get("tool") or "all"),
            window_start=None,
            window_end=current,
        )
    else:
        filtered_sessions = _filter_sessions(source_sessions, normalized, now, start, end)
        result = _build_usage_data(
            filtered_sessions,
            str(data.get("tool") or "all"),
            window_start=cutoff,
            window_end=current,
        )
    result["time_range"] = normalized
    result["analytics"] = _build_analytics(result, normalized, source_sessions, now, start, end)
    return result


def _canonical_model_name(name: Any) -> str:
    """Resolve a raw model name to its canonical name if known."""
    raw = str(name or "").strip()
    if not raw:
        return ""
    resolved = PRICING_CATALOG.resolve(raw)
    return resolved.canonical_model if resolved.canonical_model else _CANONICAL_MODELS.get(raw.lower(), raw)

def _serialize_extracted_session(session: UsageSession) -> dict[str, Any]:
    """Convert a normalized session into the dashboard's stable JSON shape.

    Provider-reported costs are preserved by the contract. If a source only
    supplies tokens, the shared pricing catalog enriches it here. Unknown
    models remain explicitly unpriced instead of inheriting another model's
    rates.

    Token provenance (estimated vs exact counts) is surfaced separately from
    cost provenance: ``estimated``/``token_source`` describe the token
    counts, while ``cost_source``/``pricing_status`` describe cost.
    """
    _refresh_estimated_session_cost(session)
    serialized = session.to_legacy_dict(include_events=True)
    estimated = bool(session.metadata.get("estimated"))
    token_source = str(session.metadata.get("token_source") or ("estimated" if estimated else "reported"))
    serialized["estimated"] = estimated
    serialized["token_source"] = token_source
    if session.cost is not None:
        serialized["pricing_status"] = session.cost.source
        return serialized

    usage = session.usage
    resolved = calculate_cost_strict(
        session.model,
        usage.uncached_input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        provider=session.provider or session.tool,
        cache_write=usage.cache_write_tokens,
        timestamp=session.created_at or session.start_time or session.activity_at,
    )
    serialized["pricing_status"] = resolved["status"]
    serialized["canonical_model"] = resolved.get("canonical_model")
    serialized["cost_cached_usd"] = resolved.get("cost_cached_usd") or 0.0
    serialized["cost_uncached_usd"] = resolved.get("cost_uncached_usd") or 0.0
    serialized["savings_usd"] = resolved.get("savings_usd") or 0.0
    serialized["total_cost_usd"] = serialized["cost_cached_usd"]
    return serialized


def _source_roots(
    codex_dir: str | Path | None,
    agy_dir: str | Path | None,
    claude_dir: str | Path | None,
    source_dirs: Mapping[str, str | Path | None] | None,
    registry: SourceRegistry,
) -> dict[str, str | Path | None]:
    """Merge legacy source-path arguments with provider-neutral overrides."""
    roots: dict[str, str | Path | None] = {
        "codex": codex_dir,
        "antigravity": agy_dir,
        "claude-code": claude_dir,
    }
    for key, value in (source_dirs or {}).items():
        source = registry.lookup(key)
        canonical = source.key if source is not None else key
        roots[normalize_source_key(canonical)] = value
    return roots


def get_tool_usage(
    tool: str = "all",
    codex_dir: str | Path | None = None,
    agy_dir: str | Path | None = None,
    time_range: str = "all",
    *,
    claude_dir: str | Path | None = None,
    source_dirs: Mapping[str, str | Path | None] | None = None,
    registry: SourceRegistry | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Extract and aggregate usage through registered provider adapters.

    ``codex_dir`` and ``agy_dir`` remain for API compatibility. New providers
    receive paths through ``source_dirs`` and require no aggregator branches.
    ``time_range=custom`` requires inclusive UTC ``start``/``end`` (YYYY-MM-DD).
    """
    time_range_normalized = _normalize_time_range(time_range)
    if time_range_normalized == "custom":
        # Validate early so callers get a 400 before expensive parsing.
        _parse_custom_range(start, end)
    tool_normalized = normalize_source_key(tool or "all")
    active_registry = registry or DEFAULT_SOURCE_REGISTRY

    if tool_normalized == "all":
        sources = active_registry.list_sources()
        result_tool = "all"
    else:
        source = active_registry.lookup(tool_normalized)
        if source is None:
            supported = ", ".join(("all", *active_registry.keys()))
            raise ValueError(
                f"Unsupported tool: {tool!r}. Expected one of: {supported}."
            )
        sources = (source,)
        result_tool = normalize_source_key(source.key)

    roots = _source_roots(codex_dir, agy_dir, claude_dir, source_dirs, active_registry)
    futures = [
        _PARSER_EXECUTOR.submit(
            source.extract_sessions,
            roots.get(normalize_source_key(source.key)),
        )
        for source in sources
    ]

    sessions: list[dict[str, Any]] = []
    for future in futures:
        extracted = future.result()
        for session in extracted:
            if not isinstance(session, UsageSession):
                raise TypeError(
                    "Usage sources must return UsageSession instances; "
                    f"received {type(session).__name__}."
                )
            sessions.append(_serialize_extracted_session(session))

    return _filter_usage_data(
        {"tool": result_tool, "sessions": sessions},
        time_range_normalized,
        start=start,
        end=end,
    )
