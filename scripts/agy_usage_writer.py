#!/usr/bin/env python3
"""Capture Antigravity token usage through the shared usage store.

A session is captured by the first of three tiers that has data for it:

1. Conversation database (token_origin "agy-conversation-db"): real per-call
   counts and per-call model ids read from <agy dir>/conversations/<id>.db.
2. Transcript JSON (token_origin "agy-transcript-json"): the real per-call
   counts newer agy versions write on every PLANNER_RESPONSE transcript line.
3. Estimate (no token_origin): counts estimated from the transcript text with
   tiktoken or a regex fallback. Only used when no recorded counts exist.

The store never lets a lower tier replace a higher one. Cache writes are always
stored as zero: no cache-write field has been observed in agy's recorded usage.
We use additive store semantics, matching read-side normalization of the
external tracker's embedded_in_input rows. The store keeps native IDs so
existing tracker sessions are updated in place.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PROVIDER = "antigravity"
ORIGIN_CONVERSATION_DB = "agy-conversation-db"
ORIGIN_TRANSCRIPT_JSON = "agy-transcript-json"
TIER_CONVERSATION_DB, TIER_TRANSCRIPT_JSON, TIER_ESTIMATE = "tier1", "tier2", "tier3"
TIER_SKIPPED = "skipped"
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
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
    base_dir: Path, session_id: str, hint: str | os.PathLike | None = None,
) -> Path | None:
    """Resolve a conversation transcript using the hook's canonical order.

    ``hint`` is the hook payload's ``transcriptPath`` (a string) or a path the
    backfill discovered. It only locates a transcript, never a conversation database.
    """
    candidates = []
    if isinstance(hint, (str, os.PathLike)) and hint:
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


def conversation_db_path(base_dir: str | Path, session_id: str) -> Path | None:
    """Locate a conversation database from the session id alone, or ``None``.

    Ids that could escape ``conversations/`` are refused; the hook payload's
    ``transcriptPath`` is deliberately never involved.
    """
    if (
        not isinstance(session_id, str) or session_id.startswith(".")
        or _SESSION_ID_PATTERN.fullmatch(session_id) is None
    ):
        return None
    return Path(base_dir) / "conversations" / f"{session_id}.db"


def _cost(model, usage, timestamp, provider=PROVIDER):
    from src.parsers.contracts import CostEstimate
    from src.pricing import calculate_cost_strict

    costs = calculate_cost_strict(
        model, usage.uncached_input_tokens, usage.cached_input_tokens,
        usage.output_tokens, provider=provider, timestamp=timestamp,
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


def _model_provider(model: str) -> str:
    """Pricing provider for a per-call model id, by the aggregator's own rules.

    A call is therefore priced the same at write time and on read. An id no rule
    matches keeps the session's provider. Importing the contracts already loads
    the aggregator through ``src.parsers``, so this adds no import cost.
    """
    from src.parsers.aggregator import _model_provider as aggregator_provider

    return aggregator_provider(model, PROVIDER) or PROVIDER


def _file_time(path: Path | None) -> datetime | None:
    """A source file's modification time in UTC, or ``None`` when it cannot be read."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) if path else None
    except (OSError, OverflowError, ValueError):
        return None


def _real_event(session_id, step_index, timestamp, model, uncached, cache_read, output, reasoning):
    """One call with recorded counts: input includes cache reads, cache writes are zero."""
    from src.parsers.contracts import TokenUsage, UsageEvent

    usage = TokenUsage(
        input_tokens=uncached + cache_read, cached_input_tokens=cache_read,
        output_tokens=output, reasoning_output_tokens=reasoning,
        total_tokens=uncached + cache_read + output, cache_write_tokens=0,
    )
    # An unpriced id keeps cost=None; the aggregator re-prices the raw id on read.
    return UsageEvent(
        timestamp=timestamp, model=model, usage=usage,
        cost=_cost(model, usage, timestamp, _model_provider(model)),
        event_id=f"{session_id}:call:{step_index}",
    )


