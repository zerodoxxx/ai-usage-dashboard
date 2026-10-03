#!/usr/bin/env python3
"""Capture Antigravity transcript estimates through the shared usage store.

The external tracker's cache-write estimate was already part of input. We use
additive store semantics with zero cache writes, matching read-side normalization
of its embedded_in_input rows. The store keeps native IDs so existing tracker
sessions are updated in place.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PROVIDER = "antigravity"
_ENC = None
_ENCODING_ATTEMPTED = False


def count_tokens(text: str) -> int:
    """Match track_usage.py, including its regex-before-chars fallback."""
    global _ENC, _ENCODING_ATTEMPTED
    if not text:
        return 0
    if not _ENCODING_ATTEMPTED:
        _ENCODING_ATTEMPTED = True
        try:
            import tiktoken
            _ENC = tiktoken.get_encoding("cl100k_base")
        except Exception:
            pass
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:
            pass
    tokens = re.findall(r"\w+|[^\w\s]|\s+", text)
    return len(tokens) if tokens else max(1, int(len(text) / 3.85))


def _base_dir(base_dir: str | Path | None = None) -> Path:
    return Path(base_dir).expanduser() if base_dir is not None else Path.home() / ".gemini/antigravity-cli"


def get_session_metadata(base_dir: Path, session_id: str) -> dict[str, str]:
    meta = {"title": f"Session {session_id[:8]}", "workspace": "", "model": "Gemini 3.8 Flash"}
    summary = base_dir / "conversation_summaries.db"
    if summary.is_file():
        try:
            with sqlite3.connect(summary.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT title, preview, workspace FROM conversation_summaries WHERE conversation_id = ?",
                    (session_id,),
                ).fetchone()
                if row:
                    meta["title"] = row[0] or (row[1][:100] if row[1] else meta["title"])
                    meta["workspace"] = row[2] or ""
        except Exception:
            pass
    settings = base_dir / "settings.json"
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            if data.get("model"):
                meta["model"] = str(data["model"]).strip()
        except Exception:
            pass
    return meta


def _capture_file_metadata(path: Path) -> dict[str, Any] | None:
    """Return a transcript identity and revision for ordering snapshots."""
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


def _resolve_transcript(
    base_dir: Path, session_id: str, hint: str | None = None,
) -> Path | None:
    """Resolve a conversation transcript using the hook's canonical order."""
    candidates = []
    if isinstance(hint, str) and hint:
        candidates.append(Path(hint).expanduser())
    candidates.extend((
        base_dir / "brain" / session_id / "transcript.jsonl",
        base_dir / "brain" / session_id / ".system_generated/logs/transcript.jsonl",
    ))
    for path in candidates:
        try:
            if path.is_file():
                return path
        except (OSError, RuntimeError):
            continue
    return None


def _cost(model, usage, timestamp):
    from src.parsers.contracts import CostEstimate
    from src.pricing import calculate_cost_strict

    costs = calculate_cost_strict(
        model, usage.uncached_input_tokens, usage.cached_input_tokens,
        usage.output_tokens, provider=PROVIDER, timestamp=timestamp,
    )
    if costs["cost_cached_usd"] is None:
        return None
    return CostEstimate(
        cached_usd=costs["cost_cached_usd"], uncached_usd=costs["cost_uncached_usd"],
        savings_usd=costs["savings_usd"], source="estimated",
    )


def parse_transcript(transcript_path: Path, session_id: str, base_dir: Path):
    """Port the legacy context accumulation, call detection and 45% cache rule."""
    from src.parsers.contracts import TokenUsage, UsageEvent, UsageSession

    capture_metadata = _capture_file_metadata(transcript_path)
    if capture_metadata is None:
        return None
    meta = get_session_metadata(base_dir, session_id)
    context_tokens = 0
    first_ts = last_ts = None
    step_count = 0
    events = []
    # A failed read must not overwrite a previous snapshot with zero usage.
    with transcript_path.open("r", encoding="utf-8", errors="ignore") as stream:
        for idx, line in enumerate(stream):
            if not line.strip():
                continue
            try:
                step = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(step, dict):
                continue
            step_count += 1
            ts = str(step.get("created_at") or datetime.now(timezone.utc).isoformat())
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            source, kind = step.get("source"), step.get("type")
            content = str(step.get("content") or "")
            thinking = str(step.get("thinking") or "")
            tool_calls = step.get("tool_calls")
            if tool_calls:
                try:
                    content += json.dumps(tool_calls, default=str)
                except Exception:
                    pass
            if source in ("USER_EXPLICIT", "USER", "SYSTEM") or kind in (
                "USER_INPUT", "CHECKPOINT", "SYSTEM_MESSAGE", "GENERIC",
            ):
                context_tokens += count_tokens(content)
            if kind == "PLANNER_RESPONSE" or source == "MODEL":
                reasoning = count_tokens(thinking)
                output = count_tokens(content) + reasoning
                cached = min(context_tokens, int(context_tokens * 0.45)) if events else 0
                usage = TokenUsage(
                    input_tokens=context_tokens, cached_input_tokens=cached,
                    output_tokens=output, reasoning_output_tokens=reasoning,
                    total_tokens=context_tokens + output, cache_write_tokens=0,
                )
                events.append(UsageEvent(
                    timestamp=ts, model=meta["model"], usage=usage,
                    cost=_cost(meta["model"], usage, ts),
                    event_id=f"{session_id}:step:{idx}",
                ))
    first_ts = first_ts or datetime.now(timezone.utc).isoformat()
    last_ts = last_ts or first_ts
    usage = TokenUsage(**{
        key: sum(getattr(event.usage, key) for event in events)
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    })
    return UsageSession(
        id=session_id, tool=PROVIDER, provider=PROVIDER, model=meta["model"],
        title=meta["title"], created_at=first_ts, start_time=first_ts,
        end_time=last_ts, activity_at=last_ts, usage=usage, events=events,
        cost=_cost(meta["model"], usage, first_ts), call_count=len(events),
        metadata={
            "estimated": True, "token_source": "estimated", "step_count": step_count,
            **capture_metadata,
        },
    )


