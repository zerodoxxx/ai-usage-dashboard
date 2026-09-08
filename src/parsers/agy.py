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

from ..pricing import calculate_cost

logger = logging.getLogger(__name__)


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
    all_session_ids = set(transcripts_by_session.keys()) | set(summaries.keys()) | set(conv_db_sessions.keys())

    sessions: list[dict[str, Any]] = []

    for session_id in sorted(all_session_ids):
        if session_id in transcripts_by_session:
            t_path = transcripts_by_session[session_id]
            input_chars = 0
            output_chars = 0
            thinking_chars = 0
            call_count = 0
            first_ts: str | None = None
            last_ts: str | None = None
            first_user_prompt = ""

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
                        # Model generation
                        elif step_type == "PLANNER_RESPONSE" or source == "MODEL":
                            call_count += 1
                            output_chars += len(content)
                            tool_calls = step.get("tool_calls")
                            if tool_calls:
                                try:
                                    output_chars += len(json.dumps(tool_calls, default=str))
                                except Exception:
                                    pass
                            thinking_chars += len(thinking)
            except Exception as e:
                logger.warning("Error reading transcript for %s: %s", t_path, e)
                continue

            # Estimation formulas:
            # prompt / user input chars // 4 -> input tokens
            # output + thinking chars // 4 -> output tokens
            # 45% cache hit rate if multi-turn (call_count > 1)
            input_tokens = input_chars // 4
            cached_input = int(input_tokens * 0.45) if call_count > 1 else 0
            uncached_input = max(0, input_tokens - cached_input)
            reasoning_output_tokens = thinking_chars // 4
            output_tokens = (output_chars + thinking_chars) // 4
            total_tokens = input_tokens + output_tokens

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
            cached_input = int(input_tokens * 0.45) if call_count > 1 else 0
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
            })

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

    # 6. Summary odometer totals
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
        "tool": "antigravity",
        "summary": summary,
        "models": models_list,
        "timeline": timeline_list,
        "sessions": sessions,
    }
