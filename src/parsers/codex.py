"""Parser for Codex sessions and rollout logs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..pricing import calculate_cost


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
    call_count = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None

    last_cumulative: dict[str, int] | None = None
    sum_incremental = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    record = json.loads(line_str)
                except Exception:
                    continue

                ts = record.get("timestamp")
                if ts:
                    if first_timestamp is None:
                        first_timestamp = str(ts)
                    last_timestamp = str(ts)

                rec_type = record.get("type")
                payload = record.get("payload") or {}

                # Format 1: token_usage_record
                if rec_type == "token_usage_record":
                    call_count += 1
                    u = payload.get("usage") or {}
                    sum_incremental["input_tokens"] += u.get("input_tokens", 0)
                    sum_incremental["cached_input_tokens"] += u.get("cached_input_tokens", 0)
                    sum_incremental["output_tokens"] += u.get("output_tokens", 0)
                    sum_incremental["reasoning_output_tokens"] += u.get("reasoning_output_tokens", 0)
                    sum_incremental["total_tokens"] += u.get("total_tokens", 0)

                    thread_cum = payload.get("thread_token_usage") or payload.get("turn_token_usage")
                    if isinstance(thread_cum, dict) and thread_cum:
                        last_cumulative = thread_cum

                # Format 2: event_msg with payload.type == 'token_count'
                elif rec_type == "event_msg" and payload.get("type") == "token_count":
                    call_count += 1
                    info = payload.get("info") or {}
                    last_u = info.get("last_token_usage") or {}
                    sum_incremental["input_tokens"] += last_u.get("input_tokens", 0)
                    sum_incremental["cached_input_tokens"] += last_u.get("cached_input_tokens", 0)
                    sum_incremental["output_tokens"] += last_u.get("output_tokens", 0)
                    sum_incremental["reasoning_output_tokens"] += last_u.get("reasoning_output_tokens", 0)
                    sum_incremental["total_tokens"] += last_u.get("total_tokens", 0)

                    tot_u = info.get("total_token_usage")
                    if isinstance(tot_u, dict) and tot_u:
                        last_cumulative = tot_u
    except Exception:
        pass

    if last_cumulative:
        input_tokens = int(last_cumulative.get("input_tokens", 0))
        cached_input_tokens = int(last_cumulative.get("cached_input_tokens", 0))
        output_tokens = int(last_cumulative.get("output_tokens", 0))
        reasoning_output_tokens = int(last_cumulative.get("reasoning_output_tokens", 0))
        total_tokens = int(last_cumulative.get("total_tokens", input_tokens + output_tokens))
    else:
        input_tokens = sum_incremental["input_tokens"]
        cached_input_tokens = sum_incremental["cached_input_tokens"]
        output_tokens = sum_incremental["output_tokens"]
        reasoning_output_tokens = sum_incremental["reasoning_output_tokens"]
        total_tokens = sum_incremental["total_tokens"] or (input_tokens + output_tokens)

    uncached_input_tokens = max(0, input_tokens - cached_input_tokens)

    return {
        "call_count": call_count,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "uncached_input_tokens": uncached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total_tokens,
        "start_time": first_timestamp,
        "end_time": last_timestamp,
    }


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

    for p in base_dir.glob("sessions/**/rollout-*.jsonl"):
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
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, model, reasoning_effort, tokens_used, created_at, rollout_path
                FROM threads
                """
            )
            for row in cursor.fetchall():
                threads_data.append(dict(row))
            conn.close()
        except Exception:
            pass

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

        matched_rollout_path: Path | None = None
        if db_rollout_path and os.path.exists(db_rollout_path):
            matched_rollout_path = Path(db_rollout_path)
        elif db_rollout_path in rollout_files_by_path:
            matched_rollout_path = rollout_files_by_path[db_rollout_path]
        elif t_id.lower() in rollout_files_by_id:
            matched_rollout_path = rollout_files_by_id[t_id.lower()]

        if matched_rollout_path:
            processed_rollout_paths.add(str(matched_rollout_path))
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
        orphan_model = "gpt-5.6-luna"
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
