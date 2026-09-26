"""Aggregator for multi-tool AI usage metrics."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Iterator

from ..pricing import MODEL_PRICING, PRICING_CATALOG, calculate_cost_strict
from ..timezones import local_timezone, timezone_name
from .agy import AntigravitySource
from .claude import ClaudeCodeSource
from .codex import CodexSource
from .contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession
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


def _parse_custom_range(
    start: str | None,
    end: str | None,
    now: datetime | None = None,
    local_tz=None,
) -> tuple[datetime, datetime]:
    """Parse local-calendar bounds, defaulting a missing end to now."""
    if not start:
        raise ValueError("Custom time range requires a 'start' query parameter (YYYY-MM-DD).")
    try:
        start_date = datetime.strptime(str(start).strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid custom start date: {start!r}. Expected format YYYY-MM-DD."
        )
    dashboard_tz = local_tz or local_timezone()
    start_dt = start_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=dashboard_tz)
    if end:
        try:
            end_date = datetime.strptime(str(end).strip(), "%Y-%m-%d")
        except (TypeError, ValueError):
            raise ValueError(
                f"Invalid custom end date: {end!r}. Expected format YYYY-MM-DD."
            )
        end_dt = end_date.replace(
            hour=23, minute=59, second=59, microsecond=999999, tzinfo=dashboard_tz
        )
    else:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        end_dt = current.astimezone(dashboard_tz)
    if start_dt > end_dt:
        if end:
            raise ValueError(
                f"Invalid custom range: start date {start!r} is after end date {end!r}."
            )
        raise ValueError(f"Invalid custom range: start date {start!r} is after the current time.")
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

    Calendar-month and custom-date boundaries use the dashboard's DST-aware
    local timezone. Relative ranges are measured back from the current instant.
    """
    normalized = _normalize_time_range(time_range)
    dashboard_tz = local_timezone()
    if now is None:
        current = datetime.now(dashboard_tz)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=dashboard_tz)
    else:
        current = now.astimezone(dashboard_tz)

    if normalized == "custom":
        custom_start, custom_end = _parse_custom_range(
            start,
            end,
            current,
            local_tz=dashboard_tz,
        )
        return custom_start, custom_end
    if normalized == "all":
        return None, current
    if normalized == "month":
        cutoff = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif normalized == "30d":
        cutoff = (current.astimezone(timezone.utc) - timedelta(days=30)).astimezone(current.tzinfo)
    elif normalized == "7d":
        cutoff = (current.astimezone(timezone.utc) - timedelta(days=7)).astimezone(current.tzinfo)
    else:  # 24h
        cutoff = (current.astimezone(timezone.utc) - timedelta(hours=24)).astimezone(current.tzinfo)
    return cutoff, current


def _session_timestamp(session: dict[str, Any], local_tz) -> datetime | None:
    """Get the best available timestamp for a session."""
    for key in ("created_at", "start_time", "end_time", "activity_at"):
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


def _event_metrics(event: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    """Read normalized token metrics from one usage event."""
    input_tokens = _as_int(event.get("input_tokens"))
    cached_input = _as_int(event.get("cached_input_tokens") or event.get("cached_input"))
    uncached_input = _as_int(event.get("uncached_input_tokens") or event.get("uncached_input"))
    if input_tokens == 0:
        input_tokens = uncached_input + cached_input
    cached_input = min(input_tokens, cached_input)
    uncached_input = max(0, input_tokens - cached_input)
    cache_write = _as_int(
        event.get("cache_write_tokens")
        or event.get("cache_creation_tokens")
        or event.get("cache_write_input_tokens")
    )
    output = _as_int(event.get("output_tokens") or event.get("output"))
    reasoning_output = _as_int(event.get("reasoning_output_tokens") or event.get("reasoning_output"))
    reported_total = _as_int(event.get("total_tokens"))
    component_total = input_tokens + cache_write + output
    # Contract reconciliation may inflate an event's provider total to cover a
    # session residual. Dashboard model/timeline rows must use the same
    # component-derived basis as the merged session aggregate, while preserving
    # a source total for events that provide no component fields at all.
    total_tokens = component_total or reported_total
    return uncached_input, cached_input, cache_write, output, reasoning_output, total_tokens


def _event_call_count(event: Mapping[str, Any]) -> int:
    """Read an event's represented call count, defaulting ordinary events to one."""
    metadata = event.get("metadata")
    if isinstance(metadata, Mapping) and "call_count" in metadata:
        return _as_int(metadata.get("call_count"))
    if "call_count" in event:
        return _as_int(event.get("call_count"))
    return 1


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
    cache_write: int = 0,
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
                else baseline - actual
            ),
            "reported": True,
        }
    if session.get("reported_cost_usd") is not None:
        total_tokens = _as_int(session.get("total_tokens"))
        if total_tokens > 0:
            weight = _as_int(event.get("total_tokens")) / total_tokens
        else:
            total_calls = max(1, _as_int(session.get("call_count")))
            weight = min(1.0, _event_call_count(event) / total_calls)
        actual = float(session.get("reported_cost_usd") or 0.0) * weight
        baseline = float(session.get("cost_uncached_usd") or actual) * weight
        return {
            "cost_cached_usd": actual,
            "cost_uncached_usd": baseline,
            "savings_usd": float(session.get("savings_usd") or 0.0) * weight,
            "reported": True,
        }
    event_model = str(event.get("model") or session.get("model") or "")
    event_provider = _model_provider(
        event_model,
        str(session.get("provider") or session.get("tool") or "") or None,
    )
    event_ts = event.get("timestamp") or session.get("created_at") or session.get("start_time")
    result = calculate_cost_strict(
        event_model,
        uncached_input,
        cached_input,
        output,
        provider=event_provider,
        cache_write=cache_write,
        cache_write_5m=_as_int(
            event.get("cache_write_5m_tokens")
            or event.get("ephemeral_5m_input_tokens")
        ),
        cache_write_1h=_as_int(
            event.get("cache_write_1h_tokens")
            or event.get("ephemeral_1h_input_tokens")
        ),
        timestamp=event_ts,
    )
    return {
        "cost_cached_usd": float(result.get("cost_cached_usd") or 0.0),
        "cost_uncached_usd": float(result.get("cost_uncached_usd") or 0.0),
        "savings_usd": float(result.get("savings_usd") or 0.0),
        "reported": False,
    }