def _real_session(session_id, title, events, origin, fallback_time=None):
    """Assemble a session whose totals are exactly the sums of its events.

    When no event carries a timestamp the session is dated ``fallback_time``
    (its source file's mtime), so a replay does not move it to "now".
    """
    from decimal import Decimal
    from src.parsers.contracts import CostEstimate, TokenUsage, UsageSession

    usage = TokenUsage(**{
        key: sum(getattr(event.usage, key) for event in events)
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    })
    models = {event.model for event in events}
    stamps = [event.timestamp for event in events if event.timestamp is not None]
    first_ts, last_ts = (min(stamps), max(stamps)) if stamps else (fallback_time, fallback_time)
    priced = [event.cost for event in events if event.cost is not None]
    cost = CostEstimate(
        cached_usd=sum((item.cached_usd for item in priced), Decimal("0")),
        uncached_usd=sum((item.uncached_usd for item in priced), Decimal("0")),
        savings_usd=sum((item.savings_usd for item in priced), Decimal("0")),
        source="estimated",
    ) if priced else None
    return UsageSession(
        id=session_id, tool=PROVIDER, provider=PROVIDER,
        model=next(iter(models)) if len(models) == 1 else "mixed",
        title=title, created_at=first_ts, start_time=first_ts,
        end_time=last_ts, activity_at=last_ts, usage=usage, events=events,
        cost=cost, call_count=len(events),
        metadata={"estimated": False, "token_source": "reported", "token_origin": origin},
    )


def build_real_session(session_id, calls, base_dir, origin):
    """Tier 1: one event per recorded call, using the model id each call ran on."""
    meta = get_session_metadata(base_dir, session_id)
    modeled = Counter(call.model for call in calls if call.model)
    # A call without a model id takes the session's most common one, else the label.
    fallback = modeled.most_common(1)[0][0] if modeled else meta["model"]
    events = [
        _real_event(
            session_id, call.step_index, call.timestamp, call.model or fallback,
            call.uncached_input, call.cache_read, call.output, call.reasoning,
        )
        for call in calls
    ]
    fallback_time = _file_time(conversation_db_path(base_dir, session_id))
    return _real_session(session_id, meta["title"], events, origin, fallback_time)


def parse_transcript_json_usage(transcript_path: Path, session_id: str, base_dir: Path):
    """Tier 2: real counts from PLANNER_RESPONSE transcript lines, all or nothing.

    Returns ``None`` unless every PLANNER_RESPONSE line carries integer
    ``input_tokens`` (uncached), ``cache_read_tokens`` and ``output_tokens``.
    """
    from src.agy_conversation_usage import MAX_TOKEN_COUNT, UNPAIRED_STEP_BASE

    def is_count(value):
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_TOKEN_COUNT

    rows = []
    with transcript_path.open("r", encoding="utf-8", errors="ignore") as stream:
        for idx, line in enumerate(stream):
            if not line.strip():
                continue
            try:
                step = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(step, dict) or step.get("type") != "PLANNER_RESPONSE":
                continue
            counts = [step.get(key) for key in ("input_tokens", "cache_read_tokens", "output_tokens")]
            if not all(is_count(value) for value in counts):
                return None
            rows.append((idx, step, *counts))
    meta = get_session_metadata(base_dir, session_id)
    events, seen = [], set()
    for idx, step, uncached, cache_read, output in rows:
        if not (uncached or cache_read or output):
            continue
        step_index = step.get("step_index")
        if not is_count(step_index) or step_index in seen:
            step_index = UNPAIRED_STEP_BASE + idx
        seen.add(step_index)
        created = step.get("created_at")
        events.append(_real_event(
            session_id, step_index, created if isinstance(created, str) and created else None,
            meta["model"], uncached, cache_read, output, 0,
        ))
    if not events:
        return None
    return _real_session(
        session_id, meta["title"], events, ORIGIN_TRANSCRIPT_JSON, _file_time(transcript_path),
    )


def _skip(stage: str, exc: Exception) -> None:
    print(f"Antigravity {stage} skipped ({type(exc).__name__}).", file=sys.stderr)


