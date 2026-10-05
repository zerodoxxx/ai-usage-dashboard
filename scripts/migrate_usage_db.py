#!/usr/bin/env python3
"""Copy the legacy shared usage database to the tool-neutral default path.

Uses the sqlite3 backup API, so content still sitting in the source's WAL is
included. The copy is written to a temporary file next to the target, verified
(per-provider session/event counts and token sums, plus ``PRAGMA
integrity_check``), and only then moved into place. Existing targets are backed up before forced
replacement. After success the source is archived, unless --keep-source is set.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import sqlite3
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.usage_store import LEGACY_DB_RELATIVE_PATH, resolve_db_path  # noqa: E402

LEGACY_PROVIDER = "antigravity"  # rows predating the provider column


class MigrationError(RuntimeError):
    """Raised when the migration cannot proceed or verification fails."""


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def summarize(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Return {provider: {sessions, events, total_tokens}} for a usage DB."""
    result: dict[str, dict[str, int]] = {}

    def entry(provider: str) -> dict[str, int]:
        return result.setdefault(provider, {"sessions": 0, "events": 0, "total_tokens": 0})

    for table, count_key, sum_key in (
        ("sessions", "sessions", "total_tokens"),
        ("token_events", "events", None),
    ):
        columns = _columns(connection, table)
        if not columns:
            continue
        provider_expr = (
            f"COALESCE(NULLIF(provider, ''), '{LEGACY_PROVIDER}')"
            if "provider" in columns
            else f"'{LEGACY_PROVIDER}'"
        )
        tokens_expr = "COALESCE(SUM(total_tokens), 0)"
        for provider, count, tokens in connection.execute(
            f"SELECT {provider_expr}, COUNT(*), {tokens_expr} FROM {table} GROUP BY 1"
        ):
            row = entry(str(provider))
            row[count_key] += int(count)
            if sum_key:
                row["total_tokens"] = int(tokens)
    return result


def _integrity_ok(path: Path) -> bool:
    connection = _open_read_only(path)
    try:
        rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    finally:
        connection.close()
    return rows == ["ok"]


def _summarize_path(path: Path) -> dict[str, dict[str, int]]:
    connection = _open_read_only(path)
    try:
        return summarize(connection)
    finally:
        connection.close()


def format_table(
    source: dict[str, dict[str, int]], target: dict[str, dict[str, int]] | None
) -> str:
    header = ["provider", "src sessions", "src events", "src tokens"]
    if target is not None:
        header += ["dst sessions", "dst events", "dst tokens", "match"]
    rows = [header]
    for provider in sorted(set(source) | set(target or {})):
        src = source.get(provider, {"sessions": 0, "events": 0, "total_tokens": 0})
        line = [provider, str(src["sessions"]), str(src["events"]), str(src["total_tokens"])]
        if target is not None:
            dst = target.get(provider, {"sessions": 0, "events": 0, "total_tokens": 0})
            line += [
                str(dst["sessions"]), str(dst["events"]), str(dst["total_tokens"]),
                "ok" if src == dst else "MISMATCH",
            ]
        rows.append(line)
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    )


def migrate(
    source: Path, target: Path, *, force: bool = False, dry_run: bool = False,
    keep_source: bool = False
) -> tuple[dict, dict | None]:
    """Copy and verify. Returns (source_summary, target_summary or None)."""
    source = Path(source).expanduser()
    target = Path(target).expanduser()
    if not source.is_file():
        raise MigrationError(f"source database not found: {source}")
    if source.resolve() == target.resolve():
        raise MigrationError("source and target are the same file")
    if target.exists() and not force:
        raise MigrationError(f"target already exists: {target} (use --force to replace it)")

    source_summary = _summarize_path(source)
    if dry_run:
        return source_summary, None

    now = datetime.now(timezone.utc)
    archived_source = source.with_name(f"{source.name}.migrated-{now:%Y-%m-%d}")
    if not keep_source and any(
        Path(str(archived_source) + suffix).exists() for suffix in ("", "-wal", "-shm")
    ):
        raise MigrationError(f"source archive already exists: {archived_source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.migrating-{os.getpid()}")
    try:
        src_conn = _open_read_only(source)
        try:
            dst_conn = sqlite3.connect(str(temporary))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        target_summary = _summarize_path(temporary)
        if target_summary != source_summary:
            raise MigrationError(
                "verification failed: per-provider counts or token sums differ\n"
                + format_table(source_summary, target_summary)
            )
        if not _integrity_ok(temporary):
            raise MigrationError("verification failed: PRAGMA integrity_check is not ok")

        if target.exists() and not force:
            raise MigrationError(f"target appeared during migration: {target}")
        if force and target.exists():
            backup = target.with_name(f"{target.name}.{now:%Y%m%dT%H%M%S%fZ}.bak")
            # The SQLite backup API preserves committed data still in the WAL.
            old = None
            saved = None
            try:
                old = _open_read_only(target)
                saved = sqlite3.connect(backup)
                old.backup(saved)
            except sqlite3.DatabaseError:
                # Preserve even a non-SQLite target, but never discard a live WAL.
                if Path(str(target) + "-wal").exists():
                    raise MigrationError("cannot back up target with an unreadable WAL")
                if saved is not None:
                    saved.close()
                    saved = None
                shutil.copy2(target, backup)
            finally:
                if saved is not None:
                    saved.close()
                if old is not None:
                    old.close()
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(temporary, target)
        if not keep_source:
            source.rename(archived_source)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(source) + suffix)
                if sidecar.exists():
                    sidecar.rename(Path(str(archived_source) + suffix))
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
    return source_summary, target_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, default=Path.home() / LEGACY_DB_RELATIVE_PATH,
                        help="database to copy (default: the legacy Antigravity path)")
    parser.add_argument("--target", type=Path, default=resolve_db_path(),
                        help="destination (default: AI_USAGE_DB_PATH or ~/.local/share/ai-usage/usage.db)")
    parser.add_argument("--force", action="store_true", help="back up and replace an existing target")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the source summary only; write nothing")
    parser.add_argument("--keep-source", action="store_true",
                        help="leave the source at its original path after a verified copy")
    args = parser.parse_args(argv)
    try:
        source_summary, target_summary = migrate(
            args.source, args.target, force=args.force, dry_run=args.dry_run,
            keep_source=args.keep_source
        )
    except (MigrationError, sqlite3.Error, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(format_table(source_summary, target_summary))
    if args.dry_run:
        print(f"\ndry run: would copy {args.source} -> {args.target}")
    else:
        disposition = "source left untouched" if args.keep_source else "source archived"
        print(f"\ncopied and verified: {args.source} -> {args.target} ({disposition})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
