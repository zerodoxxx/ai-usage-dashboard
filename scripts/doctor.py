#!/usr/bin/env python3
"""Read-only usage database and publisher health checks (table or --json)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import quote

# A diagnostic must not create bytecode in the repository or deployed releases.
sys.dont_write_bytecode = True
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import install_claude_usage_hooks as claude_installer  # noqa: E402
from scripts import install_codex_usage_hooks as codex_installer  # noqa: E402
from src.usage_store import DEFAULT_DB_RELATIVE_PATH, SCHEMA_VERSION, resolve_db_path  # noqa: E402

LABEL = "com.zerodoxxx.ai-usage-dashboard.codex-retention"
STALE_SECONDS = 24 * 60 * 60


class _PayloadView:
    """Expose the installer's selected files to its digest helper without copying."""

    def __init__(self, installer):
        self.root = installer.REPOSITORY_ROOT
        self.entrypoint = installer.PUBLISHER_RELATIVE_PATH

    def __fspath__(self):
        return str(self.root)

    def rglob(self, pattern):
        entrypoint = self.root / self.entrypoint
        package = self.root / "src"
        if entrypoint.is_symlink() or not entrypoint.is_file():
            raise OSError(f"missing publisher: {entrypoint}")
        if package.is_symlink() or not package.is_dir():
            raise OSError(f"missing dependencies: {package}")
        return [entrypoint] + [
            path for path in package.rglob(pattern)
            if path.is_file() and not path.is_symlink()
            and not codex_installer._is_denied_source_path(path.relative_to(self.root))
        ]


def expected_digest(installer) -> str:
    return codex_installer._artifact_digest(_PayloadView(installer))


def _path(value: str | Path, home: Path) -> Path:
    raw = str(value)
    if raw == "~" or raw.startswith("~/"):
        raw = str(home) + raw[1:]
    return Path(raw).expanduser().resolve()


def _command(command: str) -> tuple[list[str], dict[str, str]]:
    parts = shlex.split(command)
    environment = {}
    if parts and parts[0] == "env":
        parts.pop(0)
    while parts and "=" in parts[0] and not parts[0].startswith("--"):
        key, value = parts.pop(0).split("=", 1)
        environment[key] = value
    return parts, environment


def _option(parts: list[str], name: str) -> str | None:
    for index, part in enumerate(parts):
        if part.startswith(name + "="):
            return part.split("=", 1)[1]
        if part == name and index + 1 < len(parts):
            return parts[index + 1]
    return None


