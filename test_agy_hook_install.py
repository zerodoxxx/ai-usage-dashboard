"""Flat AGY hook installation tests; all deployments use temporary homes."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from scripts import install_agy_usage_hooks as installer


EXISTING = {
    "keep": {"enabled": True, "PreInvocation": [{"type": "command", "command": "keep-command"}]},
    "agy-token-tracker": {
        "enabled": True,
        "PostInvocation": [{"type": "command", "command": "python /old/track_usage.py"}],
        "Stop": [{"type": "command", "command": "python /old/track_usage.py"}],
    },
}


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "repo with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "src/parsers").mkdir(parents=True)
    (root / "scripts/agy_usage_writer.py").write_text("import argparse\nargparse.ArgumentParser().parse_args()\n")
    for name in ("src/__init__.py", "src/usage_store.py", "src/pricing.py", "src/parsers/__init__.py", "src/parsers/contracts.py"):
        (root / name).write_text("")
    (root / "src/secret.json").write_text("{}")
    (root / "src/usage.db").write_bytes(b"private")
    return root


def _seed(tmp_path, document=EXISTING):
    home = tmp_path / "Gemini Home"
    hooks = home / "config/hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps(document, indent=2) + "\n")
    return home, hooks


def _install(source, home, **kwargs):
    return installer.install_agy_usage_hooks(
        source_root=source, gemini_home=home, python_interpreter=sys.executable, **kwargs
    )


def test_replaces_legacy_flat_entry_with_backup_and_frozen_release(source, tmp_path):
    home, hooks = _seed(tmp_path)
    before = hooks.read_bytes()
    db = tmp_path / "my usage.db"
    result = _install(source, home, database_path=db)
    assert result.changed and result.backup_file.read_bytes() == before
    assert result.backup_file.name.startswith("hooks.json.") and result.backup_file.suffix == ".bak"
    document = json.loads(hooks.read_text())
    assert document["keep"] == EXISTING["keep"]
    assert document[installer.ENTRY_NAME]["enabled"] is True
    for event in installer.HOOK_EVENTS:
        (handler,) = document[installer.ENTRY_NAME][event]
        assert set(handler) == {"type", "command"} and handler["type"] == "command"
        parts = shlex.split(handler["command"])
        assert str(result.publisher_script) in parts and str(db) in parts
        assert str(home / "antigravity-cli") in parts
        assert "/old/track_usage.py" not in handler["command"]
    assert result.publisher_script.is_relative_to(home / "usage-publisher/releases")
    assert (result.artifact_directory / "src/usage_store.py").is_file()
    assert not (result.artifact_directory / "src/usage.db").exists()
    assert not (result.artifact_directory / "src/secret.json").exists()
    # Deployed bytes survive repository edits and branch switches.
    deployed = result.publisher_script.read_bytes()
    (source / "scripts/agy_usage_writer.py").write_text("print('new source')\n")
    assert result.publisher_script.read_bytes() == deployed


def test_idempotence_and_source_update(source, tmp_path):
    home, hooks = _seed(tmp_path)
    first = _install(source, home)
    before = hooks.read_bytes()
    backups = list(hooks.parent.glob("*.bak"))
    again = _install(source, home)
    assert not again.changed and again.backup_file is None
    assert hooks.read_bytes() == before and list(hooks.parent.glob("*.bak")) == backups
    (source / "src/usage_store.py").write_text("VERSION = 2\n")
    newer = _install(source, home)
    assert newer.publisher_script != first.publisher_script
    assert first.publisher_script.is_file()
    assert json.loads(hooks.read_text())["keep"] == EXISTING["keep"]
    assert not list(home.glob(".usage-publisher.stage-*"))


def test_dry_run_and_fresh_install(source, tmp_path):
    home = tmp_path / "fresh"
    result = _install(source, home, dry_run=True)
    assert result.changed and not home.exists()
    installer._validate_hooks(result.hooks)
    result = _install(source, home)
    assert result.changed and result.backup_file is None
    assert set(json.loads(result.hooks_file.read_text())) == {installer.ENTRY_NAME}


def test_uninstall_preserves_unrelated_hooks_and_legacy_entry(source, tmp_path):
    home, hooks = _seed(tmp_path)
    assert not installer.uninstall_agy_usage_hooks(gemini_home=home).changed
    _install(source, home)
    before = hooks.read_bytes()
    preview = installer.uninstall_agy_usage_hooks(gemini_home=home, dry_run=True)
    assert preview.changed and hooks.read_bytes() == before
    result = installer.uninstall_agy_usage_hooks(gemini_home=home)
    assert result.changed and result.backup_file.read_bytes() == before
    assert json.loads(hooks.read_text()) == {"keep": EXISTING["keep"]}
    assert not installer.uninstall_agy_usage_hooks(gemini_home=home).changed


@pytest.mark.parametrize("bad", [
    {"hooks": [{"type": "command", "command": "nested"}]},
    {"type": "prompt", "command": "wrong-type"},
    {"type": "command"},
    {"type": "command", "command": ""},
    {"type": "command", "command": 42},
    "command",
])
def test_malformed_unrelated_hook_refuses_entire_install(source, tmp_path, bad):
    home, hooks = _seed(tmp_path, {"other": {"Stop": [bad]}, **EXISTING})
    before = hooks.read_bytes()
    with pytest.raises(installer.InstallError):
        _install(source, home)
    assert hooks.read_bytes() == before
    assert not (home / "usage-publisher").exists()
    assert not list(hooks.parent.glob("*.bak"))


def test_repairs_malformed_tracker_but_keeps_its_backup(source, tmp_path):
    home, hooks = _seed(tmp_path, {installer.ENTRY_NAME: {"Stop": [{"hooks": []}]}})
    before = hooks.read_bytes()
    result = _install(source, home)
    assert result.backup_file.read_bytes() == before
    installer._validate_hooks(json.loads(hooks.read_text()))


def test_invalid_json_and_corrupt_release_are_untouched(source, tmp_path):
    home, hooks = _seed(tmp_path)
    hooks.write_text("{bad json")
    with pytest.raises(installer.InstallError):
        _install(source, home)
    assert hooks.read_text() == "{bad json"
    assert not (home / "usage-publisher").exists()
    hooks.write_text(json.dumps(EXISTING))
    result = _install(source, home)
    result.publisher_script.write_text("tampered\n")
    before = hooks.read_bytes()
    with pytest.raises(installer.InstallError, match="corrupt"):
        _install(source, home)
    assert hooks.read_bytes() == before


def test_main_python_dry_run_and_uninstall(source, tmp_path, capsys):
    home = tmp_path / "new"
    args = ["--repo-root", str(source), "--gemini-home", str(home), "--python", sys.executable]
    assert installer.main([*args, "--dry-run"]) == 0
    assert "PostInvocation" in capsys.readouterr().out and not home.exists()
    assert installer.main(args) == 0
    assert installer.main(["--gemini-home", str(home), "--uninstall"]) == 0
    assert json.loads((home / "config/hooks.json").read_text()) == {}


def test_current_repo_payload_validates_and_installed_cli_captures(tmp_path):
    import subprocess
    from src.usage_store import read_usage_sessions

    home = tmp_path / "gemini"
    db = tmp_path / "usage.db"
    result = installer.install_agy_usage_hooks(
        gemini_home=home, python_interpreter=sys.executable, database_path=db,
    )
    base = home / "antigravity-cli"
    transcript = base / "brain/fresh-conversation/transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('\n'.join(map(json.dumps, [
        {"source": "USER", "content": "hello"},
        {"source": "MODEL", "content": "hi"},
    ])))
    command = result.hooks[installer.ENTRY_NAME]["Stop"][0]["command"]
    invocation = subprocess.run(
        command, shell=True, input=json.dumps({"conversationId": "fresh-conversation"}),
        text=True, capture_output=True, check=False,
    )
    assert invocation.returncode == 0, invocation.stderr
    assert len(read_usage_sessions("antigravity", db_path=db)) == 1
