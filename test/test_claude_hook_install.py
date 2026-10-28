"""Tests for the Claude Code usage hook installer (settings.json merge)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import scripts.install_claude_usage_hooks as installer


def _make_source_tree(root: Path, value: str = "v1") -> Path:
    scripts = root / "scripts"
    parsers = root / "src" / "parsers"
    scripts.mkdir(parents=True)
    parsers.mkdir(parents=True)
    (scripts / "claude_usage_writer.py").write_text(
        "import argparse, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from src.usage_store import VALUE\n"
        "from src.parsers.claude import MARKER\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--db')\n"
        "p.parse_args()\n"
        f"MARK = {value!r}\n",
        encoding="utf-8",
    )
    (root / "src/__init__.py").write_text("", encoding="utf-8")
    (root / "src/usage_store.py").write_text("VALUE = 1\n", encoding="utf-8")
    (parsers / "__init__.py").write_text("", encoding="utf-8")
    (parsers / "claude.py").write_text("MARKER = 1\n", encoding="utf-8")
    (root / "src/secrets.json").write_text("{}\n", encoding="utf-8")
    (root / "src/usage.db").write_bytes(b"db")
    return root


def _install(tmp_path: Path, source: Path, home: Path, **kwargs):
    return installer.install_claude_usage_hooks(
        source_root=source, claude_home=home, python_interpreter=sys.executable, **kwargs
    )


def _ours(document: dict, event: str) -> list[dict]:
    return [
        h for g in document["hooks"][event] for h in g["hooks"]
        if "claude_usage_writer.py" in h.get("command", "")
    ]


EXISTING = {
    "permissions": {"allow": ["Bash(ls)"]},
    "model": "opus",
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-pre"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "existing-stop"}]}],
    },
}


def test_install_merges_preserves_settings_and_backs_up(tmp_path: Path) -> None:
    source = _make_source_tree(tmp_path / "repo with spaces")
    home = tmp_path / "Claude Home"
    home.mkdir()
    original_text = json.dumps(EXISTING, indent=2) + "\n"
    (home / "settings.json").write_text(original_text, encoding="utf-8")

    result = _install(tmp_path, source, home, database_path=tmp_path / "my usage.db")

    assert result.changed and result.backup_file is not None
    assert result.backup_file.name.startswith("settings.json.") and result.backup_file.name.endswith(".bak")
    assert result.backup_file.read_text(encoding="utf-8") == original_text

    document = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert document["permissions"] == EXISTING["permissions"] and document["model"] == "opus"
    assert document["hooks"]["PreToolUse"] == EXISTING["hooks"]["PreToolUse"]
    assert document["hooks"]["Stop"][0] == EXISTING["hooks"]["Stop"][0]
    for event in installer.HOOK_EVENTS:
        (handler,) = _ours(document, event)
        assert handler["type"] == "command" and handler["async"] is True
        assert str(result.publisher_script) in handler["command"]
        assert "'my usage.db'" in handler["command"] or str(tmp_path / "my usage.db") in handler["command"]

    assert result.publisher_script.is_relative_to(home / "usage-publisher/releases")
    artifact = result.artifact_directory
    assert (artifact / "src/parsers/claude.py").is_file()
    assert not (artifact / "src/secrets.json").exists() and not (artifact / "src/usage.db").exists()


def test_install_is_idempotent(tmp_path: Path) -> None:
    source = _make_source_tree(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(EXISTING), encoding="utf-8")
    _install(tmp_path, source, home)
    first = (home / "settings.json").read_bytes()
    backups = sorted(home.glob("settings.json.*.bak"))

    again = _install(tmp_path, source, home)

    assert again.changed is False and again.backup_file is None
    assert (home / "settings.json").read_bytes() == first
    assert sorted(home.glob("settings.json.*.bak")) == backups
    document = json.loads(first)
    assert all(len(_ours(document, e)) == 1 for e in installer.HOOK_EVENTS)
    assert not list(home.glob(".usage-publisher.stage-*"))


def test_source_update_replaces_only_our_handler(tmp_path: Path) -> None:
    source = _make_source_tree(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(EXISTING), encoding="utf-8")
    first = _install(tmp_path, source, home)
    _make_source_tree(tmp_path / "repo2", value="v2")
    second = _install(tmp_path, tmp_path / "repo2", home)

    assert second.publisher_script != first.publisher_script
    document = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    (handler,) = _ours(document, "Stop")
    assert str(second.publisher_script) in handler["command"]
    assert document["hooks"]["Stop"][0] == EXISTING["hooks"]["Stop"][0]


def test_creates_settings_when_absent(tmp_path: Path) -> None:
    source = _make_source_tree(tmp_path / "repo")
    home = tmp_path / "fresh"
    result = _install(tmp_path, source, home)
    assert result.backup_file is None
    document = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert set(document["hooks"]) == set(installer.HOOK_EVENTS)


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    source = _make_source_tree(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(EXISTING), encoding="utf-8")
    before = (home / "settings.json").read_bytes()

    result = _install(tmp_path, source, home, dry_run=True)

    assert result.changed and result.settings is not None
    assert all(len(_ours(result.settings, e)) == 1 for e in installer.HOOK_EVENTS)
    assert (home / "settings.json").read_bytes() == before
    assert sorted(p.name for p in home.iterdir()) == ["settings.json"]


def test_uninstall_removes_only_our_hooks(tmp_path: Path) -> None:
    source = _make_source_tree(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(EXISTING), encoding="utf-8")
    _install(tmp_path, source, home)

    result = installer.uninstall_claude_usage_hooks(claude_home=home)

    assert result.changed and result.backup_file is not None
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == EXISTING
    again = installer.uninstall_claude_usage_hooks(claude_home=home)
    assert again.changed is False


def test_uninstall_drops_hooks_key_when_nothing_else_remains(tmp_path: Path) -> None:
    source = _make_source_tree(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    _install(tmp_path, source, home)
    installer.uninstall_claude_usage_hooks(claude_home=home)
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {"model": "opus"}


def test_invalid_settings_are_left_untouched(tmp_path: Path) -> None:
    source = _make_source_tree(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(installer.InstallError):
        _install(tmp_path, source, home)
    assert (home / "settings.json").read_text(encoding="utf-8") == "{not json"
    assert not (home / "usage-publisher").exists()


def test_main_dry_run_and_uninstall(tmp_path: Path, capsys) -> None:
    source = _make_source_tree(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    args = ["--repo-root", str(source), "--claude-home", str(home), "--python", sys.executable]
    assert installer.main([*args, "--dry-run"]) == 0
    assert "Stop" in capsys.readouterr().out and not (home / "settings.json").exists()
    assert installer.main(args) == 0
    assert installer.main(["--claude-home", str(home), "--uninstall"]) == 0


def test_current_repository_payload_stages_cleanly(tmp_path: Path) -> None:
    result = installer.install_claude_usage_hooks(
        claude_home=tmp_path / "home", python_interpreter=sys.executable, dry_run=True
    )
    assert result.settings is not None and set(result.settings["hooks"]) == set(installer.HOOK_EVENTS)


def test_default_install_pins_tool_neutral_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.usage_store import DEFAULT_DB_RELATIVE_PATH

    monkeypatch.delenv("AI_USAGE_DB_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "Claude Home"
    home.mkdir()
    _install(tmp_path, _make_source_tree(tmp_path / "repo"), home)
    document = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    expected = str((Path.home() / DEFAULT_DB_RELATIVE_PATH).resolve())
    for event in installer.HOOK_EVENTS:
        (handler,) = _ours(document, event)
        assert expected in handler["command"]
