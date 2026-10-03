"""Shared isolation for tests that exercise the usage store or HOME paths."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_usage_database_and_home(tmp_path, monkeypatch):
    home = tmp_path / ".isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(tmp_path / "usage.db"))