def capture_session(base_dir, session_id, hint=None):
    """Return ``(session, tier)`` from the best tier that has data, else ``(None, "skipped")``.

    A tier that fails for any reason degrades to the next one.
    """
    db_path = conversation_db_path(base_dir, session_id)
    if db_path is not None:
        try:
            from src.agy_conversation_usage import read_conversation_calls

            calls = read_conversation_calls(db_path)
            if calls:
                return build_real_session(session_id, calls, base_dir, ORIGIN_CONVERSATION_DB), TIER_CONVERSATION_DB
        except Exception as exc:
            _skip("conversation database", exc)
    path = _resolve_transcript(base_dir, session_id, hint)
    if path is None:
        return None, TIER_SKIPPED
    try:
        session = parse_transcript_json_usage(path, session_id, base_dir)
        if session is not None:
            return session, TIER_TRANSCRIPT_JSON
    except Exception as exc:
        _skip("transcript token counts", exc)
    session = parse_transcript(path, session_id, base_dir)
    return (session, TIER_ESTIMATE) if session is not None else (None, TIER_SKIPPED)


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
    session, _tier = capture_session(base, session_id, payload.get("transcriptPath"))
    if session is None:
        return None
    # One snapshot per run: when the store rejects it (a better tier already
    # holds this session) nothing lower is tried.
    return session.usage.total_tokens if _write_sessions([session], db_path) else None


def backfill_agy_usage(*, db_path=None, agy_dir=None) -> tuple[int, int]:
    base = _base_dir(agy_dir)
    conversations, brain = base / "conversations", base / "brain"
    if not conversations.is_dir() and not brain.is_dir():
        raise FileNotFoundError("Antigravity conversations and brain directories are unavailable")
    grouped: dict[str, list[Path]] = {}
    if brain.is_dir():
        for path in brain.glob("**/transcript.jsonl"):
            try:
                session_id = path.relative_to(brain).parts[0]
            except (ValueError, IndexError) as exc:
                print(f"Antigravity backfill skipped {path}: {exc}", file=sys.stderr)
                continue
            grouped.setdefault(session_id, []).append(path)

    counts = {TIER_CONVERSATION_DB: 0, TIER_TRANSCRIPT_JSON: 0, TIER_ESTIMATE: 0, TIER_SKIPPED: 0}
    sessions = events = 0

    def capture(session_id: str) -> None:
        nonlocal sessions, events
        # Prefer the same canonical transcript the hook resolves. If neither
        # canonical path exists, keep the tracker's recursive last-path behavior
        # by passing that path as the transcript hint (tiers 2 and 3 only).
        paths = grouped.get(session_id)
        hint = _resolve_transcript(base, session_id) or (paths[-1] if paths else None)
        try:
            session, tier = capture_session(base, session_id, hint)
            if session is not None and _write_sessions([session], db_path):
                counts[tier] += 1
                sessions += 1
                events += len(session.events)
            else:
                counts[TIER_SKIPPED] += 1
        except Exception as exc:
            counts[TIER_SKIPPED] += 1
            print(f"Antigravity backfill skipped {session_id}: {exc}", file=sys.stderr)

    # Conversation databases first: they cover tiers 1-3 for each session.
    handled: set[str] = set()
    if conversations.is_dir():
        for db_file in sorted(conversations.glob("*.db")):
            handled.add(db_file.stem)
            if conversation_db_path(base, db_file.stem) is None:
                counts[TIER_SKIPPED] += 1
                continue
            capture(db_file.stem)
    # Then transcript-only sessions that no conversation database covered.
    for session_id in grouped:
        if session_id not in handled:
            capture(session_id)
    print(
        f"Antigravity backfill: tier1={counts[TIER_CONVERSATION_DB]} tier2={counts[TIER_TRANSCRIPT_JSON]} "
        f"tier3={counts[TIER_ESTIMATE]} skipped={counts[TIER_SKIPPED]}",
        file=sys.stderr,
    )
    return sessions, events


def main(argv: list[str] | None = None) -> int:
    # Imports, CLI errors and hook failures all remain nonfatal to agy.
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument(
            "--backfill", action="store_true",
            help="capture every conversation database first, then transcript-only sessions",
        )
        modes.add_argument("--status", action="store_true", help="print shared usage database status")
        parser.add_argument("--db", type=Path)
        parser.add_argument("--agy-dir", type=Path, help="Antigravity CLI history directory")
        args = parser.parse_args(argv)
        if args.status:
            from src.usage_store import get_usage_store_status
            print(json.dumps(get_usage_store_status(args.db), sort_keys=True))
        elif args.backfill:
            sessions, events = backfill_agy_usage(db_path=args.db, agy_dir=args.agy_dir)
            print(f"Backfilled {sessions} Antigravity sessions ({events} events).", file=sys.stderr)
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