def _write_sessions(sessions, db_path=None) -> int:
    from src.usage_store import write_usage_sessions

    return write_usage_sessions(PROVIDER, sessions, db_path=db_path)


def process_hook_payload(payload: Any, *, db_path=None, agy_dir=None) -> int | None:
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get("tool_input")
    session_id = payload.get("conversationId") or (
        tool_input.get("conversationId") if isinstance(tool_input, dict) else None
    )
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    base = _base_dir(agy_dir)
    transcript = payload.get("transcriptPath")
    path = _resolve_transcript(base, session_id, transcript)
    if path is None:
        return None
    session = parse_transcript(path, session_id, base)
    if session is None:
        return None
    return session.usage.total_tokens if _write_sessions([session], db_path) else None


def backfill_agy_usage(*, db_path=None, agy_dir=None) -> tuple[int, int]:
    base = _base_dir(agy_dir)
    brain = base / "brain"
    if not brain.is_dir():
        raise FileNotFoundError("Antigravity brain directory is unavailable")
    sessions = events = 0
    grouped: dict[str, list[Path]] = {}
    for path in brain.glob("**/transcript.jsonl"):
        try:
            session_id = path.relative_to(brain).parts[0]
        except (ValueError, IndexError) as exc:
            print(f"Antigravity backfill skipped {path}: {exc}", file=sys.stderr)
            continue
        grouped.setdefault(session_id, []).append(path)

    # Prefer the same canonical transcript the hook resolves. If neither
    # canonical path exists, keep the tracker's recursive last-path behavior.
    for session_id, paths in grouped.items():
        path = _resolve_transcript(base, session_id) or paths[-1]
        try:
            session = parse_transcript(path, session_id, base)
            if session is None:
                print(
                    f"Antigravity backfill skipped {path}: could not capture transcript revision",
                    file=sys.stderr,
                )
                continue
            sessions += _write_sessions([session], db_path)
            events += len(session.events)
        except Exception as exc:
            print(f"Antigravity backfill skipped {path}: {exc}", file=sys.stderr)
    return sessions, events


def main(argv: list[str] | None = None) -> int:
    # Imports, CLI errors and hook failures all remain nonfatal to agy.
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--backfill", action="store_true")
        modes.add_argument("--status", action="store_true")
        parser.add_argument("--db", type=Path)
        parser.add_argument("--agy-dir", type=Path, help="Antigravity CLI history directory")
        args = parser.parse_args(argv)
        if args.status:
            from src.usage_store import get_usage_store_status
            print(json.dumps(get_usage_store_status(args.db), sort_keys=True))
        elif args.backfill:
            sessions, events = backfill_agy_usage(db_path=args.db, agy_dir=args.agy_dir)
            print(f"Backfilled {sessions} Antigravity transcripts ({events} events).", file=sys.stderr)
        else:
            # The hook protocol expects an empty JSON object on stdout.
            print("{}", flush=True)
            total = process_hook_payload(json.load(sys.stdin), db_path=args.db, agy_dir=args.agy_dir)
            print(f"Antigravity capture: {total if total is not None else 'skipped'} tokens.", file=sys.stderr)
    except SystemExit as exc:
        if exc.code:
            print("Antigravity usage arguments rejected.", file=sys.stderr)
    except BaseException as exc:
        print(f"Antigravity usage capture skipped ({type(exc).__name__}: {exc}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
