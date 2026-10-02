#!/usr/bin/env python3
"""Publish Codex completion-hook usage snapshots into the shared SQLite DB.

Codex hook payloads do not contain token counts. This hook reads only the
transcript named by the completion event and asks the existing Codex parser to
extract normalized usage. It never scans the full history during normal hook
execution and never stores transcript text.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import signal
import sys
import time
from pathlib import Path
from typing import Any

# The installer deploys this script beside the project ``src/`` package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.usage_store import (  # noqa: E402
    get_usage_store_status,
    is_provider_capture_enabled,
    mark_provider_capture_enabled,
    resolve_db_path,
    UsageStoreDeadlineExceeded,
    write_usage_sessions,
)


_CAPTURE_EVENTS = frozenset({
    "stop",
    "subagentstop",
    "interrupt",
    "sessionend",
})


class _DeadlineExpired(TimeoutError):
    """The hook's total capture budget has been exhausted."""


@contextmanager
def _parse_backstop(deadline: float | None):
    """Last-resort SIGALRM guard around transcript parsing.

    The store enforces the deadline for its own SQLite waits and retries; only
    parsing (pure Python, no store call) needs an interrupting timer.
    """
    if deadline is None:
        yield
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineExpired()

    def expire(_signum, _frame):
        raise _DeadlineExpired()

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    try:
        signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, remaining)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        delay, interval = previous_timer
        if delay:
            delay = max(0.000001, delay - (time.monotonic() - started))
        signal.setitimer(signal.ITIMER_REAL, delay, interval)


def _deadline_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("deadline must be a positive finite number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("deadline must be a positive finite number")
    return seconds


def _capture_target(payload: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """Return transcript path, correct session/agent ID, and safe model hint."""
    event = str(payload.get("hook_event_name") or payload.get("event") or "").strip().casefold()
    if event and event not in _CAPTURE_EVENTS:
        return None

    if event == "subagentstop":
        # Codex currently places the parent session ID in session_id here.
        # agent_id plus agent_transcript_path identify the completed child.
        transcript_path = payload.get("agent_transcript_path")
        session_id = payload.get("agent_id")
        model = None  # Never use the parent's model for the child's transcript.
    else:
        transcript_path = payload.get("transcript_path")
        session_id = payload.get("session_id")
        model_value = payload.get("model")
        model = str(model_value).strip() if model_value else None

    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return None
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return transcript_path, session_id, model


def process_hook_payload(
    payload: Any,
    *,
    db_path: str | Path | None = None,
    codex_dir: str | Path | None = None,
    deadline: float | None = None,
) -> int | None:
    """Capture one snapshot; return persisted token total, or None on a skip."""
    if not isinstance(payload, dict):
        return None
    target = _capture_target(payload)
    if target is None:
        return None
    transcript_path, session_id, model = target

    from src.parsers.codex import extract_codex_session_for_capture

    with _parse_backstop(deadline):
        session = extract_codex_session_for_capture(
            transcript_path=transcript_path,
            session_id=session_id,
            model=model,
            codex_dir=codex_dir,
        )
    if session is None:
        return None
    written = write_usage_sessions("codex", [session], db_path=db_path, deadline=deadline)
    return session.usage.total_tokens if written else None


def backfill_codex_usage(
    *,
    db_path: str | Path | None = None,
    codex_dir: str | Path | None = None,
) -> int | None:
    """Perform a one-time complete import, then enable DB-only Codex reads."""
    target_db = resolve_db_path(db_path)
    if is_provider_capture_enabled("codex", db_path=target_db):
        return None
    source_dir = Path(codex_dir).expanduser() if codex_dir else Path.home() / ".codex"
    if not source_dir.is_dir():
        raise FileNotFoundError("Codex history directory is unavailable")

    from src.parsers.codex import extract_all_codex_sessions_for_capture

    sessions = extract_all_codex_sessions_for_capture(codex_dir=source_dir)
    write_usage_sessions("codex", sessions, db_path=target_db)
    # Enable DB-only mode only after the full snapshot transaction succeeds.
    mark_provider_capture_enabled("codex", db_path=target_db)
    return len(sessions)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--backfill",
        action="store_true",
        help="Import all existing Codex usage once and enable database-backed reads.",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Print safe provider-level database counts and Codex capture state.",
    )
    parser.add_argument("--db", type=Path, help="Override the shared usage database path.")
    parser.add_argument("--codex-dir", type=Path, help="Override the Codex history directory.")
    parser.add_argument(
        "--deadline-seconds", type=_deadline_seconds,
        help="Limit total hook capture time, including parsing and database retries.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.status:
        print(json.dumps(get_usage_store_status(args.db), sort_keys=True))
        return 0
    if args.backfill:
        try:
            count = backfill_codex_usage(db_path=args.db, codex_dir=args.codex_dir)
            if count is None:
                print(
                    "Codex capture is already enabled; existing usage snapshots were kept.",
                    file=sys.stderr,
                )
            else:
                print(f"Backfilled {count} Codex usage sessions.", file=sys.stderr)
            return 0
        except Exception as exc:
            print(
                f"Codex usage backfill failed ({type(exc).__name__}).",
                file=sys.stderr,
            )
            return 1

    # Codex expects hook commands to return a JSON object on stdout. Keep all
    # operational output off stdout, and never let telemetry failure block a
    # completed user turn.
    try:
        deadline = (
            None if args.deadline_seconds is None
            else time.monotonic() + args.deadline_seconds
        )
        with _parse_backstop(deadline):
            payload = json.load(sys.stdin)
        token_total = process_hook_payload(
            payload,
            db_path=args.db,
            codex_dir=args.codex_dir,
            deadline=deadline,
        )
        if token_total is None:
            print(
                "Codex usage capture skipped (no stable usage or newer snapshot to persist).",
                file=sys.stderr,
            )
        else:
            print(f"Codex usage captured ({token_total} tokens).", file=sys.stderr)
    except (_DeadlineExpired, UsageStoreDeadlineExceeded):
        print("Codex usage capture skipped (deadline expired).", file=sys.stderr)
    except Exception as exc:
        print(
            f"Codex usage capture skipped ({type(exc).__name__}).",
            file=sys.stderr,
        )
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
