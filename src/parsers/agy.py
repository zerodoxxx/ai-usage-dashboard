"""Parser for Antigravity (AGY) sessions and transcripts."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import UsageSession
from ..pricing import calculate_cost, calculate_cost_strict

logger = logging.getLogger(__name__)

# AGY transcripts carry text but no provider token counts (quota is tracked
# server-side by Google), so tokens are estimated with the standard
# chars//4 heuristic. Cache behavior is split by turn count:
# - single-turn (call_count <= 1): 0% cache hit. There is no prior context
#   to reuse, so assuming any cache hit would overstate savings.
# - multi-turn (call_count > 1): flat 45% of input treated as cached. This
#   is a conservative stand-in for prefix caching on repeated conversation
#   context, not a measured rate — Codex/Claude report exact counts instead.
_AGY_CACHE_HIT_RATE_MULTI_TURN = 0.45

def _allocate_total(total: int, weights: list[int]) -> list[int]:
    """Distribute an estimated total across weighted transcript events."""
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


def _reprice_session_cost(session: dict[str, Any]) -> None:
    """Reprice locally estimated costs using each call's timestamp."""
    model = session.get("model")
    provider = "deepseek" if str(model or "").casefold().startswith("deepseek") else "antigravity"
    fallback_timestamp = (
        session.get("created_at")
        or session.get("start_time")
        or session.get("end_time")
        or session.get("activity_at")
    )
    base_cost = calculate_cost_strict(
        model,
        int(session.get("uncached_input") or 0),
        int(session.get("cached_input") or 0),
        int(session.get("output") or 0),
        provider=provider,
        cache_write=int(session.get("cache_write_tokens") or 0),
        timestamp=session.get("created_at") or session.get("start_time") or session.get("activity_at"),
    )

    cached_total = float(base_cost.get("cost_cached_usd") or 0.0)
    uncached_total = float(base_cost.get("cost_uncached_usd") or 0.0)
    savings_total = float(base_cost.get("savings_usd") or 0.0)
    usage_events = session.get("usage_events", [])
    event_costs: list[dict[str, Any]] = []
    event_uncached = 0
    event_cached_total = 0
    event_output = 0
    event_reasoning = 0
    event_total = 0
    event_cache_write = 0
    for event in usage_events:
        event_model = event.get("model") or model
        event_provider = (
            "deepseek"
            if str(event_model or "").casefold().startswith("deepseek")
            else provider
        )
        event_input = int(event.get("input_tokens") or 0)
        event_cached = min(event_input, int(event.get("cached_input_tokens") or 0))
        event_cached_total += event_cached
        event_uncached += event_input - event_cached
        event_output += int(event.get("output_tokens") or 0)
        event_reasoning += int(event.get("reasoning_output_tokens") or 0)
        event_total += int(event.get("total_tokens") or event_input + int(event.get("output_tokens") or 0))
        event_cache_write += int(event.get("cache_write_tokens") or event.get("cache_creation_tokens") or 0)
        event_cost = calculate_cost_strict(
            event_model,
            max(0, event_input - event_cached),
            event_cached,
            int(event.get("output_tokens") or 0),
            provider=event_provider,
            cache_write=int(event.get("cache_write_tokens") or event.get("cache_creation_tokens") or 0),
            timestamp=event.get("timestamp") or fallback_timestamp,
        )
        if event_cost.get("status") == "known":
            event_costs.append(event_cost)

    residual_uncached = max(0, int(session.get("uncached_input") or 0) - event_uncached)
    residual_cached = max(0, int(session.get("cached_input") or 0) - event_cached_total)
    residual_output = max(0, int(session.get("output") or 0) - event_output)
    residual_reasoning = max(0, int(session.get("reasoning_output") or 0) - event_reasoning)
    residual_total = max(0, int(session.get("total_tokens") or 0) - event_total)
    residual_cache_write = max(0, int(session.get("cache_write_tokens") or 0) - event_cache_write)
    represented_calls = sum(
        int((event.get("metadata") or {}).get("call_count") or 0)
        if "call_count" in (event.get("metadata") or {}) else 1
        for event in usage_events
    )
    residual_calls = max(0, int(session.get("call_count") or 0) - represented_calls)
    if any((
        residual_uncached,
        residual_cached,
        residual_output,
        residual_reasoning,
        residual_total,
        residual_cache_write,
        residual_calls,
    )):
        residual_cost = calculate_cost_strict(
            model,
            residual_uncached,
            residual_cached,
            residual_output,
            provider=provider,
            cache_write=residual_cache_write,
            timestamp=fallback_timestamp,
        )
        if residual_cost.get("status") == "known":
            event_costs.append(residual_cost)

    if event_costs:
        cached_total = sum(float(cost.get("cost_cached_usd") or 0.0) for cost in event_costs)
        uncached_total = sum(float(cost.get("cost_uncached_usd") or 0.0) for cost in event_costs)
        savings_total = sum(float(cost.get("savings_usd") or 0.0) for cost in event_costs)

    session["cost_cached_usd"] = cached_total
    session["cost_uncached_usd"] = uncached_total
    session["savings_usd"] = savings_total
    session["cost_usd"] = cached_total


