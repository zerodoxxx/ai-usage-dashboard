"""Parser for Antigravity (AGY) sessions and transcripts."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..pricing import calculate_cost


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

    # 2. Read conversation summaries DB
    summaries: dict[str, dict[str, Any]] = {}
    db_path = base_dir / "conversation_summaries.db"
    if db_path.exists():
        conn: sqlite3.Connection | None = None
        try:
            try:
                uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
            except Exception:
                conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT conversation_id, title, preview, last_modified_time
                FROM conversation_summaries
                """
            )
            for row in cursor.fetchall():
                cid = str(row["conversation_id"])
                title_val = (row["title"] or "").strip() or (row["preview"] or "").strip()
                summaries[cid] = {
                    "title": title_val,
                    "last_modified_time": row["last_modified_time"],
                }
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # 3. Scan brain/**/transcript.jsonl using os.walk
    brain_dir = base_dir / "brain"
    transcript_files: list[Path] = []
    if brain_dir.exists():
        for root, _dirs, files in os.walk(brain_dir):
            if "transcript.jsonl" in files:
                transcript_files.append(Path(root) / "transcript.jsonl")

    sessions: list[dict[str, Any]] = []

    for t_path in transcript_files:
        try:
            rel_parts = t_path.relative_to(brain_dir).parts
            session_id = rel_parts[0]
        except Exception:
            session_id = t_path.parent.name

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
                                output_chars += len(json.dumps(tool_calls))
                            except Exception:
                                pass
                        thinking_chars += len(thinking)
        except Exception:
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
