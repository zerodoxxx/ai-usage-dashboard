"""Tests for frozen Codex usage publisher deployment and hook merging."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from scripts import install_codex_usage_hooks as installer


def _make_source_tree(root: Path, publisher_value: str = "v1") -> Path:
    scripts = root / "scripts"
    src = root / "src"
    parsers = src / "parsers"
    scripts.mkdir(parents=True)
    parsers.mkdir(parents=True)

    (scripts / "codex_usage_writer.py").write_text(
        "from __future__ import annotations\n"
        "import argparse\n"
        "from pathlib import Path\n"
        "import sys\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from src.usage_store import VALUE\n"
        "from src.parsers.codex import MARKER\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--db')\n"
        "args = parser.parse_args()\n"
        "if args.db:\n"
        "    print('{}')\n"
        f"VALUE_MARKER = {publisher_value!r}\n",
        encoding="utf-8",
    )
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "usage_store.py").write_text("VALUE = 'store'\n", encoding="utf-8")
    (parsers / "__init__.py").write_text("", encoding="utf-8")
    (parsers / "codex.py").write_text("MARKER = 'parser'\n", encoding="utf-8")
    (parsers / "contracts.py").write_text("CONTRACT = True\n", encoding="utf-8")
    (src / "pricing_data.json").write_text('{"catalog": []}\n', encoding="utf-8")

    # These files must never enter a trusted user-level hook payload.
    (src / "config.json").write_text('{"private_config": true}\n', encoding="utf-8")
    (src / "credentials.json").write_text('{"secret": "no"}\n', encoding="utf-8")
    database = src / "usage.db"
    database.write_bytes(b"database")
    log = src / "debug.log"
    log.write_text("private log\n", encoding="utf-8")
    tests = src / "tests"
    tests.mkdir()
    (tests / "test_writer.py").write_text("test only\n", encoding="utf-8")
    pycache = src / "__pycache__"
    pycache.mkdir()
    (pycache / "module.pyc").write_bytes(b"compiled")
    return root


def _read_hooks(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _publisher_commands(document: dict, event: str, script: Path) -> list[str]:
    commands: list[str] = []
    for group in document["hooks"][event]:
        for hook in group.get("hooks", []):
            if str(script) in shlex.split(hook.get("command", "")):
                commands.append(hook["command"])
    return commands


def test_installs_frozen_payload_merges_existing_hooks_and_quotes_paths(
    tmp_path: Path,
) -> None:
    source = _make_source_tree(tmp_path / "dashboard repo with spaces")
    codex_home = tmp_path / "Codex Home with spaces"
    codex_home.mkdir()
    database = tmp_path / "AGY Data" / "token usage.db"
    database.parent.mkdir()
    database.write_bytes(b"test database must not be opened")

    original = {
        "description": "Keep this metadata",
        "other_top_level": {"retained": True},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "keep-existing"}],
                }
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": "existing-stop"}]}
            ],
        },
    }
    hooks_file = codex_home / "hooks.json"
    original_text = json.dumps(original, indent=2) + "\n"
    hooks_file.write_text(original_text, encoding="utf-8")

    result = installer.install_codex_usage_hooks(
        source_root=source,
        codex_home=codex_home,
        database_path=database,
        python_interpreter=sys.executable,
    )

    assert result.changed is True
    assert result.install_directory == codex_home / "usage-publisher"
    assert database.read_bytes() == b"test database must not be opened"
    assert result.backup_file is not None
    assert result.backup_file.name.startswith("hooks.json.")
    assert result.backup_file.name.endswith(".bak")
    assert result.backup_file.read_text(encoding="utf-8") == original_text

    installed = result.install_directory
    artifact = result.artifact_directory
    installed_script = result.publisher_script
    assert installed_script.is_relative_to(installed / "releases")
    assert installed_script.read_text(encoding="utf-8") == (
        source / "scripts/codex_usage_writer.py"
    ).read_text(encoding="utf-8")
    assert (artifact / "src/__init__.py").is_file()
    assert (artifact / "src/usage_store.py").is_file()
    assert (artifact / "src/parsers/codex.py").is_file()
    assert (artifact / "src/parsers/contracts.py").is_file()
    assert (artifact / "src/pricing_data.json").is_file()
    for excluded in (
        "src/config.json",
        "src/credentials.json",
        "src/usage.db",
        "src/debug.log",
        "src/tests/test_writer.py",
        "src/__pycache__/module.pyc",
    ):
        assert not (artifact / excluded).exists()

    document = _read_hooks(hooks_file)
    assert document["description"] == original["description"]
    assert document["other_top_level"] == original["other_top_level"]
    assert document["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    assert any(
        hook["command"] == "existing-stop"
        for group in document["hooks"]["Stop"]
        for hook in group["hooks"]
    )

    resolved_python = Path(os.path.abspath(sys.executable))
    for event in installer.HOOK_EVENTS:
        commands = _publisher_commands(document, event, installed_script)
        assert len(commands) == 1
        parts = shlex.split(commands[0])
        assert parts[0] == "PYTHONDONTWRITEBYTECODE=1"
        assert parts[1].startswith("CODEX_USAGE_PUBLISHER_SHA256=")
        assert parts[2] == str(resolved_python)
        assert parts[3] == str(installed_script)
        assert parts[4:] == ["--db", str(database.resolve())]
        publisher_hook = next(
            hook
            for group in document["hooks"][event]
            for hook in group["hooks"]
            if hook.get("command") == commands[0]
        )
        assert publisher_hook["timeout"] == (
            installer.INTERRUPT_TIMEOUT_SECONDS
            if event == "Interrupt"
            else installer.HOOK_TIMEOUT_SECONDS
        )
        assert publisher_hook["async"] is True

    # --help imports the deployed src package from the frozen sibling tree.
    validated = subprocess.run(
        [str(resolved_python), str(installed_script), "--help"],
        cwd=installed,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert validated.returncode == 0


def test_install_is_idempotent_without_rewriting_hooks_or_making_backups(
    tmp_path: Path,
) -> None:
    source = _make_source_tree(tmp_path / "repo")
    codex_home = tmp_path / "codex home"
    database = tmp_path / "agy db" / "token usage.db"

    first = installer.install_codex_usage_hooks(
        source_root=source,
        codex_home=codex_home,
        database_path=database,
        python_interpreter=sys.executable,
    )
    first_bytes = first.hooks_file.read_bytes()
    backups_after_first = list(codex_home.glob("hooks.json.*.bak"))
    assert first.backup_file is None

    second = installer.install_codex_usage_hooks(
        source_root=source,
        codex_home=codex_home,
        database_path=database,
        python_interpreter=sys.executable,
    )

    assert second.changed is False
    assert second.backup_file is None
    assert second.hooks_file.read_bytes() == first_bytes
    assert list(codex_home.glob("hooks.json.*.bak")) == backups_after_first
    document = _read_hooks(second.hooks_file)
    for event in installer.HOOK_EVENTS:
        commands = _publisher_commands(
            document,
            event,
            second.publisher_script,
        )
        assert len(commands) == 1


def test_environment_database_default_and_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source_tree(tmp_path / "repo")
    codex_home = tmp_path / "codex home"
    configured_database = tmp_path / "configured AGY db" / "usage.db"
    explicit_database = tmp_path / "explicit AGY db" / "usage.db"
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(configured_database))

    first = installer.install_codex_usage_hooks(
        source_root=source,
        codex_home=codex_home,
        python_interpreter=sys.executable,
    )
    first_document = _read_hooks(first.hooks_file)
    first_command = _publisher_commands(
        first_document,
        "Stop",
        first.publisher_script,
    )[0]
    assert shlex.split(first_command)[-1] == str(configured_database.resolve())

    second = installer.install_codex_usage_hooks(
        source_root=source,
        codex_home=codex_home,
        database_path=explicit_database,
        python_interpreter=sys.executable,
    )
    second_document = _read_hooks(second.hooks_file)
    commands = _publisher_commands(
        second_document,
        "Stop",
        second.publisher_script,
    )
    assert len(commands) == 1
    assert shlex.split(commands[0])[-1] == str(explicit_database.resolve())


def test_source_update_uses_a_new_immutable_release_and_updates_hook_command(
    tmp_path: Path,
) -> None:
    source = _make_source_tree(tmp_path / "repo", publisher_value="v1")
    codex_home = tmp_path / "codex home"
    first = installer.install_codex_usage_hooks(
        source_root=source,
        codex_home=codex_home,
        database_path=tmp_path / "agy db" / "usage.db",
        python_interpreter=sys.executable,
    )
    old_release = first.artifact_directory
    old_script = first.publisher_script
    old_command = _publisher_commands(
        _read_hooks(first.hooks_file), "Stop", old_script
    )[0]

    writer = source / "scripts/codex_usage_writer.py"
    writer.write_text(
        writer.read_text(encoding="utf-8").replace("'v1'", "'v2'"),
        encoding="utf-8",
    )

    second = installer.install_codex_usage_hooks(
        source_root=source,
        codex_home=codex_home,
        database_path=tmp_path / "agy db" / "usage.db",
        python_interpreter=sys.executable,
    )
    commands = _publisher_commands(
        _read_hooks(second.hooks_file), "Stop", second.publisher_script
    )

    assert second.artifact_directory != old_release
    assert old_release.is_dir()
    assert old_script.is_file()
    assert len(commands) == 1
    assert commands[0] != old_command
    assert old_command not in json.dumps(_read_hooks(second.hooks_file))
    assert str(second.publisher_script) in shlex.split(commands[0])


def test_failed_hooks_update_restores_previous_artifact_and_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source_tree(tmp_path / "repo", publisher_value="v1")
    codex_home = tmp_path / "codex home"
    database = tmp_path / "agy db" / "token usage.db"
    database.parent.mkdir()
    database.write_bytes(b"unchanged")

    first = installer.install_codex_usage_hooks(
        source_root=source,
        codex_home=codex_home,
        database_path=database,
        python_interpreter=sys.executable,
    )
    old_script = first.publisher_script
    old_contents = old_script.read_bytes()
    old_config = first.hooks_file.read_bytes()

    (source / "scripts/codex_usage_writer.py").write_text(
        (source / "scripts/codex_usage_writer.py")
        .read_text(encoding="utf-8")
        .replace("'v1'", "'v2'"),
        encoding="utf-8",
    )

    def fail_config_replace(*_args, **_kwargs):
        raise OSError("simulated atomic hooks replacement failure")

    monkeypatch.setattr(installer, "_write_hooks_file_atomic", fail_config_replace)
    with pytest.raises(OSError, match="simulated atomic hooks replacement failure"):
        installer.install_codex_usage_hooks(
            source_root=source,
            codex_home=codex_home,
            database_path=database,
            python_interpreter=sys.executable,
        )

    assert old_script.read_bytes() == old_contents
    assert first.hooks_file.read_bytes() == old_config
    assert not list(codex_home.glob(".usage-publisher.stage-*"))
    assert not list(codex_home.glob(".usage-publisher.previous-*"))


def test_missing_publisher_fails_without_partial_config_or_package(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incomplete repo"
    (source / "src").mkdir(parents=True)
    (source / "src/__init__.py").write_text("", encoding="utf-8")
    codex_home = tmp_path / "codex home"
    codex_home.mkdir()
    hooks_file = codex_home / "hooks.json"
    original = '{"hooks":{"PreToolUse":[]}}\n'
    hooks_file.write_text(original, encoding="utf-8")

    with pytest.raises(installer.InstallError, match="Publisher script is missing"):
        installer.install_codex_usage_hooks(
            source_root=source,
            codex_home=codex_home,
            database_path=tmp_path / "db" / "usage.db",
            python_interpreter=sys.executable,
        )

    assert hooks_file.read_text(encoding="utf-8") == original
    assert not (codex_home / "usage-publisher").exists()
    assert not list(codex_home.glob(".usage-publisher.stage-*"))


def test_invalid_existing_hooks_json_is_left_untouched(tmp_path: Path) -> None:
    source = _make_source_tree(tmp_path / "repo")
    codex_home = tmp_path / "codex home"
    codex_home.mkdir()
    hooks_file = codex_home / "hooks.json"
    original = "{ malformed\n"
    hooks_file.write_text(original, encoding="utf-8")

    with pytest.raises(installer.InstallError, match="not valid JSON"):
        installer.install_codex_usage_hooks(
            source_root=source,
            codex_home=codex_home,
            database_path=tmp_path / "db" / "usage.db",
            python_interpreter=sys.executable,
        )

    assert hooks_file.read_text(encoding="utf-8") == original
    assert not (codex_home / "usage-publisher").exists()


def test_current_repository_payload_stages_without_touching_database(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parent
    codex_home = tmp_path / "Codex home with spaces"
    database = tmp_path / "AGY database" / "token usage.db"

    result = installer.install_codex_usage_hooks(
        source_root=source,
        codex_home=codex_home,
        database_path=database,
        python_interpreter=sys.executable,
    )

    assert result.publisher_script.is_file()
    assert database.exists() is False
    assert not list(result.artifact_directory.rglob("__pycache__"))
    assert not list(result.artifact_directory.rglob("test_*.py"))
    assert (result.artifact_directory / "src/usage_store.py").is_file()
    assert (result.artifact_directory / "src/parsers/codex.py").is_file()
