#!/usr/bin/env python3
"""Deploy a frozen Antigravity usage writer and merge agy's flat hooks.json.

The timestamped backup contains the replaced agy-token-tracker entry. Uninstall
removes only this publisher; backups and immutable releases remain available.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_claude_usage_hooks as _claude  # noqa: E402
import install_codex_usage_hooks as _shared  # noqa: E402

InstallError = _shared.InstallError
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_RELATIVE_PATH = Path("scripts/agy_usage_writer.py")
DEFAULT_GEMINI_HOME = Path.home() / ".gemini"
ENTRY_NAME = "agy-token-tracker"
HOOK_EVENTS = ("PostInvocation", "Stop")


@dataclass(frozen=True)
class InstallResult:
    install_directory: Path
    artifact_directory: Path | None
    publisher_script: Path | None
    hooks_file: Path
    backup_file: Path | None
    changed: bool
    hooks: dict[str, Any]


def _validate_hooks(document: dict[str, Any]) -> None:
    """A malformed hook anywhere disables all agy hooks: reject the whole file."""
    for name, entry in document.items():
        if not isinstance(entry, dict):
            raise InstallError(f"Hook entry {name!r} must be an object")
        for event, handlers in entry.items():
            if event == "enabled":
                if not isinstance(handlers, bool):
                    raise InstallError(f"Hook entry {name!r} enabled must be boolean")
                continue
            if not isinstance(handlers, list):
                raise InstallError(f"Hook entry {name!r}/{event} must be a list")
            for handler in handlers:
                if (
                    not isinstance(handler, dict)
                    or handler.get("type") != "command"
                    or not isinstance(handler.get("command"), str)
                    or not handler["command"].strip()
                    or "hooks" in handler
                ):
                    raise InstallError(f"Malformed flat command hook in {name!r}/{event}")


def _owns(handler: Any, install_directory: Path) -> bool:
    if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
        return False
    try:
        parts = shlex.split(handler["command"])
    except ValueError:
        return False
    for part in parts:
        path = Path(part)
        if path.name == PUBLISHER_RELATIVE_PATH.name and path.is_relative_to(install_directory):
            return path.parts[-2:] == PUBLISHER_RELATIVE_PATH.parts
    return False


def _merge_hooks(existing, command, install_directory):
    merged = json.loads(json.dumps(existing))
    if command is not None:
        # The entire legacy tracker entry is replaced, and retained in the
        # timestamped whole-file backup. Other named entries stay unchanged.
        merged[ENTRY_NAME] = {
            "enabled": True,
            **{event: [{"type": "command", "command": command}] for event in HOOK_EVENTS},
        }
    else:
        entry = merged.get(ENTRY_NAME)
        if isinstance(entry, dict):
            removed = False
            for event, handlers in list(entry.items()):
                if isinstance(handlers, list):
                    kept = [h for h in handlers if not _owns(h, install_directory)]
                    removed |= kept != handlers
                    if kept:
                        entry[event] = kept
                    elif kept != handlers:
                        entry.pop(event)
            if removed and set(entry) <= {"enabled"}:
                merged.pop(ENTRY_NAME)
    _validate_hooks(merged)
    return merged


def _copy_payload(source: Path, staging: Path) -> None:
    entrypoint = source / PUBLISHER_RELATIVE_PATH
    package = source / "src"
    if entrypoint.is_symlink() or not entrypoint.is_file() or package.is_symlink() or not package.is_dir():
        raise InstallError("Publisher script or src dependencies are unavailable")
    files = [(entrypoint, PUBLISHER_RELATIVE_PATH)]
    files += [
        (item, item.relative_to(source)) for item in sorted(package.rglob("*"))
        if not item.is_symlink() and item.is_file()
        and not _shared._is_denied_source_path(item.relative_to(source))
    ]
    for original, relative in files:
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)


def _validate_publisher(interpreter, staging):
    # --help alone is insufficient: writer imports are intentionally lazy to
    # prevent a missing dependency from failing the host's hook invocation.
    checks = [
        [str(interpreter), str(staging / PUBLISHER_RELATIVE_PATH), "--help"],
        [str(interpreter), "-c", "import src.usage_store; import src.pricing; import src.parsers.contracts"],
    ]
    for command in checks:
        try:
            result = subprocess.run(
                command, cwd=staging, capture_output=True, text=True, timeout=20,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError("Could not validate staged Antigravity writer") from exc
        if result.returncode:
            raise InstallError(f"Staged publisher validation failed: {result.stderr.strip()}")


def install_agy_usage_hooks(
    *, source_root=REPOSITORY_ROOT, gemini_home=DEFAULT_GEMINI_HOME,
    database_path=None, python_interpreter=sys.executable, dry_run=False,
) -> InstallResult:
    source = Path(source_root).expanduser().resolve(strict=True)
    home = Path(gemini_home).expanduser().resolve()
    hooks_file = home / "config/hooks.json"
    existing, original = _claude._load_settings(hooks_file)
    install_directory = home / "usage-publisher"
    releases = install_directory / "releases"
    if install_directory.is_symlink() or (install_directory.exists() and not install_directory.is_dir()):
        raise InstallError("Publisher directory is not a regular directory")
    interpreter = _shared._validate_python_interpreter(python_interpreter)
    database = Path(database_path or _shared._default_database_path()).expanduser().resolve()
    # Validate before deployment, allowing a malformed legacy tracker entry
    # to be repaired by replacing it with our validated flat entry.
    _merge_hooks(existing, "staging", install_directory)
    staging = (
        Path(tempfile.mkdtemp(prefix="agy-usage-dry-run-")) / "stage" if dry_run
        else home / f".usage-publisher.stage-{uuid.uuid4().hex}"
    )
    release = None
    created = False
    try:
        staging.mkdir(parents=True)
        _copy_payload(source, staging)
        _validate_publisher(interpreter, staging)
        digest = _shared._artifact_digest(staging)
        release = releases / digest
        if releases.is_symlink() or release.is_symlink():
            raise InstallError("Refusing symlinked publisher release")
        script = release / PUBLISHER_RELATIVE_PATH
        command = shlex.join([
            "PYTHONDONTWRITEBYTECODE=1", str(interpreter), str(script),
            "--db", str(database), "--agy-dir", str(home / "antigravity-cli"),
        ])
        merged = _merge_hooks(existing, command, install_directory)
        changed = merged != existing or original is None
        artifact_changed = not release.exists()
        if release.exists() and (not release.is_dir() or _shared._artifact_digest(release) != digest):
            raise InstallError("Existing immutable publisher release is corrupt")
        if dry_run:
            return InstallResult(install_directory, release, script, hooks_file, None, changed or artifact_changed, merged)
        if artifact_changed:
            releases.mkdir(parents=True, exist_ok=True)
            os.replace(staging, release)
            created = True
        backup = None
        if changed:
            hooks_file.parent.mkdir(parents=True, exist_ok=True)
            backup = _claude._write_settings_atomic(hooks_file, merged)
        return InstallResult(install_directory, release, script, hooks_file, backup, changed or artifact_changed, merged)
    except Exception:
        if created and release is not None:
            shutil.rmtree(release, ignore_errors=True)
            _shared._remove_empty_directory(releases)
            _shared._remove_empty_directory(install_directory)
        raise
    finally:
        shutil.rmtree(staging.parent if dry_run else staging, ignore_errors=True)


def uninstall_agy_usage_hooks(*, gemini_home=DEFAULT_GEMINI_HOME, dry_run=False):
    home = Path(gemini_home).expanduser().resolve()
    hooks_file = home / "config/hooks.json"
    existing, original = _claude._load_settings(hooks_file)
    install_directory = home / "usage-publisher"
    merged = _merge_hooks(existing, None, install_directory)
    changed = original is not None and merged != existing
    backup = _claude._write_settings_atomic(hooks_file, merged) if changed and not dry_run else None
    return InstallResult(install_directory, None, None, hooks_file, backup, changed, merged)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--gemini-home", type=Path, default=DEFAULT_GEMINI_HOME)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.uninstall:
            result = uninstall_agy_usage_hooks(gemini_home=args.gemini_home, dry_run=args.dry_run)
        else:
            result = install_agy_usage_hooks(
                source_root=args.repo_root, gemini_home=args.gemini_home, database_path=args.db,
                python_interpreter=args.python, dry_run=args.dry_run,
            )
        if args.dry_run:
            print(json.dumps(result.hooks, indent=2))
        else:
            print(f"Antigravity usage hooks {'updated' if result.changed else 'unchanged'}: {result.hooks_file}")
            if result.backup_file:
                print(f"Backup: {result.backup_file}")
        return 0
    except (InstallError, OSError) as exc:
        print(f"Antigravity hook install failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