def diagnose(home: Path | None = None) -> dict:
    home = (home or Path.home()).expanduser().resolve()
    configured = os.environ.get("AI_USAGE_DB_PATH")
    db = _path(configured or home / DEFAULT_DB_RELATIVE_PATH, home)
    # Use the shared resolver for normal HOME, and remap ~ for --home fixtures.
    if home == Path.home().resolve():
        db = resolve_db_path().resolve()
    now = datetime.now(timezone.utc)
    checks = []

    def add(check, status, detail, **values):
        checks.append({"check": check, "status": status, "detail": detail, **values})

    def ok(check, condition, detail, failure="FAIL", **values):
        add(check, "OK" if condition else failure, detail, **values)

    def load(path, check):
        try:
            document = json.loads(path.read_text())
            if not isinstance(document, dict):
                raise ValueError("expected a JSON object")
            add(check, "OK", str(path))
            return document
        except (OSError, ValueError) as exc:
            add(check, "FAIL", f"{path}: {exc}")
            return None

    ok("db.exists", db.is_file(), str(db), path=str(db), exists=db.is_file())
    if db.is_file():
        connection = None
        try:
            add("db.size", "OK", f"{db.stat().st_size} bytes", size_bytes=db.stat().st_size)
            # immutable prevents SQLite from creating or changing WAL/SHM files.
            # Live WAL data is explicitly reported as unavailable, never silently omitted.
            wal = Path(str(db) + "-wal")
            if wal.exists() and wal.stat().st_size > 32:
                add("db.wal", "WARN", "live WAL present; checks describe the checkpointed snapshot")
            connection = sqlite3.connect(
                f"file:{quote(str(db), safe='/')}?mode=ro&immutable=1", uri=True
            )
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            ok("db.user_version", version == SCHEMA_VERSION, str(version), "WARN", user_version=version)
            integrity = [row[0] for row in connection.execute("PRAGMA quick_check")]
            ok("db.quick_check", integrity == ["ok"], "; ".join(integrity), integrity_check=integrity)
            providers = {key: {"sessions": 0, "events": 0, "last_write": None}
                         for key in ("codex", "claude-code", "antigravity")}
            for table, key in (("sessions", "sessions"), ("token_events", "events")):
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not columns:
                    raise ValueError(f"missing table: {table}")
                provider = "COALESCE(NULLIF(provider, ''), 'antigravity')" if "provider" in columns else "'antigravity'"
                last = "MAX(updated_at)" if "updated_at" in columns else "NULL"
                for name, count, updated in connection.execute(
                    f"SELECT {provider}, COUNT(*), {last} FROM {table} GROUP BY 1"
                ):
                    row = providers.setdefault(name, {"sessions": 0, "events": 0, "last_write": None})
                    row[key] = count
                    if updated and (not row["last_write"] or updated > row["last_write"]):
                        row["last_write"] = updated
            for name, row in sorted(providers.items()):
                status = "OK"
                note = ""
                try:
                    written = datetime.fromisoformat(row["last_write"].replace("Z", "+00:00"))
                    written = written.replace(tzinfo=written.tzinfo or timezone.utc)
                    age = (now - written).total_seconds()
                    if age > STALE_SECONDS:
                        status, note = "WARN", " STALE"
                except (AttributeError, TypeError, ValueError):
                    status, note = "WARN", " no valid last write time"
                add(f"provider.{name}", status,
                    f"sessions={row['sessions']} events={row['events']} last_write={row['last_write']}{note}", **row)
        except (OSError, sqlite3.Error, ValueError) as exc:
            add("db.read", "FAIL", str(exc))
        finally:
            if connection is not None:
                connection.close()

    for name, installer, config in (
        ("codex", codex_installer, home / ".codex/hooks.json"),
        ("claude", claude_installer, home / ".claude/settings.json"),
    ):
        document = load(config, f"{name}.config")
        try:
            wanted_digest = expected_digest(installer)
        except OSError as exc:
            add(f"{name}.repo_digest", "FAIL", str(exc))
            wanted_digest = None
        hooks = document.get("hooks", {}) if document else {}
        if not isinstance(hooks, dict):
            add(f"{name}.hooks", "FAIL", "hooks must be an object")
            hooks = {}
        for event in installer.HOOK_EVENTS:
            handlers = []
            groups = hooks.get(event, [])
            if isinstance(groups, list):
                for group in groups:
                    if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                        for handler in group["hooks"]:
                            if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
                                continue
                            try:
                                parts, environment = _command(handler["command"])
                            except ValueError:
                                continue
                            if any(Path(part).name == installer.PUBLISHER_RELATIVE_PATH.name for part in parts):
                                handlers.append((handler, parts, environment))
            prefix = f"{name}.{event}"
            ok(prefix + ".present", bool(handlers), f"{len(handlers)} usage hooks")
            for index, (handler, parts, environment) in enumerate(handlers):
                label = prefix if len(handlers) == 1 else f"{prefix}.{index}"
                ok(label + ".type", handler.get("type") == "command",
                   f"type={handler.get('type')}")
                pinned = _option(parts, "--db")
                ok(label + ".db", bool(pinned) and _path(pinned, home) == db,
                   f"pinned={pinned}; resolved={db}")
                # Codex kills async hook children; Claude Code runs them to completion.
                is_async = handler.get("async", False)
                ok(label + ".sync", name != "codex" or is_async is False, f"async={is_async}")
                interpreter = parts[0] if parts else ""
                located = shutil.which(interpreter) if "/" not in interpreter else interpreter
                executable = _path(located, home) if located else None
                ok(label + ".interpreter", bool(executable and executable.is_file() and os.access(executable, os.X_OK)), interpreter)
                script = next(_path(part, home) for part in parts
                              if Path(part).name == installer.PUBLISHER_RELATIVE_PATH.name)
                release = script.parent.parent
                try:
                    digest = codex_installer._artifact_digest(release) if script.is_file() else None
                    ok(label + ".release", digest is not None and digest == release.name,
                       f"deployed={digest}; release={release}")
                    ok(label + ".digest", digest is not None and digest == wanted_digest,
                       f"deployed={digest}; repo={wanted_digest}", "WARN" if digest else "FAIL")
                    pinned_digest = environment.get("CODEX_USAGE_PUBLISHER_SHA256")
                    if name == "codex":
                        ok(label + ".pinned_digest", pinned_digest == digest and digest is not None,
                           f"pinned={pinned_digest}")
                except OSError as exc:
                    add(label + ".digest", "FAIL", str(exc))

    agy = load(home / ".gemini/config/hooks.json", "agy.config")
    entries = []
    malformed = []

    def walk(value, location="hooks"):
        if isinstance(value, list):
            for index, entry in enumerate(value):
                if not isinstance(entry, dict):
                    malformed.append(f"{location}[{index}]")
                else:
                    walk(entry, f"{location}[{index}]")
        elif isinstance(value, dict):
            if "type" in value or "command" in value or (not value and "[" in location):
                command = value.get("command")
                if not isinstance(command, str) or not command.strip():
                    malformed.append(location)
                else:
                    entries.append((location, command))
            else:
                for key, item in value.items():
                    if isinstance(item, (list, dict)):
                        walk(item, f"{location}.{key}")
                    elif "[" in location:
                        malformed.append(location)

    if agy is not None:
        walk(agy)
    ok("agy.entries", not malformed, "malformed entries: " + (", ".join(malformed) or "none"))
    writers = []
    for location, command in entries:
        try:
            parts, environment = _command(command)
            if any(Path(part).name in ("track_usage.py", "agy_usage_writer.py") for part in parts) or "usage-writer" in location:
                writers.append(command)
                destination = _option(parts, "--db") or environment.get("AI_USAGE_DB_PATH") or configured or home / DEFAULT_DB_RELATIVE_PATH
                ok(f"agy.db.{len(writers)}", _path(destination, home) == db, f"destination={destination}; resolved={db}")
        except ValueError as exc:
            add("agy.command", "FAIL", f"{location}: {exc}")
    ok("agy.writer", bool(writers), f"{len(writers)} usage-writer entries")

    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    ok("retention.plist", plist.is_file(), str(plist), "WARN")
    try:
        result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                                capture_output=True, text=True, timeout=10, check=False)
        ok("retention.loaded", result.returncode == 0,
           "loaded" if result.returncode == 0 else "not loaded", "WARN")
    except (OSError, subprocess.SubprocessError) as exc:
        add("retention.loaded", "WARN", f"not loaded: {exc}")
    log = home / ".codex/usage-retention/retention.log"
    try:
        last_line = ""
        with log.open(errors="replace") as stream:
            for line in stream:
                last_line = line.rstrip("\n")
        ok("retention.log", bool(last_line), last_line or "empty log", "WARN", last_line=last_line)
    except OSError as exc:
        add("retention.log", "WARN", str(exc))
    try:
        backups = db.parent / "backups"
        files = [path for path in backups.iterdir() if path.is_file()] if backups.is_dir() else []
        if files:
            newest = max(files, key=lambda path: path.stat().st_mtime)
            age = (now.timestamp() - newest.stat().st_mtime)
            ok("backups.newest", age <= STALE_SECONDS,
               f"{newest}; age={age / 3600:.1f}h", "WARN", path=str(newest), age_seconds=age)
        else:
            add("backups.newest", "WARN", f"no backups in {backups}")
    except OSError as exc:
        add("backups.newest", "WARN", str(exc))
    statuses = {check["status"] for check in checks}
    code = 1 if "FAIL" in statuses else 2 if "WARN" in statuses else 0
    return {"db_path": str(db), "status": "FAIL" if code == 1 else "WARN" if code == 2 else "OK",
            "exit_code": code, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print structured JSON")
    parser.add_argument("--home", type=Path, help="inspect configs and defaults under this HOME")
    args = parser.parse_args(argv)
    report = diagnose(args.home)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        rows = [["CHECK", "STATUS", "DETAIL"]] + [[row["check"], row["status"], row["detail"]] for row in report["checks"]]
        widths = [max(len(row[i]) for row in rows) for i in range(2)]
        for row in rows:
            print(f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]}")
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
