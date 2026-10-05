#!/usr/bin/env python3
"""Deploy the dashboard's Claude Code usage publisher and install user hooks.

The installed payload is a frozen, content-addressed copy of the tested
publisher and its source dependencies under ``~/.claude/usage-publisher``, so a
git branch switch cannot remove the script while Claude Code is using it.
Hooks are merged into ``~/.claude/settings.json`` (Stop, SubagentStop,
SessionEnd). All other settings and hooks are preserved, and a timestamped
backup is written before the file changes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_codex_usage_hooks as _codex  # noqa: E402  (shared, tested helpers)

InstallError = _codex.InstallError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_RELATIVE_PATH = Path("scripts/claude_usage_writer.py")
DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
HOOK_EVENTS = ("Stop", "SubagentStop", "SessionEnd")
HOOK_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class InstallResult:
    """Details of a completed (or planned) Claude hook deployment."""

    install_directory: Path
    artifact_directory: Path | None
    publisher_script: Path | None
    settings_file: Path
    backup_file: Path | None
    changed: bool
    settings: dict[str, Any] | None = None


# ---------------------------------------------------------------- payload ---


def _copy_payload(source_root: Path, staging_directory: Path) -> None:
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
        if _codex._is_denied_source_path(relative):
            continue
        files.append((item, relative))
    for source, relative in files:
        target = staging_directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _validate_staged_publisher(interpreter: Path, staging_directory: Path) -> None:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    checks = (
        [str(interpreter), str(staging_directory / PUBLISHER_RELATIVE_PATH), "--help"],
        [str(interpreter), "-c", "import src.usage_store; import src.parsers.claude"],
    )
    for command in checks:
        try:
            result = subprocess.run(
                command, cwd=staging_directory, check=False, capture_output=True,
                text=True, timeout=20, env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError("Could not validate the staged publisher") from exc
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] or result.stdout.strip().splitlines()[-1:]
            raise InstallError(
                f"Staged publisher validation failed: {detail[0] if detail else result.returncode}"
            )


# ------------------------------------------------------------ settings.json ---


def _hook_command(interpreter: Path, script: Path, database: Path | None) -> str:
    import shlex

    parts = ["PYTHONDONTWRITEBYTECODE=1", str(interpreter), str(script)]
    if database is not None:
        parts += ["--db", str(database)]
    return shlex.join(parts)


def _handler_for(command: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": command,
        "timeout": HOOK_TIMEOUT_SECONDS,
        "async": True,
    }


def _owns(handler: Any, install_directory: Path) -> bool:
    import shlex

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


def _strip_publisher(groups: list[Any], install_directory: Path) -> list[Any]:
    """Return groups without our handlers; drop groups that become empty."""
    result: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            result.append(group)
            continue
        kept = [h for h in group["hooks"] if not _owns(h, install_directory)]
        if kept == group["hooks"]:
            result.append(group)
            continue
        if not kept:
            # Only discard a group that held nothing but our handler.
            if set(group) <= {"hooks", "matcher"} and not group.get("matcher"):
                continue
        updated = dict(group)
        updated["hooks"] = kept
        result.append(updated)
    return result


def _merge_hooks(
    existing: dict[str, Any], command: str | None, install_directory: Path
) -> dict[str, Any]:
    """Install (command given) or remove (None) our hooks, preserving the rest."""
    merged = json.loads(json.dumps(existing))
    hooks = merged.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise InstallError("Existing settings have a non-object 'hooks' value")

    for event in HOOK_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise InstallError(f"Existing settings have a non-list {event!r} hooks value")
        stripped = _strip_publisher(groups, install_directory)
        if command is not None:
            stripped.append({"hooks": [_handler_for(command)]})
        if stripped:
            hooks[event] = stripped
        else:
            hooks.pop(event, None)

    if hooks:
        merged["hooks"] = hooks
    else:
        merged.pop("hooks", None)
    return merged


def _load_settings(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return {}, None
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"Refusing to replace non-regular settings file: {path}")
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"Existing settings file is not valid JSON: {path}") from exc
    if not isinstance(document, dict):
        raise InstallError(f"Existing settings file must contain a JSON object: {path}")
    return document, raw


def _encode(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_settings_atomic(path: Path, document: dict[str, Any]) -> Path | None:
    """Back up the current file (timestamped), then atomically replace it."""
    backup: Path | None = None
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".settings.json.tmp-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_encode(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
        if path.exists():
            backup = path.with_name(f"{path.name}.{_codex._timestamp()}.bak")
            shutil.copy2(path, backup)
        os.replace(temporary, path)
        temporary = None
        return backup
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# ------------------------------------------------------------------ install ---


def install_claude_usage_hooks(
    *,
    source_root: str | Path = REPOSITORY_ROOT,
    claude_home: str | Path = DEFAULT_CLAUDE_HOME,
    database_path: str | Path | None = None,
    python_interpreter: str | Path = sys.executable,
    dry_run: bool = False,
) -> InstallResult:
    """Install the frozen publisher and merge only its hook entries."""
    source = Path(source_root).expanduser().resolve(strict=True)
    home = Path(claude_home).expanduser().resolve()
    # Always pin --db: the explicit path, else AI_USAGE_DB_PATH, else the
    # shared default (~/.local/share/ai-usage/usage.db).
    database = Path(database_path or _codex._default_database_path()).expanduser().resolve()
    interpreter = _codex._validate_python_interpreter(python_interpreter)

    settings_file = home / "settings.json"
    existing, original_bytes = _load_settings(settings_file)
    install_directory = home / "usage-publisher"
    if install_directory.is_symlink() or (
        install_directory.exists() and not install_directory.is_dir()
    ):
        raise InstallError(f"Refusing to replace non-directory publisher path: {install_directory}")

    if dry_run:  # leave the Claude home untouched
        staging = Path(tempfile.mkdtemp(prefix="claude-usage-dry-run-")) / "stage"
    else:
        staging = home / f".usage-publisher.stage-{uuid.uuid4().hex}"
    releases = install_directory / "releases"
    release: Path | None = None
    release_created = False
    backup: Path | None = None
    try:
        staging.mkdir(parents=True)
        _copy_payload(source, staging)
        _validate_staged_publisher(interpreter, staging)
        digest = _codex._artifact_digest(staging)
        release = releases / digest
        if release.is_symlink() or (install_directory.exists() and releases.is_symlink()):
            raise InstallError(f"Refusing to use symlinked publisher release: {release}")

        script = release / PUBLISHER_RELATIVE_PATH
        command = _hook_command(interpreter, script, database)
        merged = _merge_hooks(existing, command, install_directory)
        settings_changed = _encode(merged) != original_bytes
        artifact_changed = not release.exists()

        if dry_run:
            return InstallResult(
                install_directory, release, script, settings_file, None,
                artifact_changed or settings_changed, merged,
            )

        if release.exists():
            if not release.is_dir() or _codex._artifact_digest(release) != digest:
                raise InstallError(f"Existing immutable publisher release is corrupt: {release}")
        else:
            releases.mkdir(parents=True, exist_ok=True)
            os.replace(staging, release)
            release_created = True
        if settings_changed:
            home.mkdir(parents=True, exist_ok=True)
            backup = _write_settings_atomic(settings_file, merged)
        return InstallResult(
            install_directory, release, script, settings_file, backup,
            artifact_changed or settings_changed, merged,
        )
    except Exception:
        if release_created and release is not None:
            shutil.rmtree(release, ignore_errors=True)
            _codex._remove_empty_directory(releases)
            _codex._remove_empty_directory(install_directory)
        raise
    finally:
        shutil.rmtree(staging.parent if dry_run else staging, ignore_errors=True)


def uninstall_claude_usage_hooks(
    *,
    claude_home: str | Path = DEFAULT_CLAUDE_HOME,
    dry_run: bool = False,
) -> InstallResult:
    """Remove only our hook entries (the frozen releases are left in place)."""
    home = Path(claude_home).expanduser().resolve()
    settings_file = home / "settings.json"
    install_directory = home / "usage-publisher"
    existing, original_bytes = _load_settings(settings_file)
    merged = _merge_hooks(existing, None, install_directory)
    changed = _encode(merged) != original_bytes and original_bytes is not None
    backup = None
    if changed and not dry_run:
        backup = _write_settings_atomic(settings_file, merged)
    return InstallResult(install_directory, None, None, settings_file, backup, changed, merged)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT,
                        help="dashboard checkout containing scripts/claude_usage_writer.py")
    parser.add_argument("--claude-home", type=Path, default=DEFAULT_CLAUDE_HOME,
                        help="Claude Code config directory (defaults to ~/.claude)")
    parser.add_argument("--db", type=Path, default=None,
                        help="usage SQLite path pinned into the hook command "
                             "(default: AI_USAGE_DB_PATH, else ~/.local/share/ai-usage/usage.db)")
    parser.add_argument("--python", type=Path, default=Path(sys.executable),
                        help="Python 3.12+ interpreter used by the hooks")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resulting hooks section without changing anything")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the usage hooks from settings.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    try:
        if args.uninstall:
            result = uninstall_claude_usage_hooks(
                claude_home=args.claude_home, dry_run=args.dry_run
            )
        else:
            result = install_claude_usage_hooks(
                source_root=args.repo_root,
                claude_home=args.claude_home,
                database_path=args.db,
                python_interpreter=args.python,
                dry_run=args.dry_run,
            )
    except (InstallError, OSError) as exc:
        print(f"Claude usage hook install failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("Dry run: no files were changed. Resulting hooks section:")
        print(json.dumps((result.settings or {}).get("hooks", {}), indent=2))
        return 0
    if args.uninstall:
        print("Removed Claude usage hooks." if result.changed else "No Claude usage hooks found.")
    else:
        print(f"Installed publisher: {result.install_directory}")
        print(f"Updated settings: {result.settings_file}")
        if not result.changed:
            print("Claude usage hooks are already up to date.")
    if result.backup_file is not None:
        print(f"Previous settings backed up to: {result.backup_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
