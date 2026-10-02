#!/usr/bin/env python3
"""Safely preview or apply age-based Codex transcript retention.

The default command is a read-only 15-day preview. ``--apply`` uses Codex's
thread deletion command only after confirming Codex is closed, every thread in
the session tree has provider-tagged usage in the shared SQLite database, and
every current rollout fragment still matches its captured source revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.usage_store import is_provider_capture_enabled, read_usage_sessions, resolve_db_path

DEFAULT_DAYS = 15
SAFE_CODEX_VERSION = "0.159.2"
_ROLLOUT_NAME_ID = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_ACTIVITY_COLUMNS = (
    "updated_at",
    "recency_at",
    "activity_at",
    "last_activity_at",
)
_PARENT_EDGE_COLUMNS = (
    "parent_thread_id",
    "parent_id",
    "parent_session_id",
    "source_thread_id",
)
_CHILD_EDGE_COLUMNS = (
    "child_thread_id",
    "child_id",
    "child_session_id",
    "spawned_thread_id",
)


@dataclass(frozen=True)
class TranscriptSource:
    path: Path
    source_hash: str
    mtime_ns: int
    ctime_ns: int
    size: int


@dataclass(frozen=True)
class RetentionTree:
    root_id: str
    thread_ids: tuple[str, ...]
    sources: tuple[TranscriptSource, ...]
    latest_activity: datetime

    @property
    def reclaimable_bytes(self) -> int:
        return sum(source.size for source in self.sources)


@dataclass
class RetentionPlan:
    cutoff: datetime
    trees: list[RetentionTree] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def skip(self, reason: str, count: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + count

    @property
    def transcript_count(self) -> int:
        return sum(len(tree.sources) for tree in self.trees)

    @property
    def thread_count(self) -> int:
        return sum(len(tree.thread_ids) for tree in self.trees)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(tree.reclaimable_bytes for tree in self.trees)


def _utc_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 1e11:
                seconds /= 1000.0
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        if seconds > 1e11:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_codex_root(path: str | Path | None) -> Path:
    root = Path(path).expanduser() if path is not None else Path.home() / ".codex"
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Codex history directory is missing or is a symlink")
    return root.resolve(strict=True)


def _safe_regular_file(path: Path, root: Path) -> tuple[Path, os.stat_result] | None:
    """Resolve a transcript without following a symlink in any path segment."""
    try:
        lexical_path = path if path.is_absolute() else root / path
        relative = lexical_path.relative_to(root)
        current = root
        for component in relative.parts:
            if component in {".", ".."}:
                return None
            current = current / component
            if current.is_symlink():
                return None
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
        stat = resolved.stat(follow_symlinks=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved, stat


def _source_for_path(path: Path, root: Path) -> TranscriptSource | None:
    safe = _safe_regular_file(path, root)
    if safe is None:
        return None
    resolved, stat = safe
    return TranscriptSource(
        path=resolved,
        source_hash=hashlib.sha256(str(resolved).encode("utf-8")).hexdigest(),
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
        size=stat.st_size,
    )


def _state_snapshot(codex_root: Path) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str]]]:
    state_path = codex_root / "state_5.sqlite"
    safe = _safe_regular_file(state_path, codex_root)
    if safe is None:
        raise RuntimeError("Codex thread database is unavailable or unsafe")
    resolved, _stat = safe
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "threads" not in tables or "thread_spawn_edges" not in tables:
            raise RuntimeError("Codex thread tree schema is unavailable")
        thread_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)")}
        if "id" not in thread_columns or "rollout_path" not in thread_columns:
            raise RuntimeError("Codex thread tree schema is incomplete")
        activity_columns = [name for name in _ACTIVITY_COLUMNS if name in thread_columns]
        if not activity_columns:
            raise RuntimeError("Codex thread activity timestamps are unavailable")

        selected = ["id", "rollout_path", *activity_columns]
        quoted = ", ".join(f'"{name}"' for name in selected)
        rows = connection.execute(f"SELECT {quoted} FROM threads").fetchall()
        threads: dict[str, dict[str, Any]] = {}
        for row in rows:
            native_id = str(row["id"] or "").strip()
            if native_id:
                threads[native_id.casefold()] = {
                    "id": native_id,
                    "rollout_path": row["rollout_path"],
                    "activity_values": [row[name] for name in activity_columns],
                }

        edge_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(thread_spawn_edges)")
        }
        parent_column = next((name for name in _PARENT_EDGE_COLUMNS if name in edge_columns), None)
        child_column = next((name for name in _CHILD_EDGE_COLUMNS if name in edge_columns), None)
        if parent_column is None or child_column is None:
            raise RuntimeError("Codex spawned-thread relationships are unavailable")
        edges = connection.execute(
            f'SELECT "{parent_column}", "{child_column}" FROM thread_spawn_edges'
        ).fetchall()
        relationships = [
            (str(row[0]).casefold(), str(row[1]).casefold())
            for row in edges
            if row[0] is not None and row[1] is not None
        ]
        return threads, relationships
    finally:
        connection.close()


def _discover_transcripts(codex_root: Path) -> tuple[dict[str, list[TranscriptSource]], set[str]]:
    """Index rollout fragments by the UUID in their filename and state path."""
    sessions_root = codex_root / "sessions"
    if sessions_root.is_symlink() or not sessions_root.is_dir():
        return {}, set()
    safe_sessions_root = sessions_root.resolve(strict=True)
    safe_sessions_root.relative_to(codex_root)
    by_thread: dict[str, dict[str, TranscriptSource]] = {}
    unsafe_ids: set[str] = set()

    def traversal_error(_error: OSError) -> None:
        raise RuntimeError("Codex transcript directories could not be fully inspected")

    for current_dir, directory_names, filenames in os.walk(
        safe_sessions_root,
        followlinks=False,
        onerror=traversal_error,
    ):
        current_path = Path(current_dir)
        if any((current_path / name).is_symlink() for name in directory_names):
            # A linked directory could hide another fragment for a thread
            # whose indexed rollout happens to be elsewhere. Do not qualify
            # any tree from a partial inventory.
            raise RuntimeError("Codex transcript tree contains a symlinked directory")
        for filename in filenames:
            if not (filename.startswith("rollout-") and filename.endswith(".jsonl")):
                continue
            path = current_path / filename
            match = _ROLLOUT_NAME_ID.search(filename)
            if not match:
                continue
            thread_id = match.group(1).casefold()
            source = _source_for_path(path, codex_root)
            if source is None:
                unsafe_ids.add(thread_id)
                continue
            by_thread.setdefault(thread_id, {})[str(source.path)] = source

    return {
        thread_id: list(sources.values())
        for thread_id, sources in by_thread.items()
    }, unsafe_ids


def _graph_components(
    threads: dict[str, dict[str, Any]],
    edges: Iterable[tuple[str, str]],
) -> list[set[str]]:
    adjacency: dict[str, set[str]] = {thread_id: set() for thread_id in threads}
    for parent, child in edges:
        adjacency.setdefault(parent, set()).add(child)
        adjacency.setdefault(child, set()).add(parent)
    components: list[set[str]] = []
    unseen = set(adjacency)
    while unseen:
        start = unseen.pop()
        component = {start}
        pending = [start]
        while pending:
            current = pending.pop()
            for neighbor in adjacency.get(current, ()):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    component.add(neighbor)
                    pending.append(neighbor)
        components.append(component)
    return components


def _verified_manifest(session: Any) -> dict[str, dict[str, int]]:
    metadata = session.metadata if isinstance(session.metadata, dict) else {}
    raw_sources = metadata.get("capture_sources")
    manifest: dict[str, dict[str, int]] = {}
    if isinstance(raw_sources, dict):
        for source_hash, revision in raw_sources.items():
            if not isinstance(revision, dict):
                continue
            try:
                manifest[str(source_hash)] = {
                    "capture_mtime_ns": int(revision["capture_mtime_ns"]),
                    "capture_ctime_ns": int(revision["capture_ctime_ns"]),
                    "capture_size": int(revision["capture_size"]),
                }
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
    source_hash = metadata.get("capture_source_hash")
    if source_hash and source_hash not in manifest:
        try:
            manifest[str(source_hash)] = {
                "capture_mtime_ns": int(metadata["capture_mtime_ns"]),
                "capture_ctime_ns": int(metadata["capture_ctime_ns"]),
                "capture_size": int(metadata["capture_size"]),
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    return manifest


def _revision_matches(source: TranscriptSource, revision: dict[str, int]) -> bool:
    return (
        revision.get("capture_mtime_ns") == source.mtime_ns
        and revision.get("capture_ctime_ns") == source.ctime_ns
        and revision.get("capture_size") == source.size
    )


def _session_activity(session: Any) -> datetime | None:
    values = (getattr(session, "activity_at", None), getattr(session, "end_time", None))
    parsed = [value for item in values if (value := _utc_datetime(item)) is not None]
    return max(parsed) if parsed else None


def build_retention_plan(
    *,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
    codex_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> RetentionPlan:
    """Build a read-only plan from verified Codex usage and local metadata."""
    if days < 1:
        raise ValueError("retention days must be at least one")
    current_time = now or datetime.now(timezone.utc)
    current_time = current_time.astimezone(timezone.utc)
    plan = RetentionPlan(cutoff=current_time - timedelta(days=days))
    try:
        codex_root = _safe_codex_root(codex_dir)
        if not is_provider_capture_enabled("codex", db_path=db_path):
            plan.skip("sqlite_capture_not_enabled")
            return plan
        threads, edges = _state_snapshot(codex_root)
        if not threads:
            return plan
        sessions = read_usage_sessions("codex", db_path=db_path)
        sessions_by_id = {
            str(session.id).casefold(): session
            for session in sessions
            if str(session.provider or session.tool).casefold() == "codex"
        }
        files_by_thread, unsafe_ids = _discover_transcripts(codex_root)

        incoming: dict[str, set[str]] = {}
        outgoing: dict[str, set[str]] = {}
        for parent, child in edges:
            incoming.setdefault(child, set()).add(parent)
            outgoing.setdefault(parent, set()).add(child)

        for component in _graph_components(threads, edges):
            root_ids = [
                node for node in component
                if not (incoming.get(node, set()) & component)
            ]
            if len(root_ids) != 1:
                plan.skip("ambiguous_thread_tree")
                continue
            root_id = root_ids[0]
            tree_ids = set()
            pending = [root_id]
            has_cycle = False
            while pending:
                node = pending.pop()
                if node in tree_ids:
                    has_cycle = True
                    continue
                tree_ids.add(node)
                pending.extend(outgoing.get(node, set()) & component)
            if has_cycle or tree_ids != component or any(
                len(incoming.get(node, set()) & component) > 1
                for node in component if node != root_id
            ):
                plan.skip("ambiguous_thread_tree")
                continue
            if any(node not in threads for node in component):
                plan.skip("missing_thread_metadata")
                continue

            sources: dict[str, TranscriptSource] = {}
            activity_values: list[datetime] = []
            state_activity_unknown = False
            capture_missing = False
            source_mismatch = False
            no_usage = False
            for thread_id in component:
                thread = threads[thread_id]
                parsed_thread_times = []
                for raw_time in thread["activity_values"]:
                    if raw_time is None or str(raw_time).strip() == "":
                        continue
                    parsed = _utc_datetime(raw_time)
                    if parsed is None:
                        state_activity_unknown = True
                    else:
                        parsed_thread_times.append(parsed)
                if not parsed_thread_times:
                    state_activity_unknown = True
                activity_values.extend(parsed_thread_times)

                session = sessions_by_id.get(thread_id)
                if session is None:
                    capture_missing = True
                    continue
                if (
                    int(session.usage.total_tokens or 0) <= 0
                    or not session.events
                    or str(session.metadata.get("capture_quality") or "") == "state-summary"
                ):
                    no_usage = True
                    continue
                manifest = _verified_manifest(session)
                current_sources = files_by_thread.get(thread_id, [])
                rollout_path = thread.get("rollout_path")
                if rollout_path:
                    pointed_source = _source_for_path(Path(str(rollout_path)).expanduser(), codex_root)
                    if pointed_source is None:
                        capture_missing = True
                    else:
                        current_sources = [*current_sources, pointed_source]
                if thread_id in unsafe_ids:
                    source_mismatch = True
                unique_sources = {str(source.path): source for source in current_sources}
                if not unique_sources:
                    capture_missing = True
                for source in unique_sources.values():
                    revision = manifest.get(source.source_hash)
                    if revision is None or not _revision_matches(source, revision):
                        source_mismatch = True
                    else:
                        sources[str(source.path)] = source
                    activity_values.append(datetime.fromtimestamp(
                        source.mtime_ns / 1_000_000_000,
                        tz=timezone.utc,
                    ))
                session_time = _session_activity(session)
                if session_time is not None:
                    activity_values.append(session_time)

            if state_activity_unknown:
                plan.skip("thread_activity_unknown")
                continue
            if source_mismatch:
                plan.skip("transcript_not_exactly_captured")
                continue
            if capture_missing:
                plan.skip("sqlite_usage_missing")
                continue
            if no_usage:
                plan.skip("no_verified_token_events")
                continue
            if len(sources) == 0:
                plan.skip("no_transcripts")
                continue
            latest_activity = max(activity_values)
            if latest_activity > plan.cutoff:
                plan.skip("within_retention_window")
                continue
            plan.trees.append(RetentionTree(
                root_id=str(threads[root_id]["id"]),
                thread_ids=tuple(str(threads[node]["id"]) for node in sorted(component)),
                sources=tuple(sorted(sources.values(), key=lambda source: str(source.path))),
                latest_activity=latest_activity,
            ))
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        plan.trees.clear()
        plan.error = f"retention scan failed ({type(exc).__name__})"
    return plan


def _process_name_is_codex(value: str) -> bool:
    name = Path(value.strip().strip('"')).name.casefold()
    return (
        name in {
            "codex",
            "codex.exe",
            "codex-cli",
            "codex-server",
            "codex-app-server",
            "codex-tui",
        }
        or name.startswith("codex helper")
        or name.startswith("codex crashpad")
    )


def find_active_codex_processes(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[str]:
    """Return Codex executable names; fail closed when process state is unknown."""
    try:
        result = run(
            ["ps", "-axo", "pid=,comm="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not verify that Codex is closed") from exc
    found: list[str] = []
    saw_current_process = False
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid == os.getpid():
            saw_current_process = True
            continue
        command = fields[1].strip()
        if _process_name_is_codex(command):
            found.append(Path(command).name)
    if not saw_current_process:
        raise RuntimeError("could not verify that Codex is closed")
    return found


def _tree_still_eligible(
    root_id: str,
    *,
    days: int,
    codex_dir: str | Path | None,
    db_path: str | Path | None,
    now: datetime,
) -> RetentionTree | None:
    fresh = build_retention_plan(
        days=days,
        now=now,
        codex_dir=codex_dir,
        db_path=db_path,
    )
    return next((tree for tree in fresh.trees if tree.root_id.casefold() == root_id.casefold()), None)


def apply_retention_plan(
    plan: RetentionPlan,
    *,
    days: int = DEFAULT_DAYS,
    codex_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    process_check: Callable[[], list[str]] = find_active_codex_processes,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    codex_executable: str | None = None,
    codex_cli_path: str | None = None,
    expected_codex_version: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[int, list[str]]:
    """Delete eligible trees through Codex after closed-state and race checks."""
    if plan.error:
        return 0, [plan.error]
    try:
        active = process_check()
    except RuntimeError as exc:
        return 0, [str(exc)]
    if active:
        return 0, ["Codex is running; no conversations were deleted"]
    executable = codex_cli_path or codex_executable or shutil.which("codex")
    if not executable:
        return 0, ["Codex CLI is unavailable; no conversations were deleted"]
    if expected_codex_version != SAFE_CODEX_VERSION:
        return 0, ["Codex version pin is missing or unsupported; no conversations were deleted"]
    try:
        version_result = run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 0, ["Could not verify the Codex CLI version; no conversations were deleted"]
    version_text = f"{version_result.stdout}\n{version_result.stderr}"
    reported_versions = re.findall(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", version_text)
    if version_result.returncode != 0 or reported_versions != [expected_codex_version]:
        return 0, ["Codex CLI version changed; no conversations were deleted"]

    deleted = 0
    errors: list[str] = []
    for tree in plan.trees:
        try:
            active = process_check()
        except RuntimeError as exc:
            errors.append(str(exc))
            break
        if active:
            errors.append("Codex started during retention; remaining conversations were skipped")
            break
        fresh_now = now().astimezone(timezone.utc)
        fresh_tree = _tree_still_eligible(
            tree.root_id,
            days=days,
            codex_dir=codex_dir,
            db_path=db_path,
            now=fresh_now,
        )
        if fresh_tree is None:
            errors.append("an eligible conversation tree changed or is no longer eligible; skipped")
            continue
        try:
            active = process_check()
        except RuntimeError as exc:
            errors.append(str(exc))
            break
        if active:
            errors.append("Codex started during retention; remaining conversations were skipped")
            break
        try:
            result = run(
                [executable, "--no-daemon", "delete", fresh_tree.root_id, "--force"],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            errors.append("Codex deletion failed; remaining work stopped")
            break
        if result.returncode != 0:
            # Codex holds a rollout writer lock while a turn is live. A failed
            # delete can therefore indicate a newly active thread; stop here.
            errors.append("Codex refused deletion; remaining work stopped")
            break
        if any(source.path.exists() for source in fresh_tree.sources):
            errors.append("some transcripts remain after deletion; remaining work stopped")
            break
        deleted += 1
    return deleted, errors


def _format_plan(plan: RetentionPlan, *, days: int, active_process_count: int | None = None) -> str:
    total_mib = plan.reclaimable_bytes / (1024 * 1024)
    lines = [
        f"Codex retention preview ({days} days; cutoff {plan.cutoff.isoformat()}):",
        f"  eligible trees: {len(plan.trees)}",
        f"  conversations including spawned sessions: {plan.thread_count}",
        f"  transcripts: {plan.transcript_count}",
        f"  transcript bytes reclaimable: {plan.reclaimable_bytes:,} ({total_mib:.1f} MiB)",
    ]
    if active_process_count is not None:
        lines.append(
            "  apply status: Codex is running; scheduled deletion will wait until it closes"
            if active_process_count else
            "  apply status: Codex is closed"
        )
    if plan.error:
        lines.append(f"  safety: {plan.error}")
    if plan.skipped:
        rendered = ", ".join(f"{reason}={count}" for reason, count in sorted(plan.skipped.items()))
        lines.append(f"  protected or skipped groups: {rendered}")
    for tree in sorted(plan.trees, key=lambda item: (item.latest_activity, item.root_id.casefold())):
        size_mib = tree.reclaimable_bytes / (1024 * 1024)
        lines.append(
            f"  tree: {len(tree.thread_ids)} threads, {len(tree.sources)} transcripts, {size_mib:.1f} MiB, "
            f"last activity {tree.latest_activity.isoformat()}"
        )
    return "\n".join(lines)


def _append_aggregate_log(path: Path, record: dict[str, Any]) -> None:
    """Append only aggregate job results and keep a small bounded log."""
    path = path.expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = path.with_name(f"{path.name}.1")
    if path.is_symlink() or backup.is_symlink():
        raise OSError("refusing a symlinked retention log")
    line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if path.exists() and path.stat().st_size + len(line) > 512 * 1024:
        backup.unlink(missing_ok=True)
        os.replace(path, backup)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(line)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Keep at least this many days (default: 15).")
    parser.add_argument("--codex-dir", type=Path, help="Override the Codex history directory.")
    parser.add_argument("--db", type=Path, help="Override the shared usage database path.")
    parser.add_argument(
        "--codex-version",
        help=f"Required apply-time safety pin (currently {SAFE_CODEX_VERSION}).",
    )
    parser.add_argument("--codex-cli", type=Path, help="Override the Codex executable used by apply.")
    parser.add_argument("--apply", action="store_true", help="Apply the retention plan; default is preview only.")
    parser.add_argument("--json", action="store_true", help="Print aggregate preview data as JSON.")
    parser.add_argument("--log-file", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.days < 1:
        print("Retention days must be at least one.", file=sys.stderr)
        return 2
    active_count: int | None = None
    if args.apply:
        try:
            active = find_active_codex_processes()
        except RuntimeError as exc:
            record = {"status": "safety_check_failed", "error": type(exc).__name__}
            print(json.dumps(record, sort_keys=True) if args.json else "Could not verify Codex is closed.", file=sys.stderr)
            if args.log_file:
                try:
                    _append_aggregate_log(args.log_file, record)
                except OSError:
                    pass
            return 2
        active_count = len(active)
        if active:
            record = {"status": "deferred_codex_running", "active_codex_processes": active_count}
            print(json.dumps(record, sort_keys=True) if args.json else "Codex is running; scheduled deletion will wait until it closes.")
            if args.log_file:
                try:
                    _append_aggregate_log(args.log_file, record)
                except OSError:
                    pass
            return 0

    try:
        plan = build_retention_plan(
            days=args.days,
            codex_dir=args.codex_dir,
            db_path=args.db,
        )
    except ValueError as exc:
        print(f"Codex retention preview unavailable ({type(exc).__name__}).", file=sys.stderr)
        return 2

    if args.json:
        record = {
            "days": args.days,
            "cutoff": plan.cutoff.isoformat(),
            "eligible_trees": len(plan.trees),
            "eligible_threads": plan.thread_count,
            "transcripts": plan.transcript_count,
            "reclaimable_bytes": plan.reclaimable_bytes,
            "skipped": plan.skipped,
            "error": plan.error,
            "active_codex_processes": active_count,
            "status": "preview" if not args.apply else "ready_to_apply",
        }
        print(json.dumps(record, sort_keys=True))
    else:
        record = None
        print(_format_plan(plan, days=args.days, active_process_count=active_count))

    if args.log_file and record is not None:
        try:
            _append_aggregate_log(args.log_file, record)
        except OSError:
            pass

    if not args.apply or plan.error or not plan.trees:
        return 0 if not plan.error else 2
    deleted, errors = apply_retention_plan(
        plan,
        days=args.days,
        codex_dir=args.codex_dir,
        db_path=args.db,
        codex_cli_path=str(args.codex_cli) if args.codex_cli else None,
        expected_codex_version=args.codex_version,
    )
    result_record = {
        "status": "complete" if not errors else "stopped_safely",
        "eligible_trees": len(plan.trees),
        "eligible_threads": plan.thread_count,
        "transcripts": plan.transcript_count,
        "reclaimable_bytes": plan.reclaimable_bytes,
        "deleted_trees": deleted,
        "errors": errors,
        "sqlite_usage_preserved": True,
    }
    if args.json:
        print(json.dumps(result_record, sort_keys=True))
    else:
        print(f"Deleted {deleted} Codex conversation trees; usage remains in the shared SQLite database.")
    for error in errors:
        if not args.json:
            print(error, file=sys.stderr)
    if args.log_file:
        try:
            _append_aggregate_log(args.log_file, result_record)
        except OSError:
            pass
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
