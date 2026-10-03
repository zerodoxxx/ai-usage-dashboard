"""Parser for Codex sessions and rollout logs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import UsageSession
from .file_cache import ParsedFileCache
from ..pricing import calculate_cost_strict

logger = logging.getLogger(__name__)

_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "cache_write_input_tokens",
    "total_tokens",
)


def _as_int(value: Any) -> int:
    """Convert a usage value to a non-negative integer."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _normalize_usage(usage: Any) -> dict[str, int] | None:
    """Normalize a usage mapping and fill a missing total-token value."""
    if not isinstance(usage, dict):
        return None

    normalized = {field: _as_int(usage.get(field)) for field in _USAGE_FIELDS}
    normalized["cached_input_tokens"] = min(
        normalized["input_tokens"], normalized["cached_input_tokens"]
    )
    if normalized["total_tokens"] == 0:
        normalized["total_tokens"] = (
            normalized["input_tokens"]
            + normalized["cache_write_input_tokens"]
            + normalized["output_tokens"]
        )
    return normalized if any(normalized.values()) else None


def _usage_delta(current: dict[str, int], previous: dict[str, int] | None) -> dict[str, int]:
    """Return the positive delta between cumulative usage snapshots."""
    if previous is None or any(current[field] < previous[field] for field in _USAGE_FIELDS):
        return dict(current)
    return {
        field: max(0, current[field] - previous[field])
        for field in _USAGE_FIELDS
    }


