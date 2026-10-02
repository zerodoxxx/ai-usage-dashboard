#!/usr/bin/env python3
"""Publish Claude Code usage snapshots into the shared SQLite DB.

Runs as a Claude Code ``Stop`` / ``SubagentStop`` / ``SessionEnd`` hook. The
hook payload carries only paths and IDs, so the transcript it names is parsed
with the existing Claude parser (``src/parsers/claude.py``) and the resulting
normalized session is written as one complete snapshot. Replaying a snapshot
replaces its event rows, so retries never double count. Transcript text is
never stored.

``--backfill`` imports every transcript under the Claude config directory
(including subagent transcripts) and is safe to re-run.

The hook path never blocks or fails Claude Code: it always exits 0 and reports
problems on stderr only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

# The installer deploys this script beside the project ``src/`` package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.usage_store import (  # noqa: E402
    find_event_owners,
    get_usage_store_status,
    mark_provider_capture_enabled,
    write_usage_sessions,
)

PROVIDER = "claude-code"

_CAPTURE_EVENTS = frozenset({"stop", "subagentstop", "sessionend"})


def _claude_dir(claude_dir: str | Path | None = None) -> Path:
    """Resolve the Claude config directory exactly as the parser does."""
    if claude_dir is not None:
        return Path(claude_dir).expanduser()
    for variable in ("CLAUDE_CONFIG_DIR", "CLAUDE_DIR"):
        if os.environ.get(variable):
            return Path(os.environ[variable]).expanduser()
    return Path.home() / ".claude"


def _capture_file_metadata(path: Path) -> dict[str, Any] | None:
    """Compact identity and revision of a transcript, used to order snapshots."""
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


def _transcript_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip()).expanduser()
    if path.suffix != ".jsonl" or not path.is_file():
        return None
    return path


def _capture_targets(payload: dict[str, Any]) -> list[Path]:
    """Return the transcripts a hook event should publish, or [] to skip."""
    event = str(payload.get("hook_event_name") or payload.get("event") or "").strip().casefold()
    if event and event not in _CAPTURE_EVENTS:
        return []

    targets: list[Path] = []
    if event == "subagentstop":
        # The agent's own transcript holds the child's usage; the parent
        # transcript is captured by its own Stop hook.
        agent = _transcript_path(payload.get("agent_transcript_path"))
        if agent is not None:
            targets.append(agent)
        return targets

    main = _transcript_path(payload.get("transcript_path"))
    if main is not None:
        targets.append(main)
        if event == "sessionend":
            # Final sweep: subagent transcripts live beside the main one.
            subagents = main.with_suffix("") / "subagents"
            if subagents.is_dir():
                targets.extend(sorted(subagents.glob("*.jsonl")))
    return targets


def _parse_stable(path: Path):
    """Parse one transcript and return (session, revision metadata) or None."""
    from src.parsers.claude import _parse_session_file

    for attempt in range(3):
        before = _capture_file_metadata(path)
        if before is None:
            return None
        session = _parse_session_file(path)
        if session is None:
            return None
        if _capture_file_metadata(path) == before:
            return session, before
        if attempt < 2:
            time.sleep(0.03)
    # Still being appended to: keep the conservative (older) revision so a
    # later hook with the final file always wins.
    return session, before


def _with_capture_metadata(session, meta: dict[str, Any] | None):
    if not meta:
        return session
    metadata = dict(session.metadata or {})
    metadata.update(meta)
    return replace(session, metadata=metadata)


def _drop_foreign_events(sessions: list, db_path: str | Path | None) -> list:
    """Remove events the DB already attributes to a session outside this batch.

    A resumed or forked transcript copies earlier responses. Each response ID
    must be counted under exactly one session, so the existing owner keeps it.
    """
    from src.parsers.claude import _refresh_session_prices, _sum_event_usage

    batch_ids = {str(session.id) for session in sessions}
    owners = find_event_owners(
        PROVIDER,
        (event.event_id for session in sessions for event in session.events if event.event_id),
        db_path=db_path,
    )
    if not owners:
        return sessions
    result = []
    for session in sessions:
        kept = [
            event for event in session.events
            if event.event_id is None
            or owners.get(event.event_id) in (None, str(session.id))
            or owners[event.event_id] in batch_ids
        ]
        if not kept:
            continue
        if len(kept) != len(session.events):
            session = replace(
                session, events=kept, usage=_sum_event_usage(kept),
                cost=None, call_count=len(kept),
            )
            _refresh_session_prices(session)
        result.append(session)
    return result


def process_hook_payload(
    payload: Any,
    *,
    db_path: str | Path | None = None,
) -> int | None:
    """Capture the payload's transcript(s); return persisted tokens or None."""
    if not isinstance(payload, dict):
        return None
    targets = _capture_targets(payload)
    if not targets:
        return None

    sessions = []
    for path in targets:
        parsed = _parse_stable(path)
        if parsed is None:
            continue
        session, meta = parsed
        sessions.append(_with_capture_metadata(session, meta))
    sessions = _drop_foreign_events(sessions, db_path)
    if not sessions:
        return None
    written = write_usage_sessions(PROVIDER, sessions, db_path=db_path)
    return sum(session.usage.total_tokens for session in sessions) if written else None