def _to_iso_string(ts: int | float | str | None) -> str:
    """Convert timestamp to an ISO 8601 string."""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        val = ts / 1000.0 if ts > 1e11 else float(ts)
        try:
            return datetime.fromtimestamp(val, timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return ""
    # Try parsing string format if necessary
    s = str(ts).strip()
    return s


def parse_agy_usage(agy_dir: str | Path | None = None) -> dict[str, Any]:
    """Parse Antigravity (AGY) tool usage metrics from transcripts and summaries DB.

    Args:
        agy_dir: Base directory for AGY data. Defaults to ~/.gemini/antigravity-cli.

    Returns:
        Dictionary with keys:
            - 'tool': 'antigravity'
            - 'summary': high-level odometer totals
            - 'models': per-model breakdown list
            - 'timeline': daily aggregated metrics list
            - 'sessions': individual session details list
    """
    base_dir = Path(agy_dir).expanduser() if agy_dir else Path.home() / ".gemini" / "antigravity-cli"

    empty_result: dict[str, Any] = {
        "tool": "antigravity",
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

    # 1. Read configured model from settings.json
    configured_model = "Gemini 3.8 Flash (High)"
    settings_file = base_dir / "settings.json"
    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                settings_data = json.load(f)
                if settings_data.get("model"):
                    configured_model = str(settings_data["model"]).strip()
        except Exception as e:
            logger.debug("Failed to read settings.json from %s: %s", settings_file, e)

    # 1b. Load from token_usage.db if available
    token_db_path = base_dir / "token_usage.db"
    db_sessions: dict[str, dict[str, Any]] = {}
    db_session_ids: set[str] = set()
    if token_db_path.exists():
        conn = None
        try:
            try:
                uri = f"file:{token_db_path.resolve().as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
            except Exception:
                conn = sqlite3.connect(str(token_db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions")
            for row in cursor.fetchall():
                sid = row["session_id"]
                db_session_ids.add(sid)
                ev_cursor = conn.execute("SELECT * FROM token_events WHERE session_id = ? ORDER BY step_index", (sid,))
                events = []
                for ev in ev_cursor.fetchall():
                    events.append({
                        "timestamp": ev["timestamp"],
                        "model": ev["model"],
                        "input_tokens": ev["input_tokens"],
                        "cached_input_tokens": ev["cached_input_tokens"],
                        "output_tokens": ev["output_tokens"],
                        "cache_write_tokens": ev["cache_write_tokens"],
                        "reasoning_output_tokens": ev["reasoning_output_tokens"],
                        "total_tokens": ev["total_tokens"],
                        "cost_usd": ev["cost_usd"],
                    })
                inp = int(row["input_tokens"] or 0)
                cached = int(row["cached_input_tokens"] or 0)
                uncached = max(0, inp - cached)
                out = int(row["output_tokens"] or 0)
                tot = int(row["total_tokens"] or 0)
                cost = float(row["cost_usd"] or 0.0)
                hit_rate = round((cached / inp * 100.0), 2) if inp > 0 else 0.0

                db_sessions[sid] = {
                    "id": sid,
                    "title": row["title"] or f"AGY Session {sid[:8]}",
                    "tool": "antigravity",
                    "model": row["model"],
                    "call_count": int(row["call_count"] or 1),
                    "uncached_input": uncached,
                    "cached_input": cached,
                    "total_input": inp,
                    "output": out,
                    "reasoning_output": int(row["reasoning_output_tokens"] or 0),
                    "total_tokens": tot,
                    "cache_hit_rate": hit_rate,
                    "cost_cached_usd": cost,
                    "cost_uncached_usd": 0.0,
                    "savings_usd": 0.0,
                    "cost_usd": cost,
                    "created_at": row["timestamp"],
                    "start_time": row["timestamp"],
                    "end_time": row["updated_at"],
                    "usage_events": events,
                    "from_token_usage_db": True,
                }
            cursor.close()
        except Exception as e:
            logger.warning("Error reading token_usage.db: %s", e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # 2. Comprehensive session discovery:
    # 2a. Scan brain/**/transcript.jsonl using os.walk
    brain_dir = base_dir / "brain"
    transcripts_by_session: dict[str, Path] = {}
    if brain_dir.exists():
        for root, _dirs, files in os.walk(brain_dir):
            if "transcript.jsonl" in files:
                t_path = Path(root) / "transcript.jsonl"
                try:
                    rel_parts = t_path.relative_to(brain_dir).parts
                    sid = rel_parts[0]
                except Exception:
                    sid = t_path.parent.name
                transcripts_by_session[sid] = t_path

    # 2b. Query conversation_summaries.db
    summaries: dict[str, dict[str, Any]] = {}
    db_path = base_dir / "conversation_summaries.db"
    if db_path.exists():
        conn: sqlite3.Connection | None = None
        cursor = None
        try:
            try:
                uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
            except Exception:
                conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute("PRAGMA query_only = ON;")
            cursor.execute(
                """
                SELECT conversation_id, title, preview, step_count, last_modified_time
                FROM conversation_summaries
                """
            )
            for row in cursor.fetchall():
                cid = str(row[0])
                title_val = (row[1] or "").strip() or (row[2] or "").strip()
                summaries[cid] = {
                    "title": title_val,
                    "step_count": int(row[3] or 0),
                    "last_modified_time": row[4],
                }
        except Exception as e:
            logger.warning("Error querying conversation_summaries.db: %s", e)
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # 2c. Query conversations/*.db
    conv_db_sessions: dict[str, dict[str, Any]] = {}
    conv_dir = base_dir / "conversations"
    if conv_dir.exists():
        for db_file in sorted(conv_dir.glob("*.db")):
            cid = db_file.stem
            step_count = 0
            c_conn = None
            c_cursor = None
            try:
                try:
                    uri = f"file:{db_file.resolve().as_posix()}?mode=ro"
                    c_conn = sqlite3.connect(uri, uri=True)
                except Exception:
                    c_conn = sqlite3.connect(str(db_file))
                c_cursor = c_conn.cursor()
                c_cursor.execute("PRAGMA query_only = ON;")
                c_cursor.execute("SELECT count(*) FROM steps")
                row = c_cursor.fetchone()
                if row:
                    step_count = int(row[0] or 0)
            except Exception as e:
                logger.debug("Error reading steps from %s: %s", db_file, e)
            finally:
                if c_cursor is not None:
                    try:
                        c_cursor.close()
                    except Exception:
                        pass
                if c_conn is not None:
                    try:
                        c_conn.close()
                    except Exception:
                        pass
            mtime = None
            try:
                mtime = db_file.stat().st_mtime
            except OSError:
                pass
            conv_db_sessions[cid] = {
                "step_count": step_count,
                "mtime": mtime,
            }

    # 3. Collect all session IDs and process
    all_session_ids = set(transcripts_by_session.keys()) | set(summaries.keys()) | set(conv_db_sessions.keys()) | db_session_ids

    sessions: list[dict[str, Any]] = []

    for session_id in sorted(all_session_ids):
        if session_id in db_sessions:
            sessions.append(db_sessions[session_id])
            continue
        if session_id in transcripts_by_session:
            t_path = transcripts_by_session[session_id]
            input_chars = 0
            output_chars = 0
            thinking_chars = 0
            call_count = 0
            first_ts: str | None = None
            last_ts: str | None = None
            first_user_prompt = ""
            pending_input_chars = 0
            call_events: list[dict[str, Any]] = []

            try:
                with open(t_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line_str = line.strip()
                        if not line_str:
                            continue
                        try:
                            step = json.loads(line_str)
                        except Exception:
                            continue
                        if not isinstance(step, dict):
                            continue

                        ts = step.get("created_at")
                        if ts:
                            if first_ts is None:
                                first_ts = str(ts)
                            last_ts = str(ts)

                        source = step.get("source")
                        step_type = step.get("type")
                        content = str(step.get("content") or "")
                        thinking = str(step.get("thinking") or "")

                        # User or system input
                        if (
                            source in ("USER_EXPLICIT", "USER", "SYSTEM")
                            or step_type in ("USER_INPUT", "CHECKPOINT", "SYSTEM_MESSAGE")
                        ):
                            input_chars += len(content)
                            pending_input_chars += len(content)
                            if not first_user_prompt and content:
                                cleaned_p = next(
                                    (ln.strip() for ln in content.splitlines() if ln.strip() and not ln.startswith("<")),
                                    ""
                                )
                                if cleaned_p:
                                    first_user_prompt = cleaned_p[:120]
                        # Tool output results fed into model context
                        elif step_type == "GENERIC":
                            input_chars += len(content)
                            pending_input_chars += len(content)
                        # Model generation
                        elif step_type == "PLANNER_RESPONSE" or source == "MODEL":
                            call_count += 1
                            call_output_chars = len(content)
                            tool_calls = step.get("tool_calls")
                            if tool_calls:
                                try:
                                    call_output_chars += len(json.dumps(tool_calls, default=str))
                                except Exception:
                                    pass
                            call_thinking_chars = len(thinking)
                            output_chars += call_output_chars
                            thinking_chars += call_thinking_chars
                            call_events.append({
                                "timestamp": str(ts or last_ts or first_ts or ""),
                                "input_chars": pending_input_chars,
                                "output_chars": call_output_chars,
                                "thinking_chars": call_thinking_chars,
                            })
                            pending_input_chars = 0
            except Exception as e:
                logger.warning("Error reading transcript for %s: %s", t_path, e)
                continue

            input_tokens = input_chars // 4
            cached_input = int(input_tokens * _AGY_CACHE_HIT_RATE_MULTI_TURN) if call_count > 1 else 0
            uncached_input = max(0, input_tokens - cached_input)
            reasoning_output_tokens = thinking_chars // 4
            output_tokens = (output_chars + thinking_chars) // 4
            total_tokens = input_tokens + output_tokens

            if call_events and pending_input_chars:
                call_events[-1]["input_chars"] += pending_input_chars

            usage_events: list[dict[str, Any]] = []
            if call_events:
                input_allocations = _allocate_total(
                    input_tokens,
                    [int(event["input_chars"]) for event in call_events],
                )
                output_allocations = _allocate_total(
                    output_tokens,
                    [int(event["output_chars"]) + int(event["thinking_chars"]) for event in call_events],
                )
                reasoning_allocations = _allocate_total(
                    reasoning_output_tokens,
                    [int(event["thinking_chars"]) for event in call_events],
                )
                cached_total = int(input_tokens * _AGY_CACHE_HIT_RATE_MULTI_TURN) if call_count > 1 else 0
                cached_allocations = _allocate_total(cached_total, input_allocations)
                for index, event in enumerate(call_events):
                    event_input = input_allocations[index]
                    event_cached = min(event_input, cached_allocations[index])
                    event_output = output_allocations[index]
                    usage_events.append({
                        "timestamp": event["timestamp"],
                        "input_tokens": event_input,
                        "cached_input_tokens": event_cached,
                        "output_tokens": event_output,
                        "reasoning_output_tokens": reasoning_allocations[index],
                        "total_tokens": event_input + event_output,
                    })

            if not usage_events and total_tokens > 0:
                usage_events.append({
                    "timestamp": str(first_ts or last_ts or ""),
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning_output_tokens,
                    "total_tokens": total_tokens,
                })

            db_entry = summaries.get(session_id)
            if db_entry:
                title = db_entry["title"] or first_user_prompt or f"AGY Session {session_id[:8]}"
                last_modified = db_entry["last_modified_time"]
            else:
                title = first_user_prompt or f"AGY Session {session_id[:8]}"
                last_modified = last_ts

            created_at_iso = _to_iso_string(first_ts or last_modified)

            cost = calculate_cost(configured_model, uncached_input, cached_input, output_tokens)
            total_input = input_tokens
            cache_hit_rate = round((cached_input / total_input * 100.0), 2) if total_input > 0 else 0.0

            sessions.append({
                "id": session_id,
                "title": title,
                "tool": "antigravity",
                "model": configured_model,
                "call_count": max(1, call_count) if total_tokens > 0 else call_count,
                "uncached_input": uncached_input,
                "cached_input": cached_input,
                "total_input": total_input,
                "output": output_tokens,
                "reasoning_output": reasoning_output_tokens,
                "total_tokens": total_tokens,
                "cache_hit_rate": cache_hit_rate,
                "cost_cached_usd": cost["cost_cached_usd"],
                "cost_uncached_usd": cost["cost_uncached_usd"],
                "savings_usd": cost["savings_usd"],
                "created_at": created_at_iso,
                "start_time": first_ts,
                "end_time": last_ts or str(last_modified or ""),
                "usage_events": usage_events,
            })
        else:
            steps = 0
            if session_id in summaries:
                steps = summaries[session_id].get("step_count") or 0
            if not steps and session_id in conv_db_sessions:
                steps = conv_db_sessions[session_id].get("step_count") or 0
            steps = max(1, steps)

            input_tokens = steps * 600
            output_tokens = steps * 200
            reasoning_output_tokens = int(output_tokens * 0.2)
            call_count = steps
            cached_input = int(input_tokens * _AGY_CACHE_HIT_RATE_MULTI_TURN) if call_count > 1 else 0
            uncached_input = max(0, input_tokens - cached_input)
            total_tokens = input_tokens + output_tokens

            title = summaries.get(session_id, {}).get("title") or f"AGY Session {session_id[:8]}"
            last_modified = summaries.get(session_id, {}).get("last_modified_time")
            if not last_modified and session_id in conv_db_sessions:
                last_modified = conv_db_sessions[session_id].get("mtime")
            created_at_iso = _to_iso_string(last_modified)

            cost = calculate_cost(configured_model, uncached_input, cached_input, output_tokens)
            total_input = input_tokens
            cache_hit_rate = round((cached_input / total_input * 100.0), 2) if total_input > 0 else 0.0
            usage_events = [{
                "timestamp": created_at_iso,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
                "total_tokens": total_tokens,
            }] if total_tokens > 0 else []

            sessions.append({
                "id": session_id,
                "title": title,
                "tool": "antigravity",
                "model": configured_model,
                "call_count": call_count,
                "uncached_input": uncached_input,
                "cached_input": cached_input,
                "total_input": total_input,
                "output": output_tokens,
                "reasoning_output": reasoning_output_tokens,
                "total_tokens": total_tokens,
                "cache_hit_rate": cache_hit_rate,
                "cost_cached_usd": cost["cost_cached_usd"],
                "cost_uncached_usd": cost["cost_uncached_usd"],
                "savings_usd": cost["savings_usd"],
                "created_at": created_at_iso,
                "start_time": str(last_modified or ""),
                "end_time": str(last_modified or ""),
                "usage_events": usage_events,
            })

    # Reprice each call at its own timestamp so the legacy parser summary
    # agrees with the shared aggregator's time-range-aware totals.
    for session in sessions:
        _reprice_session_cost(session)

    # Sort sessions by created_at descending
    sessions.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)

    # 4. Compute per-model stats
    models_dict: dict[str, dict[str, Any]] = {}
    for s in sessions:
        m = s["model"]
        if m not in models_dict:
            models_dict[m] = {
                "model": m,
                "tool": "antigravity",
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
        entry["est_cost_cached_usd"] += s.get("cost_cached_usd", s.get("cost_usd", 0.0))
        entry["est_cost_uncached_usd"] += s.get("cost_uncached_usd", 0.0)
        entry["est_savings_usd"] += s.get("savings_usd", 0.0)

    for entry in models_dict.values():
        tot_in = entry["total_input"]
        entry["cache_hit_rate"] = round((entry["cached_input"] / tot_in * 100.0), 2) if tot_in > 0 else 0.0
        entry["est_cost_cached_usd"] = round(entry["est_cost_cached_usd"], 6)
        entry["est_cost_uncached_usd"] = round(entry["est_cost_uncached_usd"], 6)
        entry["est_savings_usd"] = round(entry["est_savings_usd"], 6)

    models_list = sorted(models_dict.values(), key=lambda x: x["total_tokens"], reverse=True)

    # 5. Compute timeline stats grouped by date YYYY-MM-DD
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
        day["cost_cached_usd"] += s.get("cost_cached_usd", s.get("cost_usd", 0.0))
        day["cost_uncached_usd"] += s.get("cost_uncached_usd", 0.0)
        day["savings_usd"] += s.get("savings_usd", 0.0)

    timeline_list = []
    for date_key in sorted(timeline_dict.keys()):
        d = timeline_dict[date_key]
        d["cost_cached_usd"] = round(d["cost_cached_usd"], 6)
        d["cost_uncached_usd"] = round(d["cost_uncached_usd"], 6)
        d["savings_usd"] = round(d["savings_usd"], 6)
        timeline_list.append(d)

    # 6. Summary odometer totals
    tot_uncached = sum(s["uncached_input"] for s in sessions)
    tot_cached = sum(s["cached_input"] for s in sessions)
    tot_input = tot_uncached + tot_cached
    tot_output = sum(s["output"] for s in sessions)
    tot_reasoning = sum(s["reasoning_output"] for s in sessions)
    tot_tokens = sum(s["total_tokens"] for s in sessions)
    tot_cost_cached = round(sum(s.get("cost_cached_usd", s.get("cost_usd", 0.0)) for s in sessions), 6)
    tot_cost_uncached = round(sum(s.get("cost_uncached_usd", 0.0) for s in sessions), 6)
    tot_savings = round(sum(s.get("savings_usd", 0.0) for s in sessions), 6)
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

    result = {
        "tool": "antigravity",
        "summary": summary,
        "models": models_list,
        "timeline": timeline_list,
        "sessions": sessions,
    }
    return result


def _legacy_sessions_to_contract(
    result: dict[str, Any],
) -> list[UsageSession]:
    """Convert parsed AGY sessions into provider-neutral usage contracts."""
    sessions: list[UsageSession] = []
    for raw_session in result.get("sessions", []):
        if not isinstance(raw_session, dict):
            continue
        session = UsageSession.from_legacy_dict(raw_session)
        session.provider = "antigravity"
        session.tool = "antigravity"
        if raw_session.get("from_token_usage_db"):
            session.metadata["estimated"] = False
            session.metadata["token_source"] = "reported"
            if session.cost is not None:
                # token_usage.db stores the local estimator's result, not a
                # provider-reported bill. Keep the exact token provenance,
                # but let the shared aggregator reprice costs per call using
                # the active catalog and each event timestamp.
                session.cost.source = "estimated"
        else:
            session.metadata["estimated"] = True
            session.metadata["token_source"] = "estimated"
            if session.cost is not None and session.cost.source != "reported":
                session.cost.source = "estimated"
        for event in session.events:
            if event.model is None:
                event.model = session.model
        sessions.append(session)
    return sessions


class AntigravitySource:
    """Provider adapter for Antigravity transcripts and metadata databases."""

    key = "antigravity"
    provider = key
    aliases = ("agy", "antigravity-cli")
    default_source_path = Path.home() / ".gemini" / "antigravity-cli"
    default_root = default_source_path
    default_path = default_source_path

    def extract_sessions(self, root: str | Path | None = None) -> list[UsageSession]:
        source_root = root if root is not None else self.default_source_path
        return _legacy_sessions_to_contract(parse_agy_usage(source_root))


# Short alias retained for callers that refer to the provider as AGY.
AGYSource = AntigravitySource
AntigravityUsageSource = AntigravitySource