def _allocate_total(total: int, weights: list[int]) -> list[int]:
    """Distribute a total across events while preserving the exact sum."""
    if not weights:
        return []
    if total <= 0:
        return [0] * len(weights)
    safe_weights = [max(0, int(weight)) for weight in weights]
    weight_sum = sum(safe_weights)
    if weight_sum <= 0:
        safe_weights = [1] * len(weights)
        weight_sum = len(weights)

    allocations = [(total * weight) // weight_sum for weight in safe_weights]
    remainder = total - sum(allocations)
    fractions = sorted(
        range(len(weights)),
        key=lambda index: (total * safe_weights[index]) % weight_sum,
        reverse=True,
    )
    for index in fractions[:remainder]:
        allocations[index] += 1
    return allocations


def _event_totals(events: list[dict[str, Any]]) -> dict[str, int]:
    """Sum normalized metrics across a list of usage events."""
    return {
        field: sum(_as_int(event.get(field)) for event in events)
        for field in _USAGE_FIELDS
    }


def _advance_usage_total(
    previous: dict[str, int],
    usage: dict[str, int] | None,
    cumulative: dict[str, int] | None,
) -> dict[str, int]:
    """Keep observed responses plus any history supplied by a scoped total."""
    return {
        field: max(
            previous[field] + (usage[field] if usage else 0),
            cumulative[field] if cumulative else 0,
        )
        for field in _USAGE_FIELDS
    }


def _timestamp_seconds(value: Any) -> float | None:
    """Parse a rollout timestamp for inferred call intervals."""
    try:
        if isinstance(value, (int, float)):
            return value / 1000.0 if value > 1e11 else float(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


class _ResponseTiming:
    """Pair a response with input received before its first generated item."""

    def __init__(self) -> None:
        self.input_timestamp: Any = None
        self.start: Any = None
        self.end: Any = None
        self.model: str | None = None

    def observe_output(self, timestamp: Any, model: str | None) -> None:
        if self.end is None:
            self.start = self.input_timestamp
            self.model = model
        self.end = timestamp

    def finish(
        self, event: dict[str, Any] | None, completion: Any = None, *, trustworthy: bool = True
    ) -> None:
        end = completion if completion is not None else self.end
        start_seconds = _timestamp_seconds(self.start)
        end_seconds = _timestamp_seconds(end)
        if event is not None:
            if end is not None:
                event["timestamp"] = _to_iso_string(end)
            if self.model:
                event["model"] = self.model
            event["metadata"] = {
                "tps_duration_seconds": (
                    end_seconds - start_seconds
                    if end_seconds is not None and start_seconds is not None else None
                ),
                "tps_output_tokens": event["output_tokens"],
                "tps_trustworthy": trustworthy and self.end is not None,
            }
        # A legacy token_count can arrive after a tool result. Keep that input
        # for the next call, while consuming the input that started this one.
        input_seconds = _timestamp_seconds(self.input_timestamp)
        if input_seconds is None or end_seconds is None or input_seconds <= end_seconds:
            self.input_timestamp = None
        self.start = self.end = None
        self.model = None


def _reconcile_usage_events(
    events: list[dict[str, Any]],
    target: dict[str, int],
) -> list[dict[str, Any]]:
    """Scale event metrics to an authoritative session total when needed."""
    if not events:
        return []

    input_allocations = _allocate_total(
        target["input_tokens"],
        [
            _as_int(event.get("input_tokens"))
            or _as_int(event.get("total_tokens"))
            for event in events
        ],
    )
    cached_allocations = _allocate_total(
        target["cached_input_tokens"], input_allocations
    )
    output_allocations = _allocate_total(
        target["output_tokens"],
        [_as_int(event.get("output_tokens")) for event in events],
    )
    reasoning_allocations = _allocate_total(
        target["reasoning_output_tokens"],
        [_as_int(event.get("reasoning_output_tokens")) for event in events],
    )
    cache_write_allocations = _allocate_total(
        target["cache_write_input_tokens"],
        [_as_int(event.get("cache_write_input_tokens")) for event in events],
    )
    total_allocations = _allocate_total(
        target["total_tokens"],
        [
            _as_int(event.get("total_tokens"))
            or _as_int(event.get("input_tokens")) + _as_int(event.get("output_tokens"))
            for event in events
        ],
    )

    reconciled: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        reconciled.append({
            **event,
            "timestamp": str(event.get("timestamp") or ""),
            "input_tokens": input_allocations[index],
            "cached_input_tokens": min(input_allocations[index], cached_allocations[index]),
            "output_tokens": output_allocations[index],
            "reasoning_output_tokens": reasoning_allocations[index],
            "cache_write_input_tokens": cache_write_allocations[index],
            "total_tokens": total_allocations[index],
            "metadata": {**event.get("metadata", {}), "tps_trustworthy": False},
        })
    return reconciled


def _build_usage_event(timestamp: Any, usage: Any) -> dict[str, Any] | None:
    """Normalize one incremental token-usage record for time-window slicing."""
    normalized = _normalize_usage(usage)
    if normalized is None:
        return None

    return {
        "timestamp": _to_iso_string(timestamp),
        **normalized,
    }


def _to_iso_string(ts: int | float | str | None) -> str:
    """Convert various timestamp formats to an ISO 8601 string."""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        # If timestamp is in milliseconds (> 100 billion), convert to seconds
        val = ts / 1000.0 if ts > 1e11 else float(ts)
        try:
            return datetime.fromtimestamp(val, timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return ""
    return str(ts).strip()


def _parse_rollout_file_uncached(
    file_path: Path,
    *,
    reject_malformed_tail: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Parse a single Codex rollout .jsonl file.

    Extracts incremental and cumulative token usage, timestamps, and call counts.
    """
    call_count = 0
    extracted_model: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    token_record_events: list[dict[str, Any]] = []
    event_msg_events: list[dict[str, Any]] = []
    event_msg_fallback_events: list[dict[str, Any]] = []
    token_record_count = 0
    previous_event_msg_cumulative: dict[str, int] | None = None
    modern_total = dict.fromkeys(_USAGE_FIELDS, 0)
    turn_totals: dict[str, dict[str, int]] = {}
    unscoped_total = dict.fromkeys(_USAGE_FIELDS, 0)
    has_thread_total = False
    has_inherited_history = False
    active_turn: str | None = None
    active_model: str | None = None
    modern_timing = _ResponseTiming()
    legacy_timing = _ResponseTiming()
    seen_response_ids: set[str] = set()
    read_succeeded = True
    malformed_record_seen = False

    try:
        decode_errors = "strict" if reject_malformed_tail else "ignore"
        with open(file_path, "r", encoding="utf-8", errors=decode_errors) as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                if reject_malformed_tail:
                    try:
                        record = json.loads(line_str)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        # Capture snapshots must never replace a complete
                        # persisted snapshot with totals parsed around a
                        # truncated or corrupt JSONL record. Keep this sticky:
                        # a later valid record does not repair missing usage.
                        malformed_record_seen = True
                        continue
                    if not isinstance(record, dict):
                        malformed_record_seen = True
                        continue
                    if not any(key in line_str for key in ("token", "session_meta", "response_item", "turn_context")):
                        continue
                else:
                    if not any(key in line_str for key in ("token", "session_meta", "response_item", "turn_context")):
                        continue
                    try:
                        record = json.loads(line_str)
                    except Exception:
                        continue

                if not isinstance(record, dict):
                    continue

                ts = record.get("timestamp")
                if ts:
                    if first_timestamp is None:
                        first_timestamp = str(ts)
                    last_timestamp = str(ts)

                rec_type = record.get("type")
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    payload = {}

                if rec_type == "turn_context":
                    active_turn = str(payload["turn_id"]) if payload.get("turn_id") else None
                    model_value = payload.get("model")
                    if model_value:
                        active_model = str(model_value).strip()
                elif rec_type == "response_item":
                    item_type = payload.get("type")
                    if item_type in ("function_call_output", "custom_tool_call_output") or (
                        item_type == "message" and payload.get("role") == "user"
                    ):
                        modern_timing.input_timestamp = legacy_timing.input_timestamp = ts
                    elif item_type in ("reasoning", "function_call", "custom_tool_call") or (
                        item_type == "message" and payload.get("role") == "assistant"
                    ):
                        for timing in (modern_timing, legacy_timing):
                            timing.observe_output(ts, active_model or extracted_model)

                if rec_type == "session_meta" and isinstance(payload, dict):
                    # Forks and paginated continuations may retain cumulative
                    # counters for history outside this file. Only its local
                    # responses belong to this capture fragment.
                    has_inherited_history = has_inherited_history or bool(
                        payload.get("forked_from_id") or payload.get("history_base")
                    )
                    prov = payload.get("provenance")
                    if isinstance(prov, dict):
                        extracted_model = prov.get("model")
                    if not extracted_model:
                        extracted_model = payload.get("model")
                    if extracted_model:
                        extracted_model = str(extracted_model).strip()

                # Format 1: token_usage_record
                elif rec_type == "token_usage_record":
                    response_id = payload.get("response_id")
                    if response_id and str(response_id) in seen_response_ids:
                        continue
                    if response_id:
                        seen_response_ids.add(str(response_id))
                    token_record_count += 1
                    u = payload.get("usage")
                    if not isinstance(u, dict):
                        u = {}
                    usage_event = _build_usage_event(ts, u)
                    modern_timing.finish(usage_event, ts)
                    if usage_event:
                        if response_id:
                            usage_event["event_id"] = str(response_id)
                        token_record_events.append(usage_event)

                    normalized_usage = _normalize_usage(u)
                    thread_total = _normalize_usage(payload.get("thread_token_usage"))
                    has_thread_total = has_thread_total or thread_total is not None
                    modern_total = _advance_usage_total(modern_total, normalized_usage, thread_total)
                    turn_id = payload.get("turn_id") or active_turn
                    if turn_id:
                        turn_key = str(turn_id)
                        turn_totals[turn_key] = _advance_usage_total(
                            turn_totals.get(turn_key, dict.fromkeys(_USAGE_FIELDS, 0)),
                            normalized_usage,
                            _normalize_usage(payload.get("turn_token_usage")),
                        )
                    else:
                        # Without a turn identity this counter cannot safely
                        # describe the session. Distinct responses still add.
                        unscoped_total = _advance_usage_total(unscoped_total, normalized_usage, None)

                # Format 2: event_msg with payload.type == 'token_count'
                elif rec_type == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        info = {}
                    last_u = info.get("last_token_usage")
                    if not isinstance(last_u, dict):
                        last_u = {}

                    tot_u = info.get("total_token_usage")
                    normalized_total = _normalize_usage(tot_u)
                    if normalized_total:
                        event_delta = _usage_delta(normalized_total, previous_event_msg_cumulative)
                        previous_event_msg_cumulative = normalized_total
                        usage_event = _build_usage_event(ts, event_delta)
                        if usage_event:
                            legacy_timing.finish(
                                usage_event,
                                trustworthy=event_delta == _normalize_usage(last_u),
                            )
                            event_msg_events.append(usage_event)
                    else:
                        usage_event = _build_usage_event(ts, last_u)
                        if usage_event:
                            legacy_timing.finish(usage_event)
                            event_msg_fallback_events.append(usage_event)
    except Exception as e:
        read_succeeded = False
        logger.warning("Error reading rollout file %s: %s", file_path, e)
    if reject_malformed_tail and malformed_record_seen:
        read_succeeded = False

    if not has_thread_total:
        modern_total = {
            field: unscoped_total[field] + sum(turn[field] for turn in turn_totals.values())
            for field in _USAGE_FIELDS
        }
    # These streams overlap: never add their totals. A legacy status can omit
    # responses already present in the modern stream, even when emitted later.
    # Cumulative counters may fill gaps, but cannot erase observed responses.
    target_totals = {
        field: max(modern_total[field], (previous_event_msg_cumulative or {}).get(field, 0))
        for field in _USAGE_FIELDS
    }
    if has_inherited_history and token_record_events:
        target_totals = _event_totals(token_record_events)
    if not any(target_totals.values()):
        target_totals = _event_totals(event_msg_fallback_events)
    input_tokens = target_totals["input_tokens"]
    cached_input_tokens = target_totals["cached_input_tokens"]
    output_tokens = target_totals["output_tokens"]
    reasoning_output_tokens = target_totals["reasoning_output_tokens"]
    cache_write_tokens = target_totals["cache_write_input_tokens"]
    total_tokens = target_totals["total_tokens"]

    # A rollout may contain both a token_usage_record and a token_count status
    # message for the same call. Prefer whichever event stream matches the
    # authoritative session total, then reconcile a partially-written stream.
    if any(target_totals.values()):
        token_totals = _event_totals(token_record_events)
        message_totals = _event_totals(event_msg_events)
        if token_record_events and token_totals == target_totals:
            usage_events = token_record_events
        elif event_msg_events and message_totals == target_totals:
            usage_events = event_msg_events
        else:
            # Preserve response identities when neither partial stream covers
            # the target; a shorter legacy stream must not collapse responses.
            usage_events = token_record_events or event_msg_events or event_msg_fallback_events
            usage_events = _reconcile_usage_events(usage_events, target_totals)
    else:
        usage_events = token_record_events or event_msg_fallback_events
    call_count = len(usage_events) or token_record_count

    uncached_input_tokens = max(0, input_tokens - cached_input_tokens)

    # Older or malformed rollouts may expose only a final total. Keep a
    # timestamped fallback event so the session can still participate in a
    # time filter without changing the all-time totals.
    if not usage_events and total_tokens > 0:
        usage_events.append({
            "timestamp": _to_iso_string(last_timestamp or first_timestamp),
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
            "cache_write_input_tokens": cache_write_tokens,
            "total_tokens": total_tokens,
        })

    result = {
        "call_count": max(1, call_count) if total_tokens > 0 else call_count,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "uncached_input_tokens": uncached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "cache_write_input_tokens": cache_write_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
        "start_time": first_timestamp,
        "end_time": last_timestamp,
        # turn_context carries the model selected for the concrete turn. When
        # it is present, prefer it over a session-level parent/default model.
        "model": active_model or extracted_model,
        "usage_events": usage_events,
    }
    return dict(result), read_succeeded


_ROLLOUT_PARSE_CACHE: ParsedFileCache[dict[str, Any]] = ParsedFileCache(max_entries=1024)


def _parse_rollout_file(file_path: Path) -> dict[str, Any]:
    """Parse a rollout file, reusing only stable successful reads."""
    return _ROLLOUT_PARSE_CACHE.parse(file_path, _parse_rollout_file_uncached)


def _codex_thread_session(
    thread_id: str,
    *,
    title: Any = None,
    model: Any = None,
    reasoning_effort: Any = None,
    tokens_used: Any = 0,
    created_at: Any = None,
    parsed: dict[str, Any] | None = None,
    source_path: str | Path | None = None,
) -> dict[str, Any]:
    """Assemble the dashboard-compatible session for one Codex thread.

    This is shared by the full-history parser and the post-turn capture hook so
    both paths use the same cumulative-token fallback, timestamps, call events,
    model selection, and pricing behavior.
    """
    t_id = str(thread_id or "")
    raw_title = str(title or "").strip()
    clean_title = next(
        (line.strip() for line in raw_title.splitlines() if line.strip()),
        f"Codex Session {t_id[:8]}",
    )
    selected_model = str(model or "").strip()
    db_tokens = _as_int(tokens_used)
    usage_events: list[dict[str, Any]] = []

    if parsed is not None:
        observed_model = str(parsed.get("model") or "").strip()
        if observed_model:
            selected_model = observed_model
        call_count = _as_int(parsed.get("call_count"))
        input_tokens = _as_int(parsed.get("input_tokens"))
        cached_input = _as_int(parsed.get("cached_input_tokens"))
        uncached_input = _as_int(parsed.get("uncached_input_tokens"))
        output = _as_int(parsed.get("output_tokens"))
        reasoning_output = _as_int(parsed.get("reasoning_output_tokens"))
        cache_write_tokens = _as_int(parsed.get("cache_write_input_tokens"))
        total_tokens = _as_int(parsed.get("total_tokens"))
        start_time = parsed.get("start_time")
        end_time = parsed.get("end_time")
        usage_events = list(parsed.get("usage_events") or [])
    else:
        call_count = 1 if db_tokens > 0 else 0
        input_tokens = int(db_tokens * 0.8)
        cached_input = 0
        uncached_input = input_tokens
        output = max(0, db_tokens - input_tokens)
        reasoning_output = 0
        cache_write_tokens = 0
        total_tokens = db_tokens
        start_time = None
        end_time = None

    # The state database tracks a less detailed total. Keep its legacy fallback
    # when a rollout is missing or did not retain token events.
    if total_tokens == 0 and db_tokens > 0:
        total_tokens = db_tokens
        input_tokens = int(db_tokens * 0.8)
        uncached_input = input_tokens
        cached_input = 0
        output = max(0, db_tokens - input_tokens)
        cache_write_tokens = 0
        call_count = max(1, call_count)

    created_at_iso = _to_iso_string(created_at) or (start_time or "")
    if not usage_events and total_tokens > 0:
        usage_events = [{
            "timestamp": created_at_iso,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning_output,
            "cache_write_input_tokens": cache_write_tokens,
            "total_tokens": total_tokens,
        }]

    priced = calculate_cost_strict(
        selected_model,
        uncached_input,
        cached_input,
        output,
        provider="codex",
        cache_write=cache_write_tokens,
        timestamp=created_at_iso or start_time,
    )
    cost = {
        "cost_cached_usd": float(priced.get("cost_cached_usd") or 0.0),
        "cost_uncached_usd": float(priced.get("cost_uncached_usd") or 0.0),
        "savings_usd": float(priced.get("savings_usd") or 0.0),
    }
    total_input = uncached_input + cached_input + cache_write_tokens
    cache_hit_rate = (
        round(cached_input / (uncached_input + cached_input) * 100.0, 2)
        if uncached_input + cached_input > 0 else 0.0
    )

    session = {
        "id": t_id,
        "title": clean_title,
        "tool": "codex",
        "model": selected_model,
        "reasoning_effort": reasoning_effort,
        "call_count": call_count,
        "uncached_input": uncached_input,
        "cached_input": cached_input,
        "total_input": total_input,
        "cache_write": cache_write_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_write_input_tokens": cache_write_tokens,
        "output": output,
        "reasoning_output": reasoning_output,
        "total_tokens": total_tokens,
        "cache_hit_rate": cache_hit_rate,
        "cost_cached_usd": cost["cost_cached_usd"],
        "cost_uncached_usd": cost["cost_uncached_usd"],
        "savings_usd": cost["savings_usd"],
        "created_at": created_at_iso,
        "start_time": start_time,
        "end_time": end_time,
        "usage_events": usage_events,
    }
    if source_path is not None:
        session["metadata"] = {"rollout_path": str(source_path)}
    return session


def _strip_codex_storage_prefix(session_id: Any) -> str:
    value = str(session_id or "")
    return value[len("codex:"):] if value.startswith("codex:") else value


def _canonical_path(value: Any) -> str | None:
    if not value:
        return None
    try:
        return str(Path(str(value)).expanduser().resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _capture_file_metadata(path: Path) -> dict[str, Any] | None:
    """Build private, compact identity metadata for a captured source file."""
    try:
        resolved = path.expanduser().resolve()
        stat = resolved.stat()
    except (OSError, RuntimeError):
        return None
    return {
        "capture_source_hash": hashlib.sha256(str(resolved).encode("utf-8")).hexdigest(),
        "capture_mtime_ns": stat.st_mtime_ns,
        "capture_ctime_ns": stat.st_ctime_ns,
        "capture_size": stat.st_size,
    }


def _read_persisted_codex_sessions(
    codex_dir: Path,
    *,
    requested_root: str | Path | None,
    usage_db_path: str | Path | None,
) -> list[UsageSession]:
    """Read only Codex-owned rows from the shared usage database.

    A custom Codex root does not implicitly consult the user's global shared
    database. Tests and alternate installations can opt in with usage_db_path.
    """
    if usage_db_path is None:
        default_root = Path.home() / ".codex"
        try:
            is_default_root = codex_dir.resolve() == default_root.resolve()
        except (OSError, RuntimeError):
            is_default_root = False
        if requested_root is not None and not is_default_root:
            return []
    try:
        from src.usage_store import read_usage_sessions

        return [
            session
            for session in read_usage_sessions("codex", db_path=usage_db_path)
            if isinstance(session, UsageSession)
            and str(session.provider or session.tool).casefold() == "codex"
        ]
    except Exception as exc:
        logger.warning("Error reading persisted Codex usage: %s", exc)
        return []


def _codex_capture_is_enabled(
    codex_dir: Path,
    *,
    requested_root: str | Path | None,
    usage_db_path: str | Path | None,
) -> bool:
    """Return whether Codex has completed the one-time shared-DB backfill."""
    if usage_db_path is None:
        default_root = Path.home() / ".codex"
        try:
            is_default_root = codex_dir.resolve() == default_root.resolve()
        except (OSError, RuntimeError):
            is_default_root = False
        if requested_root is not None and not is_default_root:
            return False
    try:
        from src.usage_store import is_provider_capture_enabled

        return bool(is_provider_capture_enabled("codex", db_path=usage_db_path))
    except (ImportError, AttributeError):
        return False
    except Exception as exc:
        logger.warning("Error reading Codex capture state: %s", exc)
        return False


def _stored_session_to_legacy(session: UsageSession) -> dict[str, Any]:
    raw = session.to_legacy_dict(include_events=True)
    raw["id"] = _strip_codex_storage_prefix(session.id)
    raw["tool"] = "codex"
    raw["provider"] = "codex"
    raw["cache_write"] = session.usage.cache_write_tokens
    raw["cache_write_tokens"] = session.usage.cache_write_tokens
    raw["cache_write_input_tokens"] = session.usage.cache_write_tokens
    # The dashboard's legacy Codex event vocabulary uses this field for the
    # provider's separate cache-write input component.
    raw["usage_events"] = [
        {
            **event.to_legacy_dict(),
            "cache_write_input_tokens": event.usage.cache_write_tokens,
        }
        for event in session.events
    ]
    return raw


def parse_codex_usage(
    codex_dir: str | Path | None = None,
    *,
    usage_db_path: str | Path | None = None,
    include_persisted: bool = True,
) -> dict[str, Any]:
    """Parse Codex usage metrics from state_5.sqlite and rollout-*.jsonl files.

    Args:
        codex_dir: Base directory for Codex data. Defaults to ~/.codex.
        usage_db_path: Optional explicit shared usage database path. Custom
            Codex roots never consult the global shared database implicitly.
        include_persisted: Set to false for an explicit one-time local capture.

    Returns:
        Dictionary with keys:
            - 'tool': 'codex'
            - 'summary': high-level odometer totals
            - 'models': per-model breakdown list
            - 'timeline': daily aggregated metrics list
            - 'sessions': individual session details list
    """
    requested_root = codex_dir
    base_dir = Path(codex_dir).expanduser() if codex_dir else Path.home() / ".codex"
    capture_enabled = (
        _codex_capture_is_enabled(
            base_dir,
            requested_root=requested_root,
            usage_db_path=usage_db_path,
        )
        if include_persisted else False
    )
    # Backfill commits all provider rows before setting the capture flag. Read
    # the flag first so an activation observed here always precedes a complete
    # database snapshot, never a partially-read pre-activation row set.
    persisted_contracts = (
        _read_persisted_codex_sessions(
            base_dir,
            requested_root=requested_root,
            usage_db_path=usage_db_path,
        )
        if include_persisted else []
    )
    persisted_by_id = {
        _strip_codex_storage_prefix(session.id).casefold(): session
        for session in persisted_contracts
    }
    persisted_rollout_paths = {
        _canonical_path(session.metadata.get(key))
        for session in persisted_contracts
        for key in ("rollout_path", "source_path")
        if session.metadata.get(key)
    }
    persisted_rollout_paths.discard(None)

    empty_result: dict[str, Any] = {
        "tool": "codex",
        "summary": {
            "total_tokens": 0,
            "uncached_input": 0,
            "cached_input": 0,
            "total_input": 0,
            "cache_write": 0,
            "output": 0,
            "reasoning_output": 0,
            "cost_cached_usd": 0.0,
            "cost_uncached_usd": 0.0,
            "savings_usd": 0.0,
            "total_cost_usd": 0.0,
            "cache_hit_rate": 0.0,
            "session_count": 0,
            "call_count": 0,
        },
        "models": [],
        "timeline": [],
        "sessions": [],
    }

    if not base_dir.exists() and not persisted_contracts:
        _ROLLOUT_PARSE_CACHE.retain_paths(())
        return empty_result

    # 1. Discover all rollout files on disk
    rollout_files_by_path: dict[str, Path] = {}
    rollout_files_by_id: dict[str, Path] = {}

    for p in (() if capture_enabled else base_dir.glob("**/*rollout-*.jsonl")):
        rollout_files_by_path[str(p.resolve())] = p
        # Filename pattern: rollout-<date>-<thread_id>.jsonl
        fname = p.name
        # Match trailing UUID format if present
        match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", fname)
        if match:
            rollout_files_by_id[match.group(1).lower()] = p

    # 2. Read threads from state_5.sqlite
    db_path = base_dir / "state_5.sqlite"
    threads_data: list[dict[str, Any]] = []

    if db_path.exists() and not capture_enabled:
        conn: sqlite3.Connection | None = None
        cursor: sqlite3.Cursor | None = None
        try:
            try:
                uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
            except Exception:
                conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("PRAGMA query_only = ON;")
            cursor.execute(
                """
                SELECT id, title, model, reasoning_effort, tokens_used, created_at, rollout_path
                FROM threads
                """
            )
            for row in cursor.fetchall():
                threads_data.append(dict(row))
        except Exception as e:
            logger.warning("Error querying Codex SQLite database %s: %s", db_path, e)
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as e:
                    logger.debug("Failed to close cursor: %s", e)
            if conn is not None:
                try:
                    conn.close()
                except Exception as e:
                    logger.debug("Failed to close connection: %s", e)

    live_rollout_paths = set(rollout_files_by_path.values())
    live_rollout_paths.update(
        Path(str(thread.get("rollout_path"))).expanduser()
        for thread in threads_data
        if thread.get("rollout_path") and Path(str(thread["rollout_path"])).is_file()
    )
    _ROLLOUT_PARSE_CACHE.retain_paths(live_rollout_paths)

    sessions: list[dict[str, Any]] = [
        _stored_session_to_legacy(session)
        for session in persisted_contracts
    ]
    processed_rollout_paths: set[str] = set()

    # 3. Match DB threads with rollout data
    for t in threads_data:
        t_id = str(t.get("id") or "")
        db_rollout_path = str(t.get("rollout_path") or "")
        db_tokens = _as_int(t.get("tokens_used"))

        matched_rollout_path: Path | None = None
        if db_rollout_path and os.path.exists(db_rollout_path):
            matched_rollout_path = Path(db_rollout_path)
        elif _canonical_path(db_rollout_path) in rollout_files_by_path:
            matched_rollout_path = rollout_files_by_path[_canonical_path(db_rollout_path)]
        elif t_id.lower() in rollout_files_by_id:
            matched_rollout_path = rollout_files_by_id[t_id.lower()]

        canonical_rollout_path = _canonical_path(matched_rollout_path)
        if matched_rollout_path:
            if canonical_rollout_path:
                processed_rollout_paths.add(canonical_rollout_path)

        # A captured row already contains the authoritative totals and event
        # timestamps for this thread. Keep it even when its rollout is gone,
        # and never append a second session from the local JSONL copy.
        if t_id.casefold() in persisted_by_id or canonical_rollout_path in persisted_rollout_paths:
            continue

        parsed: dict[str, Any] | None = None
        if matched_rollout_path:
            parsed = _parse_rollout_file(matched_rollout_path)
        sessions.append(_codex_thread_session(
            t_id,
            title=t.get("title"),
            model=t.get("model"),
            reasoning_effort=t.get("reasoning_effort"),
            tokens_used=db_tokens,
            created_at=t.get("created_at"),
            parsed=parsed,
            source_path=matched_rollout_path,
        ))

    # 4. Handle any orphan rollout files not indexed in threads table
    for rpath_str, rpath in rollout_files_by_path.items():
        if rpath_str in processed_rollout_paths or rpath_str in persisted_rollout_paths:
            continue
        filename_match = re.search(
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            rpath.name,
        )
        if filename_match and filename_match.group(1).casefold() in persisted_by_id:
            continue
        parsed = _parse_rollout_file(rpath)
        if parsed["total_tokens"] == 0 and parsed["call_count"] == 0:
            continue
        # Extract UUID or basename
        orphan_id = filename_match.group(1) if filename_match else rpath.stem
        session = _codex_thread_session(
            orphan_id,
            title=f"Codex Session {orphan_id[:8]}",
            parsed=parsed,
            source_path=rpath,
        )
        sessions.append(session)

    # Sort sessions by created_at descending
    sessions.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)

    # 5. Compute per-model stats
    models_dict: dict[str, dict[str, Any]] = {}
    for s in sessions:
        m = s["model"]
        if m not in models_dict:
            models_dict[m] = {
                "model": m,
                "tool": "codex",
                "call_count": 0,
                "session_count": 0,
                "uncached_input": 0,
                "cached_input": 0,
                "total_input": 0,
                "cache_write": 0,
                "output": 0,
                "reasoning_output": 0,
                "total_tokens": 0,
                "cache_hit_rate": 0.0,
                "est_cost_cached_usd": 0.0,
                "est_cost_uncached_usd": 0.0,
                "est_savings_usd": 0.0,
            }
        entry = models_dict[m]
        entry["call_count"] += s["call_count"]
        entry["session_count"] += 1
        entry["uncached_input"] += s["uncached_input"]
        entry["cached_input"] += s["cached_input"]
        entry["total_input"] += s["total_input"]
        entry["cache_write"] += s.get("cache_write", 0)
        entry["output"] += s["output"]
        entry["reasoning_output"] += s["reasoning_output"]
        entry["total_tokens"] += s["total_tokens"]
        entry["est_cost_cached_usd"] += s["cost_cached_usd"]
        entry["est_cost_uncached_usd"] += s["cost_uncached_usd"]
        entry["est_savings_usd"] += s["savings_usd"]

    for entry in models_dict.values():
        cacheable_input = entry["uncached_input"] + entry["cached_input"]
        entry["cache_hit_rate"] = round((entry["cached_input"] / cacheable_input * 100.0), 2) if cacheable_input > 0 else 0.0
        entry["est_cost_cached_usd"] = round(entry["est_cost_cached_usd"], 6)
        entry["est_cost_uncached_usd"] = round(entry["est_cost_uncached_usd"], 6)
        entry["est_savings_usd"] = round(entry["est_savings_usd"], 6)

    models_list = sorted(models_dict.values(), key=lambda x: x["total_tokens"], reverse=True)

    # 6. Compute timeline stats grouped by date YYYY-MM-DD
    timeline_dict: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "date": "",
        "uncached_input": 0,
        "cached_input": 0,
        "total_input": 0,
        "cache_write": 0,
        "output": 0,
        "reasoning_output": 0,
        "total_tokens": 0,
        "call_count": 0,
        "session_count": 0,
        "cost_cached_usd": 0.0,
        "cost_uncached_usd": 0.0,
        "savings_usd": 0.0,
    })

    for s in sessions:
        created = s.get("created_at") or ""
        date_str = created[:10] if len(created) >= 10 and created[4] == "-" and created[7] == "-" else "unknown"
        if date_str == "unknown":
            continue
        day = timeline_dict[date_str]
        day["date"] = date_str
        day["uncached_input"] += s["uncached_input"]
        day["cached_input"] += s["cached_input"]
        day["total_input"] += s["total_input"]
        day["cache_write"] += s.get("cache_write", 0)
        day["output"] += s["output"]
        day["reasoning_output"] += s["reasoning_output"]
        day["total_tokens"] += s["total_tokens"]
        day["call_count"] += s["call_count"]
        day["session_count"] += 1
        day["cost_cached_usd"] += s["cost_cached_usd"]
        day["cost_uncached_usd"] += s["cost_uncached_usd"]
        day["savings_usd"] += s["savings_usd"]

    timeline_list = []
    for date_key in sorted(timeline_dict.keys()):
        d = timeline_dict[date_key]
        d["cost_cached_usd"] = round(d["cost_cached_usd"], 6)
        d["cost_uncached_usd"] = round(d["cost_uncached_usd"], 6)
        d["savings_usd"] = round(d["savings_usd"], 6)
        timeline_list.append(d)

    # 7. Summary odometer totals
    tot_uncached = sum(s["uncached_input"] for s in sessions)
    tot_cached = sum(s["cached_input"] for s in sessions)
    tot_cache_write = sum(s.get("cache_write", 0) for s in sessions)
    tot_input = tot_uncached + tot_cached + tot_cache_write
    tot_output = sum(s["output"] for s in sessions)
    tot_reasoning = sum(s["reasoning_output"] for s in sessions)
    tot_tokens = sum(s["total_tokens"] for s in sessions)
    tot_cost_cached = round(sum(s["cost_cached_usd"] for s in sessions), 6)
    tot_cost_uncached = round(sum(s["cost_uncached_usd"] for s in sessions), 6)
    tot_savings = round(sum(s["savings_usd"] for s in sessions), 6)
    summary_cache_hit_rate = round((tot_cached / (tot_uncached + tot_cached) * 100.0), 2) if tot_uncached + tot_cached > 0 else 0.0

    summary = {
        "total_tokens": tot_tokens,
        "uncached_input": tot_uncached,
        "cached_input": tot_cached,
        "total_input": tot_input,
        "cache_write": tot_cache_write,
        "output": tot_output,
        "reasoning_output": tot_reasoning,
        "cost_cached_usd": tot_cost_cached,
        "cost_uncached_usd": tot_cost_uncached,
        "savings_usd": tot_savings,
        "total_cost_usd": tot_cost_cached,
        "cache_hit_rate": summary_cache_hit_rate,
        "session_count": len(sessions),
        "call_count": sum(s["call_count"] for s in sessions),
    }

    return {
        "tool": "codex",
        "summary": summary,
        "models": models_list,
        "timeline": timeline_list,
        "sessions": sessions,
    }


def _legacy_sessions_to_contract(
    result: dict[str, Any],
) -> list[UsageSession]:
    """Convert the compatibility parser's sessions to normalized contracts.

    Keeping this conversion at the adapter boundary means the mature rollout
    parsing and reconciliation code above can continue to handle malformed,
    partially-written, and historical Codex logs exactly as it does today.
    Cost values are retained with their existing estimated-cost provenance by
    ``UsageSession.from_legacy_dict``.
    """
    sessions: list[UsageSession] = []
    for raw_session in result.get("sessions", []):
        if not isinstance(raw_session, dict):
            continue
        session = UsageSession.from_legacy_dict(raw_session)
        session.provider = "codex"
        session.tool = "codex"
        # Session-level model is authoritative for Codex rollout records. A
        # legacy event does not always include it, so fill it for consumers
        # that aggregate events by model.
        for event in session.events:
            if event.model is None:
                event.model = session.model
        sessions.append(session)
    return sessions


def _read_codex_thread_metadata(codex_dir: Path, thread_id: str) -> dict[str, Any]:
    """Read metadata for one Codex thread without scanning its history."""
    db_path = codex_dir / "state_5.sqlite"
    if not db_path.is_file():
        return {}

    conn: sqlite3.Connection | None = None
    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("PRAGMA query_only = ON")
        cursor.execute(
            """
            SELECT id, title, model, reasoning_effort, tokens_used, created_at,
                   rollout_path
            FROM threads
            WHERE id = ?
            LIMIT 1
            """,
            (str(thread_id),),
        )
        row = cursor.fetchone()
        return dict(row) if row is not None else {}
    except (OSError, sqlite3.Error) as exc:
        logger.debug("Could not read Codex thread metadata from %s: %s", db_path, exc)
        return {}
    finally:
        if conn is not None:
            conn.close()


def _capture_codex_session_for_capture(
    transcript_path: str | Path,
    session_id: str,
    model: str | None = None,
    codex_dir: str | Path | None = None,
) -> tuple[UsageSession | None, str]:
    """Extract one completed transcript for the native Codex capture hook.

    This reads only ``transcript_path`` and, when available, one matching row
    from ``state_5.sqlite``. A model observed in the transcript takes
    precedence over the caller's fallback model and thread metadata.
    """
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return None, "missing"

    base_dir = Path(codex_dir).expanduser() if codex_dir else Path.home() / ".codex"
    parsed: dict[str, Any] | None = None
    stable_metadata: dict[str, Any] | None = None
    for attempt in range(3):
        before = _capture_file_metadata(path)
        if before is None:
            return None, "missing"
        try:
            parsed, read_succeeded = _parse_rollout_file_uncached(
                path,
                reject_malformed_tail=True,
            )
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("Could not parse captured Codex transcript %s: %s", path, exc)
            return None, "unreadable"
        after = _capture_file_metadata(path)
        if before == after:
            stable_metadata = after
            if not read_succeeded:
                return None, "unreadable"
            break
        if attempt < 2:
            time.sleep(0.03)
    if stable_metadata is None or parsed is None:
        return None, "unstable"

    # Do not use state_5.sqlite's coarse aggregate to fabricate a complete
    # native snapshot. That could replace a richer event history already in
    # SQLite when the transcript is empty or has not flushed token records.
    if _as_int(parsed.get("total_tokens")) <= 0:
        return None, "empty"

    thread_metadata = _read_codex_thread_metadata(base_dir, session_id)
    session_data = _codex_thread_session(
        session_id,
        title=thread_metadata.get("title"),
        model=model or thread_metadata.get("model"),
        reasoning_effort=thread_metadata.get("reasoning_effort"),
        tokens_used=0,
        created_at=thread_metadata.get("created_at"),
        parsed=parsed,
        source_path=path,
    )
    if _as_int(session_data.get("total_tokens")) <= 0:
        return None, "empty"

    session = UsageSession.from_legacy_dict(session_data)
    session.metadata.pop("rollout_path", None)
    session.metadata.update(stable_metadata)
    session.provider = "codex"
    session.tool = "codex"
    for event in session.events:
        if event.model is None:
            event.model = session.model
    return session, "ok"


def extract_codex_session_for_capture(
    transcript_path: str | Path,
    session_id: str,
    model: str | None = None,
    codex_dir: str | Path | None = None,
) -> UsageSession | None:
    """Extract one complete, stable transcript for the native Codex hook."""
    session, _status = _capture_codex_session_for_capture(
        transcript_path,
        session_id,
        model=model,
        codex_dir=codex_dir,
    )
    return session


def _codex_capture_event_signature(event: Any) -> tuple[Any, ...]:
    usage = event.usage
    timestamp = event.timestamp.isoformat() if event.timestamp is not None else ""
    return (
        event.event_id or "",
        timestamp,
        event.model or "",
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        usage.reasoning_output_tokens,
        usage.cache_write_tokens,
        usage.cache_write_5m_tokens,
        usage.cache_write_1h_tokens,
    )


def _ensure_codex_capture_fragments_disjoint(sessions: list[UsageSession]) -> None:
    """Reject same-thread fragments that cannot be safely added together."""
    from datetime import datetime

    for index, left in enumerate(sessions):
        left_signatures = {_codex_capture_event_signature(event) for event in left.events}
        left_by_id = {
            event.event_id: _codex_capture_event_signature(event)
            for event in left.events
            if event.event_id
        }
        left_times = [event.timestamp for event in left.events if event.timestamp is not None]
        left_range = (min(left_times), max(left_times)) if left_times else None
        for right in sessions[index + 1:]:
            right_signatures = {_codex_capture_event_signature(event) for event in right.events}
            right_by_id = {
                event.event_id: _codex_capture_event_signature(event)
                for event in right.events
                if event.event_id
            }
            for event_id in left_by_id.keys() & right_by_id.keys():
                if left_by_id[event_id] != right_by_id[event_id]:
                    raise RuntimeError(
                        f"Codex fragments disagree about response {event_id!r}; refusing an unsafe merge"
                    )

            right_times = [event.timestamp for event in right.events if event.timestamp is not None]
            right_range = (min(right_times), max(right_times)) if right_times else None
            if left_range and right_range:
                # UsageEvent timestamps are normalized to aware UTC datetimes.
                assert isinstance(left_range[0], datetime) and isinstance(right_range[0], datetime)
                ranges_overlap = left_range[0] <= right_range[1] and right_range[0] <= left_range[1]
                if ranges_overlap and left_signatures != right_signatures:
                    raise RuntimeError(
                        "Codex same-thread rollout fragments overlap in time; refusing an unsafe merge"
                    )


def _merge_codex_capture_fragments(
    thread_id: str,
    fragments: list[UsageSession],
    *,
    thread: dict[str, Any] | None = None,
) -> UsageSession:
    """Merge stable per-file snapshots while retaining fragment provenance."""
    from src.parsers.aggregator import _merge_usage_sessions

    _ensure_codex_capture_fragments_disjoint(fragments)
    metadata_by_hash: dict[str, dict[str, int]] = {}
    primary_metadata: dict[str, Any] = {}
    for fragment in fragments:
        source_hash = str(fragment.metadata.get("capture_source_hash") or "")
        if not source_hash:
            raise RuntimeError("Codex capture fragment lacks a stable source fingerprint")
        revision = {
            "capture_mtime_ns": _as_int(fragment.metadata.get("capture_mtime_ns")),
            "capture_ctime_ns": _as_int(fragment.metadata.get("capture_ctime_ns")),
            "capture_size": _as_int(fragment.metadata.get("capture_size")),
        }
        metadata_by_hash[source_hash] = revision
        if not primary_metadata:
            primary_metadata = dict(fragment.metadata)
        for event in fragment.events:
            event.metadata["capture_source_hash"] = source_hash

    merged_sessions = _merge_usage_sessions(fragments)
    if not merged_sessions:
        raise RuntimeError(f"Could not merge Codex thread {thread_id!r}")
    merged = merged_sessions[0]
    merged.id = thread_id
    merged.provider = "codex"
    merged.tool = "codex"
    merged.metadata.update(primary_metadata)
    merged.metadata["capture_sources"] = metadata_by_hash
    if len(metadata_by_hash) > 1:
        merged.metadata["capture_quality"] = "merged-fragments"

    if thread:
        if thread.get("title"):
            merged.title = str(thread["title"])
        if thread.get("reasoning_effort"):
            merged.reasoning_effort = str(thread["reasoning_effort"])
        if thread.get("created_at"):
            parsed_created_at = UsageSession.from_legacy_dict({
                "id": thread_id,
                "tool": "codex",
                "created_at": thread.get("created_at"),
            }).created_at
            if parsed_created_at is not None:
                merged.created_at = parsed_created_at
    return merged


def extract_all_codex_sessions_for_capture(
    codex_dir: str | Path | None = None,
) -> list[UsageSession]:
    """Extract local Codex history for an explicit one-time database capture.

    This deliberately bypasses the shared usage database so a migration cannot
    re-import its own rows. Each transcript is captured with its own stable
    before/after fingerprint; one unstable or unreadable file aborts the
    backfill so the caller cannot enable DB-only mode with incomplete history.
    It performs no writes.
    """
    base_dir = Path(codex_dir).expanduser() if codex_dir else Path.home() / ".codex"
    candidate_paths: dict[str, Path] = {}
    candidate_ids_by_path: dict[str, str] = {}
    candidate_paths_by_id: defaultdict[str, list[str]] = defaultdict(list)

    for path in base_dir.glob("**/*rollout-*.jsonl"):
        canonical = str(path.resolve())
        candidate_paths[canonical] = path
        filename_match = re.search(
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            path.name,
        )
        session_id = filename_match.group(1) if filename_match else path.stem
        candidate_ids_by_path[canonical] = session_id
        candidate_paths_by_id[session_id.casefold()].append(canonical)

    thread_rows_by_id: dict[str, dict[str, Any]] = {}
    state_db = base_dir / "state_5.sqlite"
    if state_db.is_file():
        connection: sqlite3.Connection | None = None
        try:
            uri = f"file:{state_db.resolve().as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                """SELECT id, title, model, reasoning_effort, tokens_used,
                          created_at, rollout_path
                   FROM threads"""
            ).fetchall()
            for row in rows:
                thread = dict(row)
                thread_id = str(thread.get("id") or "")
                if thread_id:
                    thread_rows_by_id[thread_id.casefold()] = thread
        except sqlite3.Error as exc:
            logger.warning("Could not enumerate Codex thread rollouts in %s: %s", state_db, exc)
            raise RuntimeError(f"Could not enumerate Codex source transcripts: {state_db}") from exc
        finally:
            if connection is not None:
                connection.close()

    # Include state_5's canonical path first, followed by every same-thread
    # rollout fragment. Per-file parser reconciliation handles cumulative
    # records within each file; the established session merger deduplicates
    # shared response IDs/fingerprints across files.
    sessions_by_id: dict[str, UsageSession] = {}
    all_thread_keys = set(thread_rows_by_id) | set(candidate_paths_by_id)
    for thread_key in sorted(all_thread_keys):
        thread = thread_rows_by_id.get(thread_key)
        thread_id = str(thread.get("id") or "") if thread else ""
        state_rollout = _canonical_path(thread.get("rollout_path")) if thread else None
        source_paths = list(candidate_paths_by_id.get(thread_key, []))
        if state_rollout and Path(state_rollout).is_file():
            candidate_paths.setdefault(state_rollout, Path(state_rollout))
            candidate_ids_by_path[state_rollout] = thread_id
            source_paths = [state_rollout, *(path for path in source_paths if path != state_rollout)]
        else:
            source_paths.sort()
        fragments: list[UsageSession] = []
        for canonical in source_paths:
            transcript_path = candidate_paths[canonical]
            source_session_id = thread_id or candidate_ids_by_path.get(canonical, thread_key)
            session, status = _capture_codex_session_for_capture(
                transcript_path,
                source_session_id,
                model=thread.get("model") if thread else None,
                codex_dir=base_dir,
            )
            if status == "empty":
                continue
            if status != "ok" or session is None:
                raise RuntimeError(
                    f"Could not capture Codex transcript {transcript_path}: {status}"
                )
            fragments.append(session)

        if fragments:
            sessions_by_id[thread_key] = _merge_codex_capture_fragments(
                thread_id or fragments[0].id,
                fragments,
                thread=thread,
            )
            continue

        if thread and _as_int(thread.get("tokens_used")) > 0:
            # Older dashboard imports showed state_5's coarse token estimate
            # for threads whose rollout was missing or had no usage records.
            # Keep that visible total in the one-time backfill, explicitly
            # marked so it can never replace a later exact transcript snapshot.
            summary_data = _codex_thread_session(
                thread_id,
                title=thread.get("title"),
                model=thread.get("model"),
                reasoning_effort=thread.get("reasoning_effort"),
                tokens_used=thread.get("tokens_used"),
                created_at=thread.get("created_at"),
            )
            summary_session = _legacy_sessions_to_contract({"sessions": [summary_data]})[0]
            summary_session.metadata["capture_quality"] = "state-summary"
            sessions_by_id[thread_key] = summary_session

    sessions = list(sessions_by_id.values())

    sessions.sort(
        key=lambda session: str(session.created_at or session.start_time or ""),
        reverse=True,
    )
    return sessions


class CodexSource:
    """Provider adapter for Codex state and rollout files.

    ``extract_sessions`` is the provider-neutral entry point. The legacy
    ``parse_codex_usage`` function remains available for existing API callers.
    """

    key = "codex"
    provider = key
    aliases = ("openai-codex", "codex-cli")
    default_source_path = Path.home() / ".codex"
    default_root = default_source_path
    # ``default_path`` is a convenient spelling for registry/discovery code
    # that treats source paths generically.
    default_path = default_source_path

    def __init__(self, usage_db_path: str | Path | None = None) -> None:
        self.usage_db_path = usage_db_path

    def extract_sessions(self, root: str | Path | None = None) -> list[UsageSession]:
        source_root = root if root is not None else self.default_source_path
        return _legacy_sessions_to_contract(
            parse_codex_usage(source_root, usage_db_path=self.usage_db_path)
        )


# Explicit alias for callers that name adapters after the provider/tool.
CodexUsageSource = CodexSource