def _materialize_reported_call_events(session: UsageSession) -> None:
    """Add deterministic zero-token events for authoritative call counts."""
    cost = session.cost
    if cost is None or cost.source != "reported" or cost.reported_usd is None:
        return
    represented_calls = sum(
        _event_call_count(event.to_legacy_dict())
        for event in session.events
    )
    missing_calls = max(0, session.call_count - represented_calls)
    if missing_calls <= 0:
        return
    timestamp = (
        session.created_at
        or session.start_time
        or session.end_time
        or session.activity_at
        or (session.events[-1].timestamp if session.events else None)
    )
    for index in range(missing_calls):
        session.events.append(UsageEvent(
            timestamp=timestamp,
            model=session.model,
            event_id=f"{session.id}:reported-call-{represented_calls + index + 1}",
            metadata={"synthetic": "reported-call", "call_count": 1},
        ))


def _allocate_reported_cost_to_events(session: UsageSession) -> None:
    """Allocate a session-level reported cost across retained events."""
    cost = session.cost
    if cost is None or cost.source != "reported" or cost.reported_usd is None or not session.events:
        return
    total_tokens = sum(event.usage.total_tokens for event in session.events)
    total_calls = sum(
        _event_call_count(event.to_legacy_dict())
        for event in session.events
    )
    if total_tokens <= 0 and total_calls <= 0:
        return
    for event in session.events:
        if event.cost is not None:
            continue
        if total_tokens > 0:
            share = Decimal(event.usage.total_tokens) / Decimal(total_tokens)
        else:
            share = Decimal(_event_call_count(event.to_legacy_dict())) / Decimal(total_calls)
        event.cost = CostEstimate(
            cached_usd=cost.cached_usd * share,
            uncached_usd=cost.uncached_usd * share,
            savings_usd=cost.savings_usd * share,
            reported_usd=cost.reported_usd * share,
            currency=cost.currency,
            source="reported",
        )


def _aggregate_event_costs(events: list[UsageEvent]) -> CostEstimate | None:
    """Aggregate event costs without dropping mixed reported/estimated parts."""
    costs = [event.cost for event in events if event.cost is not None]
    if not costs or len(costs) != len(events):
        return None
    all_reported = all(cost.reported_usd is not None for cost in costs)
    has_reported = any(cost.reported_usd is not None for cost in costs)
    return CostEstimate(
        cached_usd=sum((cost.total_usd for cost in costs), Decimal("0")),
        uncached_usd=sum((cost.uncached_usd for cost in costs), Decimal("0")),
        savings_usd=sum((cost.savings_usd for cost in costs), Decimal("0")),
        reported_usd=(
            sum((cost.reported_usd for cost in costs if cost.reported_usd is not None), Decimal("0"))
            if all_reported else None
        ),
        currency=costs[0].currency,
        source="reported" if all_reported else "mixed" if has_reported else "estimated",
    )


