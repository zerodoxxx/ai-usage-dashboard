#!/usr/bin/env python3
"""Install a frozen 15-day Codex transcript-retention LaunchAgent.

The installer writes one user LaunchAgent and an immutable, content-addressed
runtime under ``~/.codex/usage-retention``. It does not delete transcripts or
load/unload launchd jobs; the caller can review the written plist before
bootstrapping it with ``launchctl``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any
import uuid

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.usage_store import LEGACY_DB_RELATIVE_PATH, resolve_db_path

RETENTION_RELATIVE_PATH = Path("scripts/codex_retention.py")
BACKUP_RELATIVE_PATH = Path("scripts/backup_usage_db.py")
DEFAULT_CODEX_HOME = Path.home() / ".codex"
LAUNCH_AGENT_LABEL = "com.zerodoxxx.ai-usage-dashboard.codex-retention"
LAUNCH_AGENT_FILENAME = f"{LAUNCH_AGENT_LABEL}.plist"
RETENTION_DAYS = 15
RUN_INTERVAL_SECONDS = 900
SAFE_CODEX_VERSION = "0.159.2"

_ALLOWED_SOURCE_SUFFIXES = {".py", ".json", ".csv", ".yaml", ".yml", ".txt"}
_DENIED_DIRECTORY_NAMES = {
    ".git", ".pytest_cache", ".ruff_cache", ".venv", "venv", "__pycache__",
    "test", "tests", "private", "secrets", "credentials", "config", "configs",
    "configuration", "settings", "static", "templates",
}
_DENIED_NAME_PARTS = ("credential", "secret", "private", "password")


@dataclass(frozen=True)
class InstallResult:
    codex_home: Path
    artifact_directory: Path
    retention_script: Path
    launch_agent_path: Path
    backup_path: Path | None
    changed: bool


class InstallError(RuntimeError):
    """A retention job could not be installed without replacing user data."""


def _database_path(configured: str | Path | None) -> Path:
    resolved = Path(resolve_db_path(configured)).expanduser().resolve(strict=False)
    legacy = (Path.home() / LEGACY_DB_RELATIVE_PATH).resolve(strict=False)
    if resolved == legacy:
        raise InstallError("The frozen legacy usage database cannot be used for retention")
    try:
        if resolved.exists() and legacy.exists() and os.path.samefile(resolved, legacy):
            raise InstallError("The frozen legacy usage database cannot be used for retention")
    except OSError:
        pass
    return resolved


def _validate_python(interpreter: str | Path) -> Path:
    candidate = Path(interpreter).expanduser()
    if not candidate.is_absolute():
        located = shutil.which(str(candidate))
        candidate = Path(located) if located else candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise InstallError("The Python interpreter is unavailable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise InstallError("The Python interpreter is not executable")
    try:
        result = subprocess.run(
            [str(resolved), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        version = tuple(int(item) for item in result.stdout.strip().split("."))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise InstallError("Could not validate the Python interpreter") from exc
    if version < (3, 12):
        raise InstallError("Python 3.12 or newer is required for the retention job")
    return resolved


def _probe_codex_version(
    codex_executable: str | Path | None,
    *,
    run: Any = subprocess.run,
) -> tuple[Path, str]:
    found = str(codex_executable) if codex_executable is not None else shutil.which("codex")
    if not found:
        raise InstallError("Codex CLI is unavailable")
    executable = Path(found).expanduser()
    if not executable.exists():
        raise InstallError("Codex CLI executable is unavailable")
    try:
        result = run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("Could not verify the Codex CLI version") from exc
    import re

    versions = re.findall(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0 or versions != [SAFE_CODEX_VERSION]:
        raise InstallError(
            f"This retention policy was reviewed for Codex {SAFE_CODEX_VERSION}; "
            "the installed CLI must match exactly"
        )
    try:
        return executable.resolve(strict=True), SAFE_CODEX_VERSION
    except OSError as exc:
        raise InstallError("Codex CLI executable path is unavailable") from exc


def _denied(relative: Path) -> bool:
    parts = [part.casefold() for part in relative.parts]
    filename = parts[-1]
    if any(part in _DENIED_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if any(denied in part for part in parts for denied in _DENIED_NAME_PARTS):
        return True
    if any(part.startswith(".") for part in parts):
        return True
    if filename.startswith("test_") or filename.endswith("_test.py"):
        return True
    if filename in {"install_codex_retention.py", "install_codex_usage_hooks.py"}:
        return True
    return Path(filename).suffix.lower() not in _ALLOWED_SOURCE_SUFFIXES


def _copy_runtime(source_root: Path, staging: Path) -> None:
    entrypoint = source_root / RETENTION_RELATIVE_PATH
    source_package = source_root / "src"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise InstallError("Retention script is missing")
    if source_package.is_symlink() or not source_package.is_dir():
        raise InstallError("Retention runtime package is missing")

    sources = [(entrypoint, RETENTION_RELATIVE_PATH)]
    backup_module = source_root / BACKUP_RELATIVE_PATH
    if backup_module.is_symlink() or not backup_module.is_file():
        raise InstallError("Backup module is missing")
    sources.append((backup_module, BACKUP_RELATIVE_PATH))
    for item in sorted(source_package.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        relative = item.relative_to(source_root)
        if not _denied(relative):
            sources.append((item, relative))
    for source, relative in sources:
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if not (staging / RETENTION_RELATIVE_PATH).is_file():
        raise InstallError("Retention script was not staged")


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


def _make_release_read_only(root: Path) -> None:
    for item in root.rglob("*"):
        if item.is_dir():
            item.chmod(0o555)
        elif item.is_file():
            item.chmod(0o444)
    root.chmod(0o555)


def _validate_runtime(interpreter: Path, staging: Path) -> None:
    validation_environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run(
            [str(interpreter), str(staging / RETENTION_RELATIVE_PATH), "--help"],
            cwd=staging,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            env=validation_environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("Could not validate the staged retention command") from exc
    if result.returncode != 0:
        raise InstallError("The staged retention command failed validation")


def _load_launch_agent(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return {}, None
    if path.is_symlink() or not path.is_file():
        raise InstallError("Refusing to replace a non-regular LaunchAgent file")
    raw = path.read_bytes()
    try:
        document = plistlib.loads(raw)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise InstallError("Existing retention LaunchAgent is not a valid plist") from exc
    if not isinstance(document, dict):
        raise InstallError("Existing retention LaunchAgent must be a plist dictionary")
    if document.get("Label") != LAUNCH_AGENT_LABEL:
        raise InstallError("The retention LaunchAgent path belongs to another job")
    arguments = document.get("ProgramArguments")
    if not isinstance(arguments, list) or not any(
        Path(str(argument)).name == RETENTION_RELATIVE_PATH.name
        for argument in arguments
    ):
        raise InstallError("Refusing to replace an unrelated LaunchAgent")
    return document, raw


def _write_plist_atomic(path: Path, document: dict[str, Any]) -> Path | None:
    temporary: Path | None = None
    backup: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            plistlib.dump(document, stream, fmt=plistlib.FMT_XML, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        os.chmod(temporary, mode)
        if path.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = path.with_name(f"{path.name}.{timestamp}.bak")
            shutil.copy2(path, backup)
        os.replace(temporary, path)
        temporary = None
        return backup
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_codex_retention(
    *,
    source_root: str | Path = REPOSITORY_ROOT,
    codex_home: str | Path = DEFAULT_CODEX_HOME,
    launch_agents_dir: str | Path | None = None,
    database_path: str | Path | None = None,
    python_interpreter: str | Path = sys.executable,
    codex_executable: str | Path | None = None,
    run: Any = subprocess.run,
) -> InstallResult:
    """Write one version-pinned user LaunchAgent and immutable runtime."""
    source = Path(source_root).expanduser().resolve(strict=True)
    raw_home = Path(codex_home).expanduser()
    if raw_home.is_symlink() or (raw_home.exists() and not raw_home.is_dir()):
        raise InstallError("Codex home is not a safe directory")
    raw_home.mkdir(parents=True, exist_ok=True)
    home = raw_home.resolve(strict=True)
    interpreter = _validate_python(python_interpreter)
    codex_binary, codex_version = _probe_codex_version(codex_executable, run=run)
    database = _database_path(database_path)
    agents_dir = (
        Path(launch_agents_dir).expanduser().absolute()
        if launch_agents_dir is not None
        else Path.home() / "Library" / "LaunchAgents"
    )
    if agents_dir.is_symlink():
        raise InstallError("Refusing to write through a symlinked LaunchAgents directory")
    agent_path = agents_dir / LAUNCH_AGENT_FILENAME

    existing_agent, original_bytes = _load_launch_agent(agent_path)
    retention_root = home / "usage-retention"
    if retention_root.is_symlink() or (retention_root.exists() and not retention_root.is_dir()):
        raise InstallError("Refusing to use an unsafe retention directory")
    retention_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    releases = retention_root / "releases"
    if releases.is_symlink():
        raise InstallError("Refusing to use a symlinked release directory")

    staging = retention_root / f".stage-{uuid.uuid4().hex}"
    release: Path | None = None
    created_release = False
    backup: Path | None = None
    try:
        staging.mkdir(mode=0o700)
        _copy_runtime(source, staging)
        _validate_runtime(interpreter, staging)
        digest = _artifact_digest(staging)
        release = releases / digest
        if release.is_symlink():
            raise InstallError("Refusing to use a symlinked retention release")
        if release.exists():
            if not release.is_dir() or _artifact_digest(release) != digest:
                raise InstallError("Existing immutable retention release failed verification")
            shutil.rmtree(staging)
        else:
            releases.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(staging, release)
            created_release = True
            _make_release_read_only(release)

        installed_script = release / RETENTION_RELATIVE_PATH
        log_path = retention_root / "retention.log"
        arguments = [
            str(interpreter),
            str(installed_script),
            "--apply",
            "--days",
            str(RETENTION_DAYS),
            "--codex-dir",
            str(home),
            "--db",
            str(database),
            "--codex-version",
            codex_version,
            "--codex-cli",
            str(codex_binary),
            "--json",
            "--log-file",
            str(log_path),
        ]
        desired = dict(existing_agent)
        desired.update({
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(release),
            "StartInterval": RUN_INTERVAL_SECONDS,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": os.devnull,
            "StandardErrorPath": os.devnull,
        })
        encoded = plistlib.dumps(desired, fmt=plistlib.FMT_XML, sort_keys=True)
        if original_bytes == encoded:
            agent_changed = False
        else:
            agents_dir.mkdir(parents=True, exist_ok=True)
            backup = _write_plist_atomic(agent_path, desired)
            agent_changed = True
        return InstallResult(
            codex_home=home,
            artifact_directory=release,
            retention_script=installed_script,
            launch_agent_path=agent_path,
            backup_path=backup,
            changed=created_release or agent_changed,
        )
    except Exception:
        if created_release and release is not None:
            shutil.rmtree(release, ignore_errors=True)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--codex-home", type=Path, default=DEFAULT_CODEX_HOME)
    parser.add_argument("--launch-agents-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--codex", type=Path, help="Codex CLI executable, for testing or nonstandard installs.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if sys.platform != "darwin" and args.launch_agents_dir is None:
        print("Codex retention LaunchAgent is available only on macOS.", file=sys.stderr)
        return 2
    try:
        result = install_codex_retention(
            source_root=args.repo_root,
            codex_home=args.codex_home,
            launch_agents_dir=args.launch_agents_dir,
            database_path=args.db,
            python_interpreter=args.python,
            codex_executable=args.codex,
        )
    except (InstallError, OSError) as exc:
        print(f"Codex retention installation failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(f"Installed retention runtime: {result.artifact_directory}")
    print(f"Wrote LaunchAgent: {result.launch_agent_path}")
    if result.backup_path is not None:
        print(f"Previous LaunchAgent backed up to: {result.backup_path}")
    elif not result.changed:
        print("The Codex retention job is already up to date.")
    print("Review the plist, then load it with launchctl bootstrap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