def _merge_same_id(sessions: list) -> list:
    """Combine sessions that share an ID (the store keys rows on it)."""
    from src.parsers.claude import _refresh_session_prices, _sum_event_usage

    grouped: dict[str, list] = defaultdict(list)
    for session in sessions:
        grouped[str(session.id)].append(session)
    merged = []
    for group in grouped.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        seen: set[str] = set()
        events = []
        for session in group:
            for event in session.events:
                if event.event_id is not None:
                    if event.event_id in seen:
                        continue
                    seen.add(event.event_id)
                events.append(event)
        events.sort(key=lambda event: (event.timestamp is None, str(event.timestamp)))
        primary = group[0]
        metadata = {
            key: value for key, value in (primary.metadata or {}).items()
            if not key.startswith("capture_")
        }
        combined = replace(
            primary, events=events, usage=_sum_event_usage(events), cost=None,
            call_count=len(events), metadata=metadata,
        )
        _refresh_session_prices(combined)
        merged.append(combined)
    return merged


def backfill_claude_usage(
    *,
    db_path: str | Path | None = None,
    claude_dir: str | Path | None = None,
    enable_capture: bool = True,
) -> tuple[int, int]:
    """Import every transcript once; return (sessions written, events written)."""
    base = _claude_dir(claude_dir)
    if not base.is_dir():
        raise FileNotFoundError("Claude history directory is unavailable")

    from src.parsers.claude import ClaudeCodeSource, _parse_session_file, _session_files

    files = _session_files(base)
    # Revisions are taken before parsing so a transcript growing mid-backfill
    # is recorded as older than its next hook snapshot.
    revisions = {path: _capture_file_metadata(path) for path in files}
    # extract_sessions applies the parser's cross-transcript de-duplication.
    sessions = ClaudeCodeSource().extract_sessions(base)

    files_by_id: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        parsed = _parse_session_file(path)  # parse-cache hit after extract_sessions
        if parsed is not None:
            files_by_id[str(parsed.id)].append(path)

    prepared = []
    for session in sessions:
        paths = files_by_id.get(str(session.id), [])
        meta = revisions.get(paths[0]) if len(paths) == 1 else None
        prepared.append(_with_capture_metadata(session, meta))
    prepared = _drop_foreign_events(_merge_same_id(prepared), db_path)

    write_usage_sessions(PROVIDER, prepared, db_path=db_path)
    if enable_capture:
        mark_provider_capture_enabled(PROVIDER, db_path=db_path)
    return len(prepared), sum(len(session.events) for session in prepared)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--backfill",
        action="store_true",
        help="Import all existing Claude Code transcripts (safe to re-run).",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Print safe provider-level database counts and capture state.",
    )
    parser.add_argument(
        "--no-enable-capture",
        action="store_true",
        help="With --backfill, do not mark Claude capture enabled in the database.",
    )
    parser.add_argument("--db", type=Path, help="Override the shared usage database path.")
    parser.add_argument("--claude-dir", type=Path, help="Override the Claude config directory.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.status:
        print(json.dumps(get_usage_store_status(args.db), sort_keys=True))
        return 0
    if args.backfill:
        try:
            sessions, events = backfill_claude_usage(
                db_path=args.db,
                claude_dir=args.claude_dir,
                enable_capture=not args.no_enable_capture,
            )
            print(
                f"Backfilled {sessions} Claude Code sessions ({events} events).",
                file=sys.stderr,
            )
            return 0
        except Exception as exc:
            print(
                f"Claude usage backfill failed ({type(exc).__name__}: {exc}).",
                file=sys.stderr,
            )
            return 1

    # Hook mode: never block or fail Claude Code, and keep stdout empty.
    try:
        payload = json.load(sys.stdin)
        token_total = process_hook_payload(payload, db_path=args.db)
        if token_total is None:
            print("Claude usage capture skipped (nothing to persist).", file=sys.stderr)
        else:
            print(f"Claude usage captured ({token_total} tokens).", file=sys.stderr)
    except BaseException as exc:  # noqa: BLE001 - telemetry must never fail the host
        print(f"Claude usage capture skipped ({type(exc).__name__}: {exc}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
