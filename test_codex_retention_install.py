"""Synthetic coverage for immutable Codex retention LaunchAgent installation."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

from scripts.install_codex_retention import (
    InstallError,
    LAUNCH_AGENT_FILENAME,
    LAUNCH_AGENT_LABEL,
    RETENTION_DAYS,
    RUN_INTERVAL_SECONDS,
    SAFE_CODEX_VERSION,
    install_codex_retention,
)


def _fake_codex(path: Path, version: str = SAFE_CODEX_VERSION) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' 'codex-cli {version}'\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_installs_frozen_payload_and_idempotent_launch_agent(tmp_path: Path) -> None:
    codex_home = tmp_path / "Codex Home"
    codex_home.mkdir()
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    unrelated = launch_agents / "org.example.keep.plist"
    unrelated.write_bytes(plistlib.dumps({"Label": "org.example.keep", "RunAtLoad": True}))
    codex = _fake_codex(tmp_path / "codex")
    database = tmp_path / "shared usage" / "token_usage.db"

    installed = install_codex_retention(
        codex_home=codex_home,
        launch_agents_dir=launch_agents,
        database_path=database,
        codex_executable=codex,
    )

    assert installed.changed
    assert installed.artifact_directory.is_dir()
    assert installed.retention_script.is_file()
    assert installed.retention_script.stat().st_mode & 0o222 == 0
    assert installed.launch_agent_path.name == LAUNCH_AGENT_FILENAME
    assert unrelated.read_bytes() == plistlib.dumps({"Label": "org.example.keep", "RunAtLoad": True})
    document = plistlib.loads(installed.launch_agent_path.read_bytes())
    assert document["Label"] == LAUNCH_AGENT_LABEL
    assert document["StartInterval"] == RUN_INTERVAL_SECONDS == 900
    assert document["RunAtLoad"] is True
    assert document["StandardOutPath"] == os.devnull
    arguments = document["ProgramArguments"]
    assert arguments[arguments.index("--days") + 1] == str(RETENTION_DAYS)
    assert arguments[arguments.index("--db") + 1] == str(database)
    assert arguments[arguments.index("--codex-version") + 1] == SAFE_CODEX_VERSION
    assert arguments[arguments.index("--codex-cli") + 1] == str(codex.resolve())
    assert arguments[arguments.index("--log-file") + 1] == str(codex_home / "usage-retention" / "retention.log")

    second = install_codex_retention(
        codex_home=codex_home,
        launch_agents_dir=launch_agents,
        database_path=database,
        codex_executable=codex,
    )

    assert not second.changed
    assert second.backup_path is None
    assert second.artifact_directory == installed.artifact_directory


def test_rejects_codex_version_without_writing_a_job(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    codex = _fake_codex(tmp_path / "codex", "0.160.0")

    with pytest.raises(InstallError, match="must match exactly"):
        install_codex_retention(
            codex_home=codex_home,
            launch_agents_dir=launch_agents,
            codex_executable=codex,
        )

    assert not (launch_agents / LAUNCH_AGENT_FILENAME).exists()
    assert not (codex_home / "usage-retention").exists()


def test_refuses_to_replace_an_unrelated_existing_launch_agent(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    target = launch_agents / LAUNCH_AGENT_FILENAME
    target.write_bytes(plistlib.dumps({
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/other-job"],
        "Sentinel": "preserve",
    }))
    original = target.read_bytes()
    codex = _fake_codex(tmp_path / "codex")

    with pytest.raises(InstallError, match="unrelated LaunchAgent"):
        install_codex_retention(
            codex_home=codex_home,
            launch_agents_dir=launch_agents,
            codex_executable=codex,
        )

    assert target.read_bytes() == original


def test_writes_timestamped_backup_for_its_own_job_update(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    target = launch_agents / LAUNCH_AGENT_FILENAME
    target.write_bytes(plistlib.dumps({
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/usr/local/bin/python3", "/old/codex_retention.py"],
        "CustomKeepAliveSetting": "preserved",
    }))
    codex = _fake_codex(tmp_path / "codex")

    installed = install_codex_retention(
        codex_home=codex_home,
        launch_agents_dir=launch_agents,
        codex_executable=codex,
    )

    assert installed.backup_path is not None
    assert installed.backup_path.read_bytes() != target.read_bytes()
    document = plistlib.loads(target.read_bytes())
    assert document["CustomKeepAliveSetting"] == "preserved"
    assert document["ProgramArguments"] != ["/usr/local/bin/python3", "/old/codex_retention.py"]
