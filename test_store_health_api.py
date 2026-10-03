"""Capture-health API regressions, including recovery across cached polls."""

from __future__ import annotations

import sqlite3

import pytest

import src.app as app_module
import src.usage_store as store_module
from src.parsers import aggregator
from src.usage_store import write_usage_sessions
from test_store_read_path import TestClient, _make_session


@pytest.fixture(autouse=True)
def isolate_health_caches(monkeypatch):
    monkeypatch.setattr(app_module, "refresh_pricing", lambda: None)
    app_module._USAGE_RESPONSE_CACHE.clear()
    aggregator._SESSION_CACHE.clear()
    yield
    app_module._USAGE_RESPONSE_CACHE.clear()
    aggregator._SESSION_CACHE.clear()


def _get(client, query=""):
    response = client.get(f"/api/usage{query}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert {"summary", "sessions", "models", "timeline", "analytics", "pricing"} <= payload.keys()
    assert set(payload["store"]) == {"path", "database_exists", "readable", "error", "providers"}
    return payload


def _write(provider="codex", session_id="health"):
    write_usage_sessions(provider, [_make_session(
        provider, session_id, "Health", "gpt-6-luna",
        input_tokens=100, cached_tokens=0, output_tokens=10, reported_cost=0.1,
    )])


def test_missing_database_does_not_create_or_cache_it(monkeypatch, tmp_path):
    path = tmp_path / "missing-parent" / "usage.db"
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(path))
    with TestClient(app_module.app) as client:
        payload = _get(client)
        assert payload["store"] == {
            "path": str(path), "database_exists": False, "readable": False,
            "error": None, "providers": {},
        }
        assert payload["summary"]["total_tokens"] == 0
        assert not path.parent.exists()
        assert aggregator.usage_cache_key() is None
        assert not app_module._USAGE_RESPONSE_CACHE
        _write()
        recovered = _get(client)
        assert recovered["store"]["readable"] is True
        assert recovered["store"]["providers"]["codex"]["sessions"] == 1
        assert recovered["summary"]["total_tokens"] == 110


def test_corrupt_database_returns_actionable_health_and_recovers(tmp_path):
    path = tmp_path / "usage.db"
    path.write_bytes(b"not a SQLite database")
    with TestClient(app_module.app) as client:
        payload = _get(client)
        assert payload["store"]["database_exists"] is True
        assert payload["store"]["readable"] is False
        assert payload["store"]["error"]
        assert payload["store"]["providers"] == {}
        assert payload["summary"]["total_tokens"] == 0
        assert aggregator.usage_cache_key() is None
        assert not app_module._USAGE_RESPONSE_CACHE
        path.unlink()
        _write()
        assert _get(client)["store"]["readable"] is True


def test_provider_last_writes_are_unfiltered_and_read_only(monkeypatch, tmp_path):
    path = tmp_path / "usage.db"
    timestamps = {
        "codex": "2026-09-29T10:00:00+00:00",
        "claude-code": "2026-10-01T11:00:00+00:00",
        "antigravity": "2026-10-02T12:00:00+00:00",
    }
    for provider in timestamps:
        _write(provider)
    _write("codex", "earlier")
    with sqlite3.connect(path) as connection:
        for provider, timestamp in timestamps.items():
            connection.execute("UPDATE sessions SET updated_at=? WHERE provider=?", (timestamp, provider))
        connection.execute("UPDATE sessions SET updated_at='2026-09-01T00:00:00+00:00' WHERE session_id='codex:earlier'")
    original_connect = sqlite3.connect
    connections = []

    def read_only_connect(database, *args, **kwargs):
        connections.append(str(database))
        assert kwargs.get("uri") is True
        assert str(database).endswith("?mode=ro")
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", read_only_connect)
    with TestClient(app_module.app) as client:
        payload = _get(client, "?tool=codex&time_range=24h")
    assert connections
    assert payload["store"]["readable"] is True
    assert payload["store"]["error"] is None
    assert payload["store"]["providers"] == {
        provider: {"last_write_at": timestamp, "sessions": 2 if provider == "codex" else 1}
        for provider, timestamp in timestamps.items()
    }


def test_readability_changes_bypass_a_warm_response_without_stat_changes(monkeypatch, tmp_path):
    _write()
    with TestClient(app_module.app) as client:
        _get(client)
        _get(client)
        assert app_module._USAGE_RESPONSE_CACHE
        path = tmp_path / "usage.db"
        signature = path.stat()
        probe = store_module.read_store_health

        def inaccessible(db_path=None):
            return {"path": str(path), "database_exists": True, "readable": False,
                    "error": "permission denied", "providers": {}}

        monkeypatch.setattr(store_module, "read_store_health", inaccessible)
        broken = _get(client)
        assert broken["store"]["readable"] is False
        assert broken["store"]["error"] == "permission denied"
        assert path.stat().st_mtime_ns == signature.st_mtime_ns
        monkeypatch.setattr(store_module, "read_store_health", probe)
        assert _get(client)["store"]["readable"] is True


def test_valid_empty_database_is_readable_without_being_migrated(tmp_path):
    path = tmp_path / "usage.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
    before = path.read_bytes()
    with TestClient(app_module.app) as client:
        payload = _get(client)
    assert payload["store"]["readable"] is True
    assert payload["store"]["providers"] == {}
    assert path.read_bytes() == before


def test_status_read_failure_is_not_cached(monkeypatch):
    _write()
    original_status = store_module.get_usage_store_status
    monkeypatch.setattr(store_module, "get_usage_store_status", lambda *_: {"read_error": True})
    with TestClient(app_module.app) as client:
        payload = _get(client)
        assert payload["store"]["readable"] is False
        assert payload["store"]["error"]
        assert not app_module._USAGE_RESPONSE_CACHE
        monkeypatch.setattr(store_module, "get_usage_store_status", original_status)
        assert _get(client)["store"]["readable"] is True
