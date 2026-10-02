#!/usr/bin/env python3
"""Deploy the dashboard's Codex usage publisher and install user-level hooks.

The installed payload is a frozen copy of the tested publisher and its source
dependencies. Hook commands point only into ``~/.codex/usage-publisher`` so a
git branch switch cannot remove the script while Codex is using it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any
import uuid


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_RELATIVE_PATH = Path("scripts/codex_usage_writer.py")
DEFAULT_CODEX_HOME = Path.home() / ".codex"
HOOK_EVENTS = ("Stop", "SubagentStop", "Interrupt")
HOOK_TIMEOUT_SECONDS = 30
INTERRUPT_TIMEOUT_SECONDS = 3

_ALLOWED_SOURCE_SUFFIXES = {".py", ".json", ".csv", ".yaml", ".yml", ".txt"}
_DENIED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".sqlite-wal",
    ".sqlite-shm",
    ".log",
    ".jsonl",
}
_DENIED_DIRECTORY_NAMES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "__pycache__",
    "test",
    "tests",
    "private",
    "secrets",
    "credentials",
    "config",
    "configs",
    "configuration",
    "settings",
    "static",
    "templates",
}
_DENIED_NAME_PARTS = ("credential", "secret", "private", "password")


@dataclass(frozen=True)
class InstallResult:
    """Details of a completed Codex hook deployment."""

    install_directory: Path
    artifact_directory: Path
    publisher_script: Path
    hooks_file: Path
    backup_file: Path | None
    changed: bool


class InstallError(RuntimeError):
    """Raised when the publisher cannot be installed safely."""


def _default_database_path() -> Path:
    configured = os.environ.get("AI_USAGE_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    # Must match src.usage_store.DEFAULT_DB_RELATIVE_PATH.
    return Path.home() / ".local/share/ai-usage/usage.db"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _validate_python_interpreter(interpreter: str | Path) -> Path:
    candidate = Path(interpreter).expanduser()
    if not candidate.is_absolute():
        located = shutil.which(str(candidate))
        candidate = Path(located) if located else candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise InstallError(f"Python interpreter does not exist: {candidate}") from exc

    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise InstallError(f"Python interpreter is not executable: {resolved}")

    try:
        probe = subprocess.run(
            [str(candidate), "-c", "import sqlite3, sys; print(sys.version_info[:2])"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"Could not validate Python interpreter: {resolved}") from exc

    try:
        version = tuple(int(part) for part in probe.stdout.strip().strip("()").split(", "))
    except ValueError as exc:
        raise InstallError(f"Python interpreter returned an invalid version: {resolved}") from exc
    if version < (3, 12):
        raise InstallError(
            f"Python 3.12 or newer is required for the publisher: {resolved}"
        )
    return candidate


def _is_denied_source_path(relative_path: Path) -> bool:
    parts = [part.lower() for part in relative_path.parts]
    filename = parts[-1]
    suffix = Path(filename).suffix.lower()

    if any(part in _DENIED_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if any(denied in part for part in parts for denied in _DENIED_NAME_PARTS):
        return True
    if any(part.startswith(".") for part in parts):
        return True
    if any(denied in filename for denied in _DENIED_NAME_PARTS):
        return True
    if filename.startswith("test_") or filename.endswith("_test.py"):
        return True
    if suffix in _DENIED_SUFFIXES:
        return True
    if filename == "install_codex_usage_hooks.py":
        return True
    if "config" in Path(filename).stem or "settings" in Path(filename).stem:
        return True
    if filename.startswith((".env", "credentials.", "secrets.")):
        return True
    return suffix not in _ALLOWED_SOURCE_SUFFIXES


def _copy_payload(source_root: Path, staging_directory: Path) -> None:
    """Copy the exact publisher entry point and safe runtime source tree."""
    entrypoint = source_root / PUBLISHER_RELATIVE_PATH
    source_package = source_root / "src"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise InstallError(f"Publisher script is missing: {entrypoint}")
    if source_package.is_symlink() or not source_package.is_dir():
        raise InstallError(f"Publisher dependencies are missing: {source_package}")

    files: list[tuple[Path, Path]] = [(entrypoint, PUBLISHER_RELATIVE_PATH)]
    for item in sorted(source_package.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        relative = item.relative_to(source_root)
        if _is_denied_source_path(relative):
            continue
        files.append((item, relative))

    copied_entrypoint = False
    for source, relative in files:
        target = staging_directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if relative == PUBLISHER_RELATIVE_PATH:
            copied_entrypoint = True
    if not copied_entrypoint:
        raise InstallError("Publisher script was not copied into the staged package")


def _artifact_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with item.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _validate_staged_publisher(interpreter: Path, staging_directory: Path) -> None:
    script = staging_directory / PUBLISHER_RELATIVE_PATH
    validation_environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run(
            [str(interpreter), str(script), "--help"],
            cwd=staging_directory,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=validation_environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("Could not validate the staged publisher") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or result.stdout.strip().splitlines()[-1:]
        message = detail[0] if detail else f"exit code {result.returncode}"
        raise InstallError(f"Staged publisher validation failed: {message}")

    try:
        imports = subprocess.run(
            [
                str(interpreter),
                "-c",
                "import src.usage_store; import src.parsers.codex",
            ],
            cwd=staging_directory,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=validation_environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("Could not validate the staged publisher dependencies") from exc
    if imports.returncode != 0:
        detail = imports.stderr.strip().splitlines()[-1:] or imports.stdout.strip().splitlines()[-1:]
        message = detail[0] if detail else f"exit code {imports.returncode}"
        raise InstallError(f"Staged publisher dependency validation failed: {message}")


def _hook_command(
    interpreter: Path, script: Path, database: Path, publisher_digest: str
) -> str:
    parts = [
        "PYTHONDONTWRITEBYTECODE=1",
        f"CODEX_USAGE_PUBLISHER_SHA256={publisher_digest}",
        str(interpreter),
        str(script),
        "--db",
        str(database),
    ]
    return shlex.join(parts)


def _handler_for(command: str, event: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": command,
        "timeout": (
            INTERRUPT_TIMEOUT_SECONDS if event == "Interrupt" else HOOK_TIMEOUT_SECONDS
        ),
        "async": True,
    }


def _is_publisher_handler(handler: Any, install_directory: Path) -> bool:
    if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
        return False
    try:
        parts = shlex.split(handler["command"])
    except ValueError:
        return False
    for part in parts:
        candidate = Path(part)
        if candidate.name != PUBLISHER_RELATIVE_PATH.name:
            continue
        try:
            relative = candidate.relative_to(install_directory)
        except ValueError:
            continue
        if relative.parts[-2:] == PUBLISHER_RELATIVE_PATH.parts:
            return True
    return False


def _upsert_publisher_hook(
    config: dict[str, Any], event: str, handler: dict[str, Any], install_directory: Path
) -> bool:
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("Existing hooks.json has a non-object 'hooks' value")

    groups = hooks.get(event, [])
    if not isinstance(groups, list):
        raise InstallError(f"Existing hooks.json has a non-list {event!r} value")

    own_handlers = [
        candidate
        for group in groups
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for candidate in group["hooks"]
        if _is_publisher_handler(candidate, install_directory)
    ]
    if len(own_handlers) == 1 and own_handlers[0] == handler:
        return False

    updated_groups: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            updated_groups.append(group)
            continue

        kept_handlers = [
            candidate
            for candidate in group["hooks"]
            if not _is_publisher_handler(candidate, install_directory)
        ]
        if kept_handlers == group["hooks"]:
            updated_groups.append(group)
            continue
        updated_group = dict(group)
        updated_group["hooks"] = kept_handlers
        if not kept_handlers and set(updated_group) == {"hooks"}:
            continue
        # Keep an otherwise empty custom group intact so its unrelated matcher
        # or metadata is preserved exactly.
        updated_groups.append(updated_group)

    updated_groups.append({"hooks": [handler]})
    hooks[event] = updated_groups
    return True


def _merge_hooks(
    existing: dict[str, Any],
    command: str,
    install_directory: Path,
) -> tuple[dict[str, Any], bool]:
    merged = json.loads(json.dumps(existing))
    if "hooks" not in merged:
        merged["hooks"] = {}
    if not isinstance(merged["hooks"], dict):
        raise InstallError("Existing hooks.json has a non-object 'hooks' value")

    changed = False
    for event in HOOK_EVENTS:
        changed = _upsert_publisher_hook(
            merged,
            event,
            _handler_for(command, event),
            install_directory,
        ) or changed
    return merged, changed


def _load_hooks_file(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return {}, None
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"Refusing to replace non-regular hooks file: {path}")
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"Existing hooks file is not valid JSON: {path}") from exc
    if not isinstance(document, dict):
        raise InstallError(f"Existing hooks file must contain a JSON object: {path}")
    return document, raw


def _write_hooks_file_atomic(path: Path, document: dict[str, Any]) -> Path | None:
    encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    backup: Path | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".hooks.json.tmp-", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        os.chmod(temporary, mode)

        if path.exists():
            backup = path.with_name(f"hooks.json.{_timestamp()}.bak")
            shutil.copy2(path, backup)

        os.replace(temporary, path)
        temporary = None
        return backup
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def install_codex_usage_hooks(
    *,
    source_root: str | Path = REPOSITORY_ROOT,
    codex_home: str | Path = DEFAULT_CODEX_HOME,
    database_path: str | Path | None = None,
    python_interpreter: str | Path = sys.executable,
) -> InstallResult:
    """Install the frozen publisher and merge only its user-level hook entries."""
    source = Path(source_root).expanduser().resolve(strict=True)
    home = Path(codex_home).expanduser().resolve()
    database = Path(database_path or _default_database_path()).expanduser().resolve()
    interpreter = _validate_python_interpreter(python_interpreter)

    hooks_file = home / "hooks.json"
    existing_config, original_hooks_bytes = _load_hooks_file(hooks_file)

    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError(f"Could not prepare Codex home: {home}") from exc

    install_directory = home / "usage-publisher"
    if install_directory.is_symlink() or (
        install_directory.exists() and not install_directory.is_dir()
    ):
        raise InstallError(f"Refusing to replace non-directory publisher path: {install_directory}")

    staging_directory = home / f".usage-publisher.stage-{uuid.uuid4().hex}"
    releases_directory = install_directory / "releases"
    release_directory: Path | None = None
    release_created = False
    artifact_changed = False
    backup_file: Path | None = None

    try:
        staging_directory.mkdir()
        _copy_payload(source, staging_directory)
        _validate_staged_publisher(interpreter, staging_directory)

        publisher_digest = _artifact_digest(staging_directory)
        release_directory = releases_directory / publisher_digest
        if install_directory.exists() and releases_directory.is_symlink():
            raise InstallError(f"Refusing to use symlinked release directory: {releases_directory}")
        if release_directory.is_symlink():
            raise InstallError(f"Refusing to use symlinked publisher release: {release_directory}")

        if release_directory.exists():
            if not release_directory.is_dir() or _artifact_digest(release_directory) != publisher_digest:
                raise InstallError(
                    f"Existing immutable publisher release is corrupt: {release_directory}"
                )
            shutil.rmtree(staging_directory)
        else:
            releases_directory.mkdir(parents=True, exist_ok=True)
            os.replace(staging_directory, release_directory)
            release_created = True
            artifact_changed = True

        installed_script = release_directory / PUBLISHER_RELATIVE_PATH
        command = _hook_command(
            interpreter,
            installed_script,
            database,
            publisher_digest,
        )
        merged_config, hooks_changed = _merge_hooks(
            existing_config,
            command,
            install_directory,
        )

        if hooks_changed:
            expected_bytes = (
                json.dumps(merged_config, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            if original_hooks_bytes == expected_bytes:
                hooks_changed = False
            else:
                backup_file = _write_hooks_file_atomic(hooks_file, merged_config)

        return InstallResult(
            install_directory=install_directory,
            artifact_directory=release_directory,
            publisher_script=installed_script,
            hooks_file=hooks_file,
            backup_file=backup_file,
            changed=artifact_changed or hooks_changed,
        )
    except Exception:
        if release_created and release_directory is not None:
            shutil.rmtree(release_directory, ignore_errors=True)
            _remove_empty_directory(releases_directory)
            _remove_empty_directory(install_directory)
        raise
    finally:
        if staging_directory.exists():
            shutil.rmtree(staging_directory, ignore_errors=True)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="dashboard checkout containing scripts/codex_usage_writer.py",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=DEFAULT_CODEX_HOME,
        help="Codex user configuration directory (defaults to ~/.codex)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="AGY token usage SQLite database path (defaults to AI_USAGE_DB_PATH or ~/.local/share/ai-usage/usage.db)",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python 3.12+ interpreter used by Codex lifecycle hooks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    try:
        result = install_codex_usage_hooks(
            source_root=args.repo_root,
            codex_home=args.codex_home,
            database_path=args.db,
            python_interpreter=args.python,
        )
    except (InstallError, OSError) as exc:
        print(f"Codex usage hook install failed: {exc}", file=sys.stderr)
        return 1

    print(f"Installed publisher: {result.install_directory}")
    print(f"Updated hooks: {result.hooks_file}")
    if result.backup_file is not None:
        print(f"Previous hooks backed up to: {result.backup_file}")
    elif not result.changed:
        print("Codex usage hooks are already up to date.")
    print("Review and trust the exact definitions with /hooks in Codex CLI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
