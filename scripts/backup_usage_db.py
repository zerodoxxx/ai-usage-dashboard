#!/usr/bin/env python3
"""Back up the shared usage database without opening the source for writes."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.usage_store import resolve_db_path

_BACKUP_NAME = re.compile(r"usage-\d{8}T\d{6}\.\d{6}Z\.db\Z")


class BackupError(RuntimeError):
    """A complete, verified backup could not be made."""


def _provider_counts(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for table, count_key, token_key in (
        ("sessions", "sessions", "session_tokens"),
        ("token_events", "events", "event_tokens"),
    ):
        for provider, count, tokens in connection.execute(
            f"SELECT provider, COUNT(*), COALESCE(SUM(total_tokens), 0) FROM {table} GROUP BY provider"
        ):
            values = counts.setdefault(str(provider), {
                "sessions": 0, "events": 0, "session_tokens": 0, "event_tokens": 0,
            })
            values[count_key] = int(count)
            values[token_key] = int(tokens)
    return counts


def _integrity_check(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise BackupError("backup integrity check failed")


def backup_usage_db(
    db_path: str | Path | None = None,
    *,
    dest_dir: str | Path | None = None,
    keep: int = 14,
) -> dict:
    """Publish a verified SQLite snapshot, then rotate older verified backups."""
    temporary: Path | None = None
    try:
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
            raise BackupError("keep must be at least one")
        source_path = Path(resolve_db_path(db_path)).expanduser().resolve(strict=True)
        if not source_path.is_file():
            raise BackupError("usage database is not a regular file")
        destination = (Path(dest_dir).expanduser() if dest_dir is not None else source_path.parent / "backups").resolve()
        uri = f"file:{quote(str(source_path), safe='/')}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as source:
            source.execute("PRAGMA query_only = ON")
            # Counts and backup must see the same committed snapshot even
            # when a capture writer commits during the backup.
            source.execute("BEGIN")
            expected = _provider_counts(source)
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".usage-backup-", suffix=".tmp", dir=destination)
            os.close(descriptor)
            temporary = Path(name)
            saved = sqlite3.connect(temporary)
            try:
                source.backup(saved)
                saved.execute("PRAGMA journal_mode = DELETE")
            finally:
                saved.close()
        saved_uri = f"file:{quote(str(temporary), safe='/')}?mode=ro"
        saved = sqlite3.connect(saved_uri, uri=True)
        try:
            _integrity_check(saved)
            actual = _provider_counts(saved)
            if actual != expected:
                raise BackupError("backup provider counts do not match source snapshot")
        finally:
            saved.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_path = destination / f"usage-{timestamp}.db"
        # Exclusive publication cannot replace an existing backup on collision.
        os.link(temporary, backup_path)
        temporary.unlink()
        temporary = None
        directory_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        backups = sorted(
            (path for path in destination.iterdir()
             if _BACKUP_NAME.fullmatch(path.name) and path.is_file()
             and not path.is_symlink() and path != source_path),
            key=lambda path: path.name,
            reverse=True,
        )
        rotated = 0
        for old in backups[keep:]:
            if old != backup_path:
                old.unlink()
                rotated += 1
        return {"status": "complete", "path": str(backup_path), "providers": actual, "rotated": rotated}
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        raise BackupError(str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Override the shared usage database path.")
    parser.add_argument("--dest-dir", type=Path, help="Backup directory (default: resolved database parent/backups).")
    parser.add_argument("--keep", type=int, default=14, help="Number of newest backups to retain (default: 14).")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON.")
    args = parser.parse_args(argv)
    try:
        record = backup_usage_db(args.db, dest_dir=args.dest_dir, keep=args.keep)
    except BackupError as exc:
        record = {"status": "backup_failed", "error": str(exc)}
        print(json.dumps(record, sort_keys=True) if args.json else str(exc))
        return 1
    print(json.dumps(record, sort_keys=True) if args.json else f"Verified backup: {record['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
