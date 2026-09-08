"""Parser for Codex sessions and rollout logs."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import UsageSession
from ..pricing import calculate_cost

logger = logging.getLogger(__name__)

_ROLLOUT_PARSE_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
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
        normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
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
    safe_weights = [max(0, int(weight)) for weight in weights]
    weight_sum = sum(safe_weights)
    if total <= 0 or weight_sum <= 0:
        return [0] * len(weights)

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
            "timestamp": str(event.get("timestamp") or ""),
            "input_tokens": input_allocations[index],
            "cached_input_tokens": min(input_allocations[index], cached_allocations[index]),
            "output_tokens": output_allocations[index],
            "reasoning_output_tokens": reasoning_allocations[index],
            "total_tokens": total_allocations[index],
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


def _parse_rollout_file(file_path: Path) -> dict[str, Any]:
    """Parse a single Codex rollout .jsonl file.

    Extracts incremental and cumulative token usage, timestamps, and call counts.
    """
    cache_key = None
    try:
        stat = file_path.stat()
        cache_key = (str(file_path.resolve()), stat.st_mtime_ns, stat.st_size)
        if cache_key in _ROLLOUT_PARSE_CACHE:
            return dict(_ROLLOUT_PARSE_CACHE[cache_key])
    except OSError as e:
        logger.debug("Failed to stat rollout file %s: %s", file_path, e)

    call_count = 0
    extracted_model: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    token_record_events: list[dict[str, Any]] = []
    event_msg_events: list[dict[str, Any]] = []
    event_msg_fallback_events: list[dict[str, Any]] = []
    token_record_count = 0
    previous_event_msg_cumulative: dict[str, int] | None = None
    last_cumulative: dict[str, int] | None = None

    has_error = False
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                if "token" not in line_str and "session_meta" not in line_str:
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

                if rec_type == "session_meta" and isinstance(payload, dict):
                    prov = payload.get("provenance")
                    if isinstance(prov, dict):
                        extracted_model = prov.get("model")
                    if not extracted_model:
                        extracted_model = payload.get("model")
                    if extracted_model:
                        extracted_model = str(extracted_model).strip()

                # Format 1: token_usage_record
                elif rec_type == "token_usage_record":
                    token_record_count += 1
                    u = payload.get("usage")
                    if not isinstance(u, dict):
                        u = {}
                    usage_event = _build_usage_event(ts, u)
                    if usage_event:
                        token_record_events.append(usage_event)

                    thread_cum = payload.get("thread_token_usage") or payload.get("turn_token_usage")
                    normalized_thread_cum = _normalize_usage(thread_cum)
                    if normalized_thread_cum:
                        last_cumulative = normalized_thread_cum

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
                            event_msg_events.append(usage_event)
                        last_cumulative = normalized_total
                    else:
                        usage_event = _build_usage_event(ts, last_u)
                        if usage_event:
                            event_msg_fallback_events.append(usage_event)
    except Exception as e:
        has_error = True
        logger.warning("Error reading rollout file %s: %s", file_path, e)

    if last_cumulative:
        input_tokens = last_cumulative["input_tokens"]
        cached_input_tokens = last_cumulative["cached_input_tokens"]
        output_tokens = last_cumulative["output_tokens"]
        reasoning_output_tokens = last_cumulative["reasoning_output_tokens"]
        total_tokens = last_cumulative["total_tokens"]
    else:
        usage_events_for_totals = token_record_events or event_msg_fallback_events
        input_tokens = sum(event["input_tokens"] for event in usage_events_for_totals)
        cached_input_tokens = sum(event["cached_input_tokens"] for event in usage_events_for_totals)
        output_tokens = sum(event["output_tokens"] for event in usage_events_for_totals)
        reasoning_output_tokens = sum(event["reasoning_output_tokens"] for event in usage_events_for_totals)
        total_tokens = sum(event["total_tokens"] for event in usage_events_for_totals)

    # A rollout may contain both a token_usage_record and a token_count status
    # message for the same call. Prefer whichever event stream matches the
    # authoritative session total, then reconcile a partially-written stream.
    target_totals = {
        field: _as_int(value)
        for field, value in last_cumulative.items()
    } if last_cumulative else None
    if target_totals:
        token_totals = _event_totals(token_record_events)
        message_totals = _event_totals(event_msg_events)
        if token_record_events and token_totals == target_totals:
            usage_events = token_record_events
        elif event_msg_events and message_totals == target_totals:
            usage_events = event_msg_events
        else:
            usage_events = event_msg_events or token_record_events or event_msg_fallback_events
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
            "total_tokens": total_tokens,
        })

    result = {
        "call_count": max(1, call_count) if total_tokens > 0 else call_count,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "uncached_input_tokens": uncached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total_tokens,
        "start_time": first_timestamp,
        "end_time": last_timestamp,
        "model": extracted_model,
        "usage_events": usage_events,
    }
    if cache_key is not None and not has_error:
        _ROLLOUT_PARSE_CACHE[cache_key] = result
    return dict(result)


def parse_codex_usage(codex_dir: str | Path | None = None) -> dict[str, Any]:
    """Parse Codex usage metrics from state_5.sqlite and rollout-*.jsonl files.

    Args:
        codex_dir: Base directory for Codex data. Defaults to ~/.codex.

    Returns:
        Dictionary with keys:
            - 'tool': 'codex'
            - 'summary': high-level odometer totals
            - 'models': per-model breakdown list
            - 'timeline': daily aggregated metrics list
            - 'sessions': individual session details list
    """
    base_dir = Path(codex_dir).expanduser() if codex_dir else Path.home() / ".codex"

    empty_result: dict[str, Any] = {
        "tool": "codex",
        "summary": {
            "total_tokens": 0,
            "uncached_input": 0,
            "cached_input": 0,
            "total_input": 0,
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

    if not base_dir.exists():
        return empty_result

    # 1. Discover all rollout files on disk
    rollout_files_by_path: dict[str, Path] = {}
    rollout_files_by_id: dict[str, Path] = {}

    for p in base_dir.glob("**/*rollout-*.jsonl"):
        rollout_files_by_path[str(p)] = p
        # Filename pattern: rollout-<date>-<thread_id>.jsonl
        fname = p.name
        # Match trailing UUID format if present
        match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", fname)
        if match:
            rollout_files_by_id[match.group(1).lower()] = p

    # 2. Read threads from state_5.sqlite
    db_path = base_dir / "state_5.sqlite"
    threads_data: list[dict[str, Any]] = []

    if db_path.exists():
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

    sessions: list[dict[str, Any]] = []
    processed_rollout_paths: set[str] = set()

    # 3. Match DB threads with rollout data
    for t in threads_data:
        t_id = str(t.get("id") or "")
        db_rollout_path = str(t.get("rollout_path") or "")
        raw_title = str(t.get("title") or "").strip()
        # Clean title to first readable non-empty line
        clean_title = next((line.strip() for line in raw_title.splitlines() if line.strip()), f"Codex Session {t_id[:8]}")
        model = str(t.get("model") or "gpt-5.6-luna").strip()
        reasoning_effort = t.get("reasoning_effort")
        db_tokens = int(t.get("tokens_used") or 0)
        created_at_raw = t.get("created_at")
        usage_events: list[dict[str, Any]] = []

        matched_rollout_path: Path | None = None
        if db_rollout_path and os.path.exists(db_rollout_path):
            matched_rollout_path = Path(db_rollout_path)
        elif db_rollout_path in rollout_files_by_path:
            matched_rollout_path = rollout_files_by_path[db_rollout_path]
        elif t_id.lower() in rollout_files_by_id:
            matched_rollout_path = rollout_files_by_id[t_id.lower()]

        if matched_rollout_path:
            processed_rollout_paths.add(str(matched_rollout_path.resolve()))
            parsed = _parse_rollout_file(matched_rollout_path)
            call_count = parsed["call_count"]
            input_tokens = parsed["input_tokens"]
            cached_input = parsed["cached_input_tokens"]
            uncached_input = parsed["uncached_input_tokens"]
            output = parsed["output_tokens"]
            reasoning_output = parsed["reasoning_output_tokens"]
            total_tokens = parsed["total_tokens"]
            start_time = parsed["start_time"]
            end_time = parsed["end_time"]
            usage_events = list(parsed.get("usage_events") or [])
        else:
            call_count = 1 if db_tokens > 0 else 0
            input_tokens = int(db_tokens * 0.8)
            cached_input = 0
            uncached_input = input_tokens
            output = max(0, db_tokens - input_tokens)
            reasoning_output = 0
            total_tokens = db_tokens
            start_time = None
            end_time = None

        # Fallback to DB tokens if parsed yielded 0 but DB had usage
        if total_tokens == 0 and db_tokens > 0:
            total_tokens = db_tokens
            input_tokens = int(db_tokens * 0.8)
            uncached_input = input_tokens
            cached_input = 0
            output = max(0, db_tokens - input_tokens)
            call_count = max(1, call_count)

        created_at_iso = _to_iso_string(created_at_raw) or (start_time or "")

        if not usage_events and total_tokens > 0:
            usage_events = [{
                "timestamp": created_at_iso,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input,
                "output_tokens": output,
                "reasoning_output_tokens": reasoning_output,
                "total_tokens": total_tokens,
            }]

        cost = calculate_cost(model, uncached_input, cached_input, output)
        total_input = uncached_input + cached_input
        cache_hit_rate = round((cached_input / total_input * 100.0), 2) if total_input > 0 else 0.0

        sessions.append({
            "id": t_id,
            "title": clean_title,
            "tool": "codex",
            "model": model,
            "reasoning_effort": reasoning_effort,
            "call_count": call_count,
            "uncached_input": uncached_input,
            "cached_input": cached_input,
            "total_input": total_input,
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
        })

    # 4. Handle any orphan rollout files not indexed in threads table
    for rpath_str, rpath in rollout_files_by_path.items():
        if rpath_str in processed_rollout_paths:
            continue
        parsed = _parse_rollout_file(rpath)
        if parsed["total_tokens"] == 0 and parsed["call_count"] == 0:
            continue
        # Extract UUID or basename
        match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", rpath.name)
        orphan_id = match.group(1) if match else rpath.stem
        orphan_model = parsed.get("model") or "gpt-5.6-luna"
        created_at_iso = parsed["start_time"] or ""

        cost = calculate_cost(orphan_model, parsed["uncached_input_tokens"], parsed["cached_input_tokens"], parsed["output_tokens"])
        tot_in = parsed["uncached_input_tokens"] + parsed["cached_input_tokens"]
        c_hit_rate = round((parsed["cached_input_tokens"] / tot_in * 100.0), 2) if tot_in > 0 else 0.0

        sessions.append({
            "id": orphan_id,
            "title": f"Codex Session {orphan_id[:8]}",
            "tool": "codex",
            "model": orphan_model,
            "reasoning_effort": None,
            "call_count": parsed["call_count"],
            "uncached_input": parsed["uncached_input_tokens"],
            "cached_input": parsed["cached_input_tokens"],
            "total_input": tot_in,
            "output": parsed["output_tokens"],
            "reasoning_output": parsed["reasoning_output_tokens"],
            "total_tokens": parsed["total_tokens"],
            "cache_hit_rate": c_hit_rate,
            "cost_cached_usd": cost["cost_cached_usd"],
            "cost_uncached_usd": cost["cost_uncached_usd"],
            "savings_usd": cost["savings_usd"],
            "created_at": created_at_iso,
            "start_time": parsed["start_time"],
            "end_time": parsed["end_time"],
            "usage_events": list(parsed.get("usage_events") or []),
        })

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
        entry["output"] += s["output"]
        entry["reasoning_output"] += s["reasoning_output"]
        entry["total_tokens"] += s["total_tokens"]
        entry["est_cost_cached_usd"] += s["cost_cached_usd"]
        entry["est_cost_uncached_usd"] += s["cost_uncached_usd"]
        entry["est_savings_usd"] += s["savings_usd"]

    for entry in models_dict.values():
        tot_in = entry["total_input"]
        entry["cache_hit_rate"] = round((entry["cached_input"] / tot_in * 100.0), 2) if tot_in > 0 else 0.0
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
    tot_input = tot_uncached + tot_cached
    tot_output = sum(s["output"] for s in sessions)
    tot_reasoning = sum(s["reasoning_output"] for s in sessions)
    tot_tokens = sum(s["total_tokens"] for s in sessions)
    tot_cost_cached = round(sum(s["cost_cached_usd"] for s in sessions), 6)
    tot_cost_uncached = round(sum(s["cost_uncached_usd"] for s in sessions), 6)
    tot_savings = round(sum(s["savings_usd"] for s in sessions), 6)
    summary_cache_hit_rate = round((tot_cached / tot_input * 100.0), 2) if tot_input > 0 else 0.0

    summary = {
        "total_tokens": tot_tokens,
        "uncached_input": tot_uncached,
        "cached_input": tot_cached,
        "total_input": tot_input,
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

    def extract_sessions(self, root: str | Path | None = None) -> list[UsageSession]:
        source_root = root if root is not None else self.default_source_path
        return _legacy_sessions_to_contract(parse_codex_usage(source_root))


# Explicit alias for callers that name adapters after the provider/tool.
CodexUsageSource = CodexSource