def _refresh_estimated_session_cost(session: UsageSession) -> None:
    """Reprice estimated data against the current catalog.

    Parser snapshots can outlive a pricing refresh. Repricing here keeps the
    dashboard current while preserving provider-reported costs verbatim.
    """
    reported_session_cost = None
    if session.cost is not None and (
        session.cost.source == "reported" or session.cost.reported_usd is not None
    ):
        reported_session_cost = session.cost
        _materialize_reported_call_events(session)
        _allocate_reported_cost_to_events(session)
        all_events_reported = bool(session.events) and all(
            event.cost is not None and event.cost.reported_usd is not None
            for event in session.events
        )
        if not session.events or all_events_reported:
            return
        # A mixed session still needs its non-reported event costs repriced;
        # the event aggregate below will retain the reported subtotal.
        session.cost = None

    provider = _model_provider(
        str(session.model or ""),
        str(session.provider or session.tool or "") or None,
    )
    usage = session.usage
    event_fallback_time = (
        session.created_at or session.start_time or session.end_time or session.activity_at
    )
    resolved = calculate_cost_strict(
        session.model,
        usage.uncached_input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        provider=provider,
        cache_write=usage.cache_write_tokens,
        cache_write_5m=usage.cache_write_5m_tokens,
        cache_write_1h=usage.cache_write_1h_tokens,
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
        event_provider = _model_provider(
            str(event_model or ""),
            provider,
        )
        event_result = calculate_cost_strict(
            event_model,
            event_usage.uncached_input_tokens,
            event_usage.cached_input_tokens,
            event_usage.output_tokens,
            provider=event_provider,
            cache_write=event_usage.cache_write_tokens,
            cache_write_5m=event_usage.cache_write_5m_tokens,
            cache_write_1h=event_usage.cache_write_1h_tokens,
            timestamp=event.timestamp or event_fallback_time,
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

    # Time-dependent pricing (for example, DeepSeek peak/off-peak rates) must
    # be applied to each call at its own timestamp. Reuse those event costs for
    # the session total so all-time totals match the same calls when a range is
    # sliced later. Pricing the whole session at its start time can materially
    # understate usage spread across multiple pricing windows.
    if session.events and not (
        session.cost is not None
        and (session.cost.source == "reported" or session.cost.reported_usd is not None)
    ):
        event_uncached = sum(event.usage.uncached_input_tokens for event in session.events)
        event_cached = sum(event.usage.cached_input_tokens for event in session.events)
        event_output = sum(event.usage.output_tokens for event in session.events)
        event_reasoning = sum(event.usage.reasoning_output_tokens for event in session.events)
        event_total = sum(event.usage.total_tokens for event in session.events)
        event_cache_write = sum(event.usage.cache_write_tokens for event in session.events)
        event_cache_write_5m = sum(event.usage.cache_write_5m_tokens for event in session.events)
        event_cache_write_1h = sum(event.usage.cache_write_1h_tokens for event in session.events)

        # Some providers retain authoritative session totals when only part of
        # a transcript can be converted into timestamped usage events. Keep
        # that residual as an event at the best available session timestamp so
        # all-time and bounded ranges account for it consistently.
        residual_uncached = max(0, usage.uncached_input_tokens - event_uncached)
        residual_cached = max(0, usage.cached_input_tokens - event_cached)
        residual_output = max(0, usage.output_tokens - event_output)
        residual_reasoning = max(0, usage.reasoning_output_tokens - event_reasoning)
        residual_total = max(0, usage.total_tokens - event_total)
        residual_cache_write = max(0, usage.cache_write_tokens - event_cache_write)
        residual_cache_write_5m = max(0, usage.cache_write_5m_tokens - event_cache_write_5m)
        residual_cache_write_1h = max(0, usage.cache_write_1h_tokens - event_cache_write_1h)
        residual_cost_data: dict[str, Any] = {
            "status": "unknown",
            "cost_cached_usd": 0.0,
            "cost_uncached_usd": 0.0,
            "savings_usd": 0.0,
        }
        has_residual = any((
            residual_uncached,
            residual_cached,
            residual_output,
            residual_reasoning,
            residual_total,
            residual_cache_write,
        ))
        represented_call_count = sum(
            _as_int(event.metadata.get("call_count"))
            if "call_count" in event.metadata else 1
            for event in session.events
        )
        residual_call_count = max(0, session.call_count - represented_call_count)
        if has_residual or residual_call_count:
            residual_cost_data = calculate_cost_strict(
                session.model,
                residual_uncached,
                residual_cached,
                residual_output,
                provider=provider,
                cache_write=residual_cache_write,
                cache_write_5m=residual_cache_write_5m,
                cache_write_1h=residual_cache_write_1h,
                timestamp=event_fallback_time,
            )
            residual_cost = (
                CostEstimate(
                    cached_usd=residual_cost_data.get("cost_cached_usd") or 0.0,
                    uncached_usd=residual_cost_data.get("cost_uncached_usd") or 0.0,
                    savings_usd=residual_cost_data.get("savings_usd") or 0.0,
                    source="estimated",
                )
                if residual_cost_data.get("status") == "known" else None
            )
            residual_input = residual_uncached + residual_cached
            session.events.append(UsageEvent(
                timestamp=event_fallback_time,
                usage=TokenUsage(
                    input_tokens=residual_input,
                    cached_input_tokens=residual_cached,
                    output_tokens=residual_output,
                    reasoning_output_tokens=residual_reasoning,
                    total_tokens=residual_total,
                    cache_write_tokens=residual_cache_write,
                    cache_write_5m_tokens=residual_cache_write_5m,
                    cache_write_1h_tokens=residual_cache_write_1h,
                ),
                model=session.model,
                cost=residual_cost,
                event_id=f"{session.id}:session-residual",
                metadata={
                    "synthetic": "session-residual",
                    "call_count": 1 if residual_call_count else 0,
                },
            ))
            # The authoritative call count can exceed both timestamped and
            # untimestamped event records; retain those calls as zero-token
            # placeholders at the same fallback time.
            for index in range(max(0, residual_call_count - 1)):
                session.events.append(UsageEvent(
                    timestamp=event_fallback_time,
                    model=session.model,
                    cost=CostEstimate() if residual_cost_data.get("status") == "known" else None,
                    event_id=f"{session.id}:session-residual-call-{index + 2}",
                    metadata={"synthetic": "session-residual-call", "call_count": 1},
                ))

        event_costs = [event.cost for event in session.events if event.cost is not None]
        if not event_costs and reported_session_cost is not None:
            session.cost = reported_session_cost
            return
        if event_costs:
            all_reported = (
                len(event_costs) == len(session.events)
                and all(cost.reported_usd is not None for cost in event_costs)
                and not has_residual
            )
            has_reported = any(cost.reported_usd is not None for cost in event_costs)
            event_cached_cost = sum(float(cost.total_usd) for cost in event_costs)
            event_uncached_cost = sum(float(cost.uncached_usd) for cost in event_costs)
            event_savings = sum(float(cost.savings_usd) for cost in event_costs)
            session.cost = CostEstimate(
                cached_usd=event_cached_cost,
                uncached_usd=event_uncached_cost,
                savings_usd=event_savings,
                reported_usd=(
                    sum(float(cost.reported_usd or 0.0) for cost in event_costs)
                    if all_reported else None
                ),
                currency=event_costs[0].currency if event_costs else "USD",
                source=(
                    "reported" if all_reported
                    else "mixed" if has_reported
                    else "estimated"
                ),
            )


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
    unplaced_events: list[dict[str, Any]] = []
    timestamped_event_count = 0
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        event_timestamp = _event_timestamp(event, local_tz)
        if event_timestamp is None:
            unplaced_events.append(event)
            continue
        timestamped_event_count += 1
        if _timestamp_in_bounds(event_timestamp, start, end, include_end):
            selected_events.append((event, event_timestamp))

    # When only part of a session has per-call timestamps, place the remaining
    # records at the session fallback timestamp if it falls in this range.
    # This keeps the full-session residual used by all-time totals visible in
    # the matching bounded range.
    fallback_timestamp = _session_timestamp(session, local_tz)
    if timestamped_event_count and fallback_timestamp is not None and _timestamp_in_bounds(
        fallback_timestamp, start, end, include_end
    ):
        for event in unplaced_events:
            placed_event = dict(event)
            placed_event["timestamp"] = fallback_timestamp.isoformat()
            selected_events.append((placed_event, fallback_timestamp))

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
    cache_write = 0
    cache_write_5m = 0
    cache_write_1h = 0
    output = 0
    reasoning_output = 0
    total_tokens = 0
    cost_cached = 0.0
    cost_uncached = 0.0
    savings = 0.0
    for event, _event_time in selected_events:
        event_uncached, event_cached, event_cache_write, event_output, event_reasoning, event_total = _event_metrics(event)
        uncached_input += event_uncached
        cached_input += event_cached
        cache_write += event_cache_write
        cache_write_5m += _as_int(event.get("cache_write_5m_tokens") or event.get("ephemeral_5m_input_tokens"))
        cache_write_1h += _as_int(event.get("cache_write_1h_tokens") or event.get("ephemeral_1h_input_tokens"))
        output += event_output
        reasoning_output += event_reasoning
        total_tokens += event_total
        event_cost = _event_cost(
            session,
            event,
            event_uncached,
            event_cached,
            event_output,
            event_cache_write,
        )
        cost_cached += event_cost["cost_cached_usd"]
        cost_uncached += event_cost["cost_uncached_usd"]
        savings += event_cost["savings_usd"]

    sliced = dict(session)
    sliced["usage_events"] = [event for event, _event_time in selected_events]
    sliced.update({
        "call_count": sum(_event_call_count(event) for event, _event_time in selected_events),
        "uncached_input": uncached_input,
        "cached_input": cached_input,
        "total_input": uncached_input + cached_input + cache_write,
        "cache_write": cache_write,
        "cache_write_5m": cache_write_5m,
        "cache_write_1h": cache_write_1h,
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
        "cache_write": 0,
        "output": 0,
        "reasoning_output": 0,
        "total_tokens": 0,
        "cache_hit_rate": 0.0,
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
        "cache_write": 0,
        "output": 0,
        "reasoning_output": 0,
        "total_tokens": 0,
        "cache_hit_rate": 0.0,
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
    cache_write: int,
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
        row["cache_write"] = int(row.get("cache_write") or 0) + cache_write
        row["total_input"] = int(row.get("total_input") or 0) + uncached_input + cached_input + cache_write
        row["output"] = int(row.get("output") or 0) + output
        row["reasoning_output"] = int(row.get("reasoning_output") or 0) + reasoning_output
        cacheable_input = int(row.get("uncached_input") or 0) + int(row.get("cached_input") or 0)
        row["cache_hit_rate"] = (
            round(int(row.get("cached_input") or 0) / cacheable_input * 100.0, 2)
            if cacheable_input > 0 else 0.0
        )
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


def _model_provider(model_name: str, fallback: str | None) -> str | None:
    """Infer the pricing provider from an explicit model identifier."""
    normalized = str(model_name or "").strip().casefold()
    if normalized.startswith("deepseek"):
        return "deepseek"
    if normalized.startswith("gemini"):
        return "antigravity"
    if normalized.startswith("claude"):
        return "claude"
    if normalized.startswith(("gpt-", "o1", "o3", "o4")):
        return "codex"
    return fallback


def _session_model_contributions(
    session: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Split one session into model-specific usage and cost contributions."""
    raw_events = session.get("usage_events")
    events = [event for event in raw_events if isinstance(event, dict)] if isinstance(raw_events, list) else []
    contributions: list[dict[str, Any]] = []

    if not events:
        uncached = _as_int(session.get("uncached_input"))
        cached = _as_int(session.get("cached_input"))
        cache_write = _as_int(session.get("cache_write"))
        output = _as_int(session.get("output"))
        reasoning = _as_int(session.get("reasoning_output"))
        return [{
            "model": str(session.get("model") or "unknown"),
            "uncached_input": uncached,
            "cached_input": cached,
            "cache_write": cache_write,
            "output": output,
            "reasoning_output": reasoning,
            "total_tokens": _as_int(session.get("total_tokens")),
            "call_count": _as_int(session.get("call_count")),
            "cost": {
                "cost_cached_usd": float(session.get("cost_cached_usd") or 0.0),
                "cost_uncached_usd": float(session.get("cost_uncached_usd") or 0.0),
                "savings_usd": float(session.get("savings_usd") or 0.0),
                "reported": session.get("reported_cost_usd") is not None,
            },
        }]

    for event in events:
        uncached, cached, cache_write, output, reasoning, total = _event_metrics(event)
        contributions.append({
            "model": str(event.get("model") or session.get("model") or "unknown"),
            "uncached_input": uncached,
            "cached_input": cached,
            "cache_write": cache_write,
            "output": output,
            "reasoning_output": reasoning,
            "total_tokens": total,
            "call_count": _event_call_count(event),
            "cost": _event_cost(session, event, uncached, cached, output, cache_write),
        })

    event_calls = sum(contribution["call_count"] for contribution in contributions)
    residual_uncached = max(0, _as_int(session.get("uncached_input")) - sum(c["uncached_input"] for c in contributions))
    residual_cached = max(0, _as_int(session.get("cached_input")) - sum(c["cached_input"] for c in contributions))
    residual_cache_write = max(0, _as_int(session.get("cache_write")) - sum(c["cache_write"] for c in contributions))
    residual_output = max(0, _as_int(session.get("output")) - sum(c["output"] for c in contributions))
    residual_reasoning = max(0, _as_int(session.get("reasoning_output")) - sum(c["reasoning_output"] for c in contributions))
    residual_total = max(0, _as_int(session.get("total_tokens")) - sum(c["total_tokens"] for c in contributions))
    residual_calls = max(0, _as_int(session.get("call_count")) - event_calls)
    if any((residual_uncached, residual_cached, residual_cache_write, residual_output,
            residual_reasoning, residual_total, residual_calls)):
        event_cost = {
            key: sum(float(contribution["cost"].get(key) or 0.0) for contribution in contributions)
            for key in ("cost_cached_usd", "cost_uncached_usd", "savings_usd")
        }
        contributions.append({
            "model": str(session.get("model") or "unknown"),
            "uncached_input": residual_uncached,
            "cached_input": residual_cached,
            "cache_write": residual_cache_write,
            "output": residual_output,
            "reasoning_output": residual_reasoning,
            "total_tokens": residual_total,
            "call_count": residual_calls,
            "cost": {
                "cost_cached_usd": float(session.get("cost_cached_usd") or 0.0) - event_cost["cost_cached_usd"],
                "cost_uncached_usd": float(session.get("cost_uncached_usd") or 0.0) - event_cost["cost_uncached_usd"],
                "savings_usd": float(session.get("savings_usd") or 0.0) - event_cost["savings_usd"],
                "reported": session.get("reported_cost_usd") is not None,
            },
        })
    return contributions


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

    models_map: dict[tuple[str, str], dict[str, Any]] = {}
    model_statuses: dict[tuple[str, str], set[str]] = defaultdict(set)
    model_token_sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    model_cost_sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    for session_index, session in enumerate(sessions_combined):
        session_key = f"{session.get('tool', '')}:{session.get('id', session_index)}"
        session_token_source = str(
            session.get("token_source")
            or ("estimated" if session.get("estimated") else "reported")
        )
        tool_value = str(session.get("tool") or (tool if tool != "all" else "")).strip()
        provider_hint = str(session.get("provider") or tool_value or "").strip() or None
        for contribution in _session_model_contributions(session):
            raw_model = str(contribution["model"] or "unknown")
            contribution_provider = _model_provider(raw_model, provider_hint)
            resolved = PRICING_CATALOG.resolve(raw_model, contribution_provider)
            canonical_model = resolved.canonical_model or _canonical_model_name(raw_model)
            model_key = (contribution_provider or tool_value or "unknown", canonical_model)
            uncached_input = int(contribution["uncached_input"])
            cached_input = int(contribution["cached_input"])
            cache_write = int(contribution["cache_write"])
            total_input = uncached_input + cached_input + cache_write
            cost = contribution["cost"]
            if cost.get("reported"):
                contribution_cost_source = "reported"
            elif resolved.status == "known":
                contribution_cost_source = "estimated"
            else:
                contribution_cost_source = "unavailable"
            model = models_map.setdefault(model_key, {
                "model": canonical_model,
                "canonical_model": canonical_model,
                "tool": tool_value,
                "provider": contribution_provider or tool_value,
                "call_count": 0,
                "session_ids": set(),
                "uncached_input": 0,
                "cached_input": 0,
                "cache_write": 0,
                "total_input": 0,
                "output": 0,
                "reasoning_output": 0,
                "total_tokens": 0,
                "cache_hit_rate": 0.0,
                "est_cost_cached_usd": 0.0,
                "est_cost_uncached_usd": 0.0,
                "est_savings_usd": 0.0,
            })
            if model["tool"] != tool_value:
                model["tool"] = "all"
            model["call_count"] += int(contribution["call_count"])
            model["session_ids"].add(session_key)
            model["uncached_input"] += uncached_input
            model["cached_input"] += cached_input
            model["cache_write"] += cache_write
            model["total_input"] += total_input
            model["output"] += int(contribution["output"])
            model["reasoning_output"] += int(contribution["reasoning_output"])
            model["total_tokens"] += int(contribution["total_tokens"])
            model["est_cost_cached_usd"] += float(cost["cost_cached_usd"])
            model["est_cost_uncached_usd"] += float(cost["cost_uncached_usd"])
            model["est_savings_usd"] += float(cost["savings_usd"])
            model_cost_sources[model_key].add(contribution_cost_source)
            model_statuses[model_key].add(resolved.status)
            model_token_sources[model_key].add(session_token_source)

    models_list: list[dict[str, Any]] = []
    local_tz = (
        window_end.tzinfo
        if window_end is not None and window_end.tzinfo is not None
        else window_start.tzinfo
        if window_start is not None and window_start.tzinfo is not None
        else local_timezone()
    )
    for model_key, model in models_map.items():
        model["session_count"] = len(model.pop("session_ids"))
        cacheable_input = int(model["uncached_input"]) + int(model["cached_input"])
        model["cache_hit_rate"] = round(
            int(model["cached_input"]) / cacheable_input * 100.0,
            2,
        ) if cacheable_input > 0 else 0.0
        model["est_cost_cached_usd"] = round(float(model["est_cost_cached_usd"]), 6)
        model["est_cost_uncached_usd"] = round(float(model["est_cost_uncached_usd"]), 6)
        model["est_savings_usd"] = round(float(model["est_savings_usd"]), 6)
        statuses = model_statuses[model_key]
        cost_sources = model_cost_sources[model_key]
        has_reported = "reported" in cost_sources
        has_estimated = "estimated" in cost_sources
        has_unavailable = "unavailable" in cost_sources
        priced = not has_unavailable
        unpriced = has_unavailable
        if unpriced and not (has_reported or has_estimated):
            model["est_cost_cached_usd"] = 0.0
            model["est_cost_uncached_usd"] = 0.0
            model["est_savings_usd"] = 0.0
        if has_reported and (has_estimated or has_unavailable):
            pricing_status = "mixed"
        elif has_reported:
            pricing_status = "reported"
        elif has_estimated:
            pricing_status = "known"
        elif "ambiguous" in statuses:
            pricing_status = "ambiguous"
        elif "unpriced" in statuses:
            pricing_status = "unpriced"
        elif "unknown" in statuses:
            pricing_status = "unknown"
        else:
            pricing_status = "unavailable"
        model["pricing_status"] = pricing_status
        model["cost_source"] = (
            "mixed" if has_reported and (has_estimated or has_unavailable)
            else "reported" if has_reported
            else "estimated" if has_estimated
            else "unavailable"
        )
        model["cost_available"] = not has_unavailable
        model["priced"] = priced
        model["unpriced"] = unpriced
        token_sources = model_token_sources[model_key]
        model["token_source"] = (
            "estimated" if token_sources == {"estimated"}
            else "reported" if token_sources == {"reported"}
            else "mixed"
        )
        model["estimated"] = model["token_source"] == "estimated"
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
    reference_now = window_end if window_end is not None else datetime.now(local_tz)
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=local_tz)

    def record_point(
        when: datetime,
        session_key: str,
        uncached_input: int,
        cached_input: int,
        cache_write: int,
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
            cache_write,
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
            cache_write,
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
            cache_write,
            output,
            reasoning_output,
            total_tokens,
            call_count,
            cost,
        )
        weekday_session_ids[weekday_key].add(session_key)

    for session_index, session in enumerate(sessions_combined):
        session_key = f"{session.get('tool', '')}:{session.get('id', f'session-{session_index}')}"
        event_rows: list[tuple[dict[str, Any], datetime]] = []
        fallback_event_time = _session_timestamp(session, local_tz)
        raw_events = session.get("usage_events")
        if isinstance(raw_events, list):
            for event in raw_events:
                if not isinstance(event, dict):
                    continue
                event_time = _event_timestamp(event, local_tz)
                if event_time is None and fallback_event_time is not None:
                    event = dict(event)
                    event["timestamp"] = fallback_event_time.isoformat()
                    event_time = fallback_event_time
                if event_time is not None:
                    event_rows.append((event, event_time))

        if event_rows:
            for event, event_time in event_rows:
                event_uncached, event_cached, event_cache_write, event_output, event_reasoning, event_total = _event_metrics(event)
                event_cost = _event_cost(
                    session,
                    event,
                    event_uncached,
                    event_cached,
                    event_output,
                    event_cache_write,
                )
                record_point(
                    event_time,
                    session_key,
                    event_uncached,
                    event_cached,
                    event_cache_write,
                    event_output,
                    event_reasoning,
                    event_total,
                    _event_call_count(event),
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
                _as_int(session.get("cache_write")),
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
            _as_int(session.get("cache_write")),
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
    cache_write = sum(int(s.get("cache_write") or 0) for s in sessions_combined)
    total_input = uncached_input + cached_input + cache_write
    output = sum(int(s.get("output") or 0) for s in sessions_combined)
    reasoning_output = sum(int(s.get("reasoning_output") or 0) for s in sessions_combined)
    total_tokens = sum(int(s.get("total_tokens") or 0) for s in sessions_combined)
    cost_cached = round(sum(float(s.get("cost_cached_usd") or 0.0) for s in sessions_combined), 6)
    cost_uncached = round(sum(float(s.get("cost_uncached_usd") or 0.0) for s in sessions_combined), 6)
    savings = round(sum(float(s.get("savings_usd") or 0.0) for s in sessions_combined), 6)
    unpriced_count = sum(1 for m in models_list if m.get("unpriced"))

    summary = {
        "total_tokens": total_tokens,
        "uncached_input": uncached_input,
        "cached_input": cached_input,
        "total_input": total_input,
        "cache_write": cache_write,
        "output": output,
        "reasoning_output": reasoning_output,
        "cost_cached_usd": cost_cached,
        "cost_uncached_usd": cost_uncached,
        "savings_usd": savings,
        "total_cost_usd": cost_cached,
        "cache_hit_rate": round((cached_input / (uncached_input + cached_input) * 100.0), 2) if uncached_input + cached_input > 0 else 0.0,
        "session_count": len(sessions_combined),
        "call_count": sum(int(s.get("call_count") or 0) for s in sessions_combined),
        "unpriced_model_count": unpriced_count,
        "cost_complete": unpriced_count == 0,
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
        "timezone": timezone_name(local_tz),
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

    window_length = current.astimezone(timezone.utc) - cutoff.astimezone(timezone.utc)
    previous_start = (cutoff.astimezone(timezone.utc) - window_length).astimezone(current.tzinfo)
    return previous_start, cutoff


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
            current.tzinfo or local_timezone(),
            current,
        )
    if start is None:
        return 30.0 if sessions else 1.0
    elapsed = current.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    return max(elapsed.total_seconds() / 86400.0, 1.0)


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
            "pricing_status": str(session.get("pricing_status") or session.get("cost_source") or "unknown"),
            "cost_source": str(session.get("cost_source") or session.get("pricing_status") or "unknown"),
            "cost_available": session.get("cost_available") is not False,
            "activity_at": str(
                session.get("activity_at")
                or session.get("created_at")
                or session.get("start_time")
                or ""
            ),
        })

    usage_days = [day for day in timeline if _day_has_usage(day)]
    peak_day = None
    if usage_days:
        peak = max(usage_days, key=lambda item: float(item.get("cost_cached_usd") or 0.0))
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
        range_now = current if normalized == "custom" and not end else now
        filtered_sessions = _filter_sessions(source_sessions, normalized, range_now, start, end)
        result = _build_usage_data(
            filtered_sessions,
            str(data.get("tool") or "all"),
            window_start=cutoff,
            window_end=current,
        )
    result["time_range"] = normalized
    analytics_now = current if normalized == "custom" and not end else now
    result["analytics"] = _build_analytics(
        result, normalized, source_sessions, analytics_now, start, end
    )
    return result


def _canonical_model_name(name: Any) -> str:
    """Resolve a raw model name to its canonical name if known."""
    raw = str(name or "").strip()
    if not raw:
        return ""
    resolved = PRICING_CATALOG.resolve(raw)
    return resolved.canonical_model if resolved.canonical_model else _CANONICAL_MODELS.get(raw.lower(), raw)


def _sum_token_usage(usages: list[TokenUsage]) -> TokenUsage:
    """Aggregate normalized token usage without losing cache-write components."""
    total = sum(usage.total_tokens for usage in usages)
    aggregate = TokenUsage(
        input_tokens=sum(usage.input_tokens for usage in usages),
        cached_input_tokens=sum(usage.cached_input_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        reasoning_output_tokens=sum(usage.reasoning_output_tokens for usage in usages),
        total_tokens=total,
        cache_read_tokens=sum(usage.cache_read_tokens or 0 for usage in usages),
        cache_write_tokens=sum(usage.cache_write_tokens for usage in usages),
        cache_write_5m_tokens=sum(usage.cache_write_5m_tokens for usage in usages),
        cache_write_1h_tokens=sum(usage.cache_write_1h_tokens for usage in usages),
        preserve_total=True,
    )
    # The sum of authoritative provider totals must not be raised to the
    # component-derived bound during an intermediate merge.
    aggregate.total_tokens = total
    return aggregate


def _max_token_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    """Merge two cumulative usage snapshots without adding duplicate usage."""
    return TokenUsage(
        input_tokens=max(left.input_tokens, right.input_tokens),
        cached_input_tokens=max(left.cached_input_tokens, right.cached_input_tokens),
        output_tokens=max(left.output_tokens, right.output_tokens),
        reasoning_output_tokens=max(left.reasoning_output_tokens, right.reasoning_output_tokens),
        total_tokens=max(left.total_tokens, right.total_tokens),
        cache_read_tokens=max(left.cache_read_tokens or 0, right.cache_read_tokens or 0),
        cache_write_tokens=max(left.cache_write_tokens, right.cache_write_tokens),
        cache_write_5m_tokens=max(left.cache_write_5m_tokens, right.cache_write_5m_tokens),
        cache_write_1h_tokens=max(left.cache_write_1h_tokens, right.cache_write_1h_tokens),
        preserve_total=True,
    )


def _sum_event_component_usage(events: list[UsageEvent]) -> TokenUsage:
    """Aggregate event components without trusting reconciled event totals."""
    aggregate = _sum_token_usage([event.usage for event in events])
    component_total = aggregate.total_input + aggregate.output_tokens
    aggregate.total_tokens = component_total or sum(event.usage.total_tokens for event in events)
    return aggregate


def _subtract_component_usage(total: TokenUsage, represented: TokenUsage) -> TokenUsage:
    """Return component-level usage not present in a represented aggregate."""
    input_tokens = max(0, total.input_tokens - represented.input_tokens)
    cached_input = max(0, total.cached_input_tokens - represented.cached_input_tokens)
    output_tokens = max(0, total.output_tokens - represented.output_tokens)
    reasoning_output = max(
        0,
        total.reasoning_output_tokens - represented.reasoning_output_tokens,
    )
    cache_write = max(0, total.cache_write_tokens - represented.cache_write_tokens)
    cache_write_5m = max(
        0,
        total.cache_write_5m_tokens - represented.cache_write_5m_tokens,
    )
    cache_write_1h = max(
        0,
        total.cache_write_1h_tokens - represented.cache_write_1h_tokens,
    )
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output,
        total_tokens=max(
            input_tokens + cache_write + output_tokens,
            max(0, total.total_tokens - represented.total_tokens),
        ),
        cache_read_tokens=max(0, (total.cache_read_tokens or 0) - (represented.cache_read_tokens or 0)),
        cache_write_tokens=cache_write,
        cache_write_5m_tokens=cache_write_5m,
        cache_write_1h_tokens=cache_write_1h,
        preserve_total=True,
    )


def _has_component_usage(usage: TokenUsage) -> bool:
    """Whether a usage aggregate contains any component-level tokens."""
    return any((
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        usage.reasoning_output_tokens,
        usage.cache_write_tokens,
        usage.total_tokens,
    ))


def _event_identity(event: UsageEvent) -> tuple[str, ...]:
    """Return a stable identity for event-level cross-file deduplication."""
    if event.event_id:
        return ("event_id", str(event.event_id))
    timestamp = event.timestamp.isoformat() if hasattr(event.timestamp, "isoformat") else str(event.timestamp or "")
    usage = event.usage
    return (
        "fingerprint",
        timestamp,
        str(event.model or ""),
        str(usage.input_tokens),
        str(usage.cached_input_tokens),
        str(usage.output_tokens),
        str(usage.reasoning_output_tokens),
        str(usage.cache_write_tokens),
        str(usage.cache_write_5m_tokens),
        str(usage.cache_write_1h_tokens),
    )


def _merge_usage_sessions(sessions: list[UsageSession]) -> list[UsageSession]:
    """Merge segmented records that share one provider and canonical session ID."""
    merged: dict[tuple[str, str], UsageSession] = {}
    order: list[tuple[str, str]] = []
    for session in sessions:
        if session.cost is not None and (
            session.cost.source == "reported" or session.cost.reported_usd is not None
        ):
            _materialize_reported_call_events(session)
        key = (normalize_source_key(str(session.tool or session.provider or "")), str(session.id))
        current = merged.get(key)
        if current is None:
            merged[key] = session
            order.append(key)
            continue

        seen_event_ids = {
            identity
            for event in current.events
            if (identity := _event_identity(event)) is not None
        }
        current_event_identities = set(seen_event_ids)
        current_comparable_event_identities = {
            _event_identity(event)
            for event in current.events
            if not event.metadata.get("synthetic")
        } or current_event_identities
        represented = _sum_event_component_usage(session.events)
        residual = (
            _subtract_component_usage(session.usage, represented)
            if session.events
            else session.usage
        )
        represented_calls = sum(
            _event_call_count(event.to_legacy_dict())
            for event in session.events
        )
        residual_calls = max(0, session.call_count - represented_calls)

        current_is_reported = bool(
            current.cost
            and current.cost.source == "reported"
            and current.cost.reported_usd is not None
        )
        incoming_is_reported = bool(
            session.cost
            and session.cost.source == "reported"
            and session.cost.reported_usd is not None
        )
        duplicate_events: list[UsageEvent] = []
        duplicate_calls = 0
        new_events: list[UsageEvent] = []
        for event in session.events:
            identity = _event_identity(event)
            if identity in seen_event_ids:
                duplicate_events.append(event)
                duplicate_calls += _event_call_count(event.to_legacy_dict())
                continue
            seen_event_ids.add(identity)
            new_events.append(event)
        duplicate_usage = _sum_event_component_usage(duplicate_events)
        incoming_event_identities = {
            _event_identity(event)
            for event in session.events
        }

        # A fully duplicate segment contributes no tokens or calls again. A
        # source record with no events can still contribute an authoritative
        # session-level residual. Duplicate-event snapshots can also carry
        # newer aggregate totals, which are cumulative and must be reconciled
        # without counting their events again.
        if not new_events and session.events:
            current.usage = _max_token_usage(current.usage, session.usage)
            current.call_count = max(current.call_count, session.call_count)
            continue

        if current_is_reported and not incoming_is_reported:
            _allocate_reported_cost_to_events(current)
        elif incoming_is_reported and not current_is_reported:
            _allocate_reported_cost_to_events(session)
        elif current_is_reported and incoming_is_reported:
            for event in (*current.events, *new_events):
                event.cost = None

        current.events.extend(new_events)
        combined_usage = _sum_event_component_usage(current.events)
        missing_residual = _subtract_component_usage(residual, combined_usage)
        current.usage = (
            _sum_token_usage([combined_usage, missing_residual])
            if _has_component_usage(missing_residual)
            else combined_usage
        )
        new_call_count = sum(
            _event_call_count(event.to_legacy_dict())
            for event in new_events
        )
        residual_call_count = residual_calls
        if new_events and _has_component_usage(residual) and not _has_component_usage(missing_residual):
            residual_call_count = 0
        current.call_count += new_call_count + residual_call_count
        current.metadata.update(session.metadata)
        current.metadata["merged_session_count"] = int(
            current.metadata.get("merged_session_count", 1)
        ) + 1

        if current_is_reported and incoming_is_reported and current.cost and session.cost:
            current_comparable = current_comparable_event_identities
            incoming_comparable = {
                _event_identity(event)
                for event in session.events
                if not event.metadata.get("synthetic")
            } or incoming_event_identities
            incoming_is_subset = incoming_comparable <= current_comparable
            current_is_subset = current_comparable <= incoming_comparable
            if incoming_is_subset:
                # The incoming record contains no unique calls; its reported
                # total is already covered by the current event set.
                pass
            elif current_is_subset:
                # The incoming record is the more complete snapshot of the same
                # authoritative session, regardless of merge order.
                current.cost = session.cost
            else:
                session_total = represented.total_tokens
                if session_total > 0:
                    accepted_share = Decimal(
                        max(0, session_total - duplicate_usage.total_tokens)
                    ) / Decimal(session_total)
                elif duplicate_calls > 0:
                    accepted_share = Decimal(
                        max(0, session.call_count - duplicate_calls)
                    ) / Decimal(max(1, session.call_count))
                else:
                    accepted_share = Decimal(1)
                current.cost = CostEstimate(
                    cached_usd=current.cost.cached_usd + session.cost.cached_usd * accepted_share,
                    uncached_usd=current.cost.uncached_usd + session.cost.uncached_usd * accepted_share,
                    savings_usd=current.cost.savings_usd + session.cost.savings_usd * accepted_share,
                    reported_usd=current.cost.reported_usd + session.cost.reported_usd * accepted_share,
                    currency=current.cost.currency,
                    source="reported",
                )
        elif current_is_reported != incoming_is_reported or not current_is_reported:
            # Mixed provenance is rebuilt from event-level reported and
            # estimated costs during the normal refresh pass.
            current.cost = None

        for timestamp_name, reducer in (
            ("created_at", min),
            ("start_time", min),
        ):
            values = [
                value
                for value in (getattr(current, timestamp_name), getattr(session, timestamp_name))
                if value is not None
            ]
            if values:
                setattr(current, timestamp_name, reducer(values))
        for timestamp_name in ("end_time", "activity_at"):
            values = [
                value
                for value in (getattr(current, timestamp_name), getattr(session, timestamp_name))
                if value is not None
            ]
            if values:
                setattr(current, timestamp_name, max(values))
        if len(str(session.title or "")) > len(str(current.title or "")):
            current.title = session.title

    for key, session in merged.items():
        models = {
            str(model).strip()
            for model in [session.model, *(event.model for event in session.events)]
            if model and str(model).strip().lower() not in {"mixed", "unknown"}
        }
        if len(models) > 1:
            session.model = "mixed"
        elif models:
            session.model = next(iter(models))
        for event in session.events:
            if event.model is None and session.model != "mixed":
                event.model = session.model
    return [merged[key] for key in order]

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
    component_total = int(session.usage.total_input + session.usage.output_tokens)
    reported_total = int(session.usage.total_tokens)
    serialized["total_tokens"] = component_total or reported_total
    if component_total and component_total != reported_total:
        serialized["reported_total_tokens"] = reported_total
    estimated = bool(session.metadata.get("estimated"))
    token_source = str(session.metadata.get("token_source") or ("estimated" if estimated else "reported"))
    serialized["estimated"] = estimated
    serialized["token_source"] = token_source
    serialized["cost_available"] = session.cost is not None
    serialized["cost_source"] = session.cost.source if session.cost is not None else "unavailable"
    if session.cost is not None:
        serialized["pricing_status"] = session.cost.source
        return serialized

    usage = session.usage
    resolved = calculate_cost_strict(
        session.model,
        usage.uncached_input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        provider=_model_provider(
            str(session.model or ""),
            str(session.provider or session.tool or "") or None,
        ),
        cache_write=usage.cache_write_tokens,
        cache_write_5m=usage.cache_write_5m_tokens,
        cache_write_1h=usage.cache_write_1h_tokens,
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
    ``time_range=custom`` requires a local-calendar ``start`` date
    (YYYY-MM-DD); the optional ``end`` date is inclusive and defaults to the
    current instant in the dashboard timezone.
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

    extracted_sessions: list[UsageSession] = []
    for future in futures:
        extracted = future.result()
        for session in extracted:
            if not isinstance(session, UsageSession):
                raise TypeError(
                    "Usage sources must return UsageSession instances; "
                    f"received {type(session).__name__}."
                )
            extracted_sessions.append(session)

    sessions = [
        _serialize_extracted_session(session)
        for session in _merge_usage_sessions(extracted_sessions)
    ]

    return _filter_usage_data(
        {"tool": result_tool, "sessions": sessions},
        time_range_normalized,
        start=start,
        end=end,
    )
