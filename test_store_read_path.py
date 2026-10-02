"""API regressions for the SQLite-only dashboard read path."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import src.app as app_module
from src.parsers import agy as agy_parser
from src.parsers import claude as claude_parser
from src.parsers import codex as codex_parser
from src.parsers import file_cache
from src.parsers.claude import ClaudeCodeSource
from src.parsers.contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession
from src.usage_store import write_usage_sessions


class _Response:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8")

    def json(self) -> dict:
        return json.loads(self.text)


class _InProcessASGITestClient:
    """Small stdlib ASGI client for environments without httpx/httpx2."""

    __test__ = False

    def __init__(self, app) -> None:
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def get(self, url: str) -> _Response:
        parsed = urlsplit(url)
        messages: list[dict] = []
        request_sent = False

        async def receive() -> dict:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": parsed.path,
            "raw_path": parsed.path.encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        asyncio.run(self.app(scope, receive, send))
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        return _Response(start["status"], body)


# Starlette 1.6 requires httpx2 for its TestClient; this environment has
# neither httpx nor httpx2. Keep the API test in-process without adding a dep.
TestClient = _InProcessASGITestClient


USAGE_RESPONSE_KEYS = {
    "tool",
    "timezone",
    "summary",
    "models",
    "timeline",
    "timeline_by_model",
    "hourly_timeline",
    "weekday_hour",
    "sessions",
    "unpriced_models",
    "heatmap_daily",
    "heatmap_by_model",
    "model_index",
    "time_range",
    "analytics",
    "pricing",
}


def _make_session(
    provider: str,
    session_id: str,
    title: str,
    model: str,
    *,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    estimated: bool = False,
    reported_cost: float | None = None,
) -> UsageSession:
    timestamp = datetime.now(timezone.utc) - timedelta(days=1)
    usage = TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + cache_write_tokens + output_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    cost = CostEstimate(
        cached_usd=reported_cost if reported_cost is not None else 0.08,
        uncached_usd=reported_cost if reported_cost is not None else 0.1,
        savings_usd=0.02 if reported_cost is None else 0,
        reported_usd=reported_cost,
        source="reported" if reported_cost is not None else "estimated",
    )
    event = UsageEvent(
        timestamp=timestamp,
        model=model,
        usage=usage,
        cost=cost,
        event_id=f"{session_id}-event-1",
    )
    return UsageSession(
        id=session_id,
        tool=provider,
        provider=provider,
        model=model,
        title=title,
        created_at=timestamp,
        start_time=timestamp,
        end_time=timestamp,
        activity_at=timestamp,
        usage=usage,
        events=[event],
        cost=cost,
        metadata={
            "estimated": estimated,
            "token_source": "estimated" if estimated else "reported",
        },
        call_count=1,
    )


def _seed_all_providers() -> dict[str, UsageSession]:
    sessions = {
        "codex": _make_session(
            "codex",
            "codex-db-session",
            "Stored Codex session",
            "gpt-6-luna",
            input_tokens=1_000,
            cached_tokens=200,
            output_tokens=300,
            reported_cost=0.42,
        ),
        "antigravity": _make_session(
            "antigravity",
            "agy-db-session",
            "Stored Antigravity session",
            "gemini-3.8-flash",
            input_tokens=2_000,
            cached_tokens=900,
            output_tokens=400,
            cache_write_tokens=20,
            estimated=True,
        ),
        "claude-code": _make_session(
            "claude-code",
            "claude-db-session",
            "Stored Claude Code session",
            "claude-sonnet-4-20250514",
            input_tokens=1_200,
            cached_tokens=600,
            output_tokens=400,
        ),
    }
    for provider, session in sessions.items():
        assert write_usage_sessions(provider, [session]) == 1
    return sessions


def _poison_home_with_provider_logs(home: Path) -> None:
    claude_path = home / ".claude" / "projects" / "x" / "session.jsonl"
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "type": "user",
                    "sessionId": "poison-claude",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {"role": "user", "content": "poison"},
                },
                {
                    "type": "assistant",
                    "sessionId": "poison-claude",
                    "uuid": "poison-claude-response",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {
                        "id": "poison-claude-event",
                        "role": "assistant",
                        "model": "claude-sonnet-4-20250514",
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": 9_000_000,
                            "cache_read_input_tokens": 4_000_000,
                            "output_tokens": 9_000_000,
                        },
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    codex_path = home / ".codex" / "sessions" / "rollout-2026-10-01-poison.jsonl"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"type": "session_meta", "payload": {"model": "gpt-6-luna"}},
                {
                    "type": "token_usage_record",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": {
                        "response_id": "poison-codex-event",
                        "usage": {
                            "input_tokens": 9_000_000,
                            "cached_input_tokens": 0,
                            "output_tokens": 9_000_000,
                            "total_tokens": 18_000_000,
                        },
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _assert_no_raw_file_reads(monkeypatch) -> None:
    def unexpected_raw_read(*_args, **_kwargs):
        raise AssertionError("The dashboard request path attempted to read provider files")

    monkeypatch.setattr(codex_parser, "parse_codex_usage", unexpected_raw_read)
    monkeypatch.setattr(codex_parser, "_parse_rollout_file", unexpected_raw_read)
    monkeypatch.setattr(agy_parser, "parse_agy_usage", unexpected_raw_read)
    monkeypatch.setattr(ClaudeCodeSource, "extract_sessions", unexpected_raw_read)
    monkeypatch.setattr(claude_parser, "_session_files", unexpected_raw_read)
    monkeypatch.setattr(codex_parser.Path, "glob", unexpected_raw_read)
    monkeypatch.setattr(codex_parser.Path, "rglob", unexpected_raw_read)
    monkeypatch.setattr(file_cache.ParsedFileCache, "parse", unexpected_raw_read)
    monkeypatch.setattr(file_cache.ParsedFileCache, "retain_paths", unexpected_raw_read)


def _assert_response_shape(payload: dict) -> None:
    assert set(payload) == USAGE_RESPONSE_KEYS
    assert isinstance(payload["summary"], dict)
    assert isinstance(payload["sessions"], list)
    assert isinstance(payload["models"], list)


def test_api_usage_reads_all_providers_from_store_without_raw_file_access(
    monkeypatch,
) -> None:
    sessions = _seed_all_providers()
    home = Path.home()
    _poison_home_with_provider_logs(home)

    # Pricing refresh is unrelated to this endpoint read-path regression and
    # otherwise attempts the external pricing feed on a fresh isolated cache.
    monkeypatch.setattr(app_module, "refresh_pricing", lambda: None)
    _assert_no_raw_file_reads(monkeypatch)

    expected_totals = {
        "codex": 1_300,
        "antigravity": 2_420,
        "claude-code": 1_600,
    }
    with TestClient(app_module.app) as client:
        responses = {
            query: client.get(f"/api/usage?tool={query}")
            for query in ("all", "codex", "agy", "claude-code")
        }

    for response in responses.values():
        assert response.status_code == 200, response.text
        _assert_response_shape(response.json())

    all_usage = responses["all"].json()
    assert all_usage["tool"] == "all"
    assert all_usage["summary"]["session_count"] == 3
    assert all_usage["summary"]["call_count"] == 3
    assert all_usage["summary"]["total_tokens"] == sum(expected_totals.values())
    assert {session["id"] for session in all_usage["sessions"]} == {
        session.id for session in sessions.values()
    }
    assert not any(session["id"].startswith("poison-") for session in all_usage["sessions"])
    for session in all_usage["sessions"]:
        assert session["title"] == sessions[session["tool"]].title
        assert session["total_tokens"] == expected_totals[session["tool"]]
        assert {"cost_source", "estimated", "token_source", "pricing_status"} <= set(session)
        assert session["cost_source"] in {"estimated", "reported"}
        assert session["token_source"] in {"estimated", "reported"}
        assert isinstance(session["estimated"], bool)
        assert session["pricing_status"]

    for query, provider in (("codex", "codex"), ("agy", "antigravity"), ("claude-code", "claude-code")):
        payload = responses[query].json()
        assert payload["tool"] == provider
        assert payload["summary"]["session_count"] == 1
        assert payload["summary"]["call_count"] == 1
        assert payload["summary"]["total_tokens"] == expected_totals[provider]
        assert [session["id"] for session in payload["sessions"]] == [sessions[provider].id]


def test_empty_providers_and_missing_database_return_successful_empty_payloads(
    monkeypatch, tmp_path: Path
) -> None:
    stored = _make_session(
        "codex",
        "only-codex-session",
        "Only Codex",
        "gpt-6-luna",
        input_tokens=100,
        cached_tokens=25,
        output_tokens=10,
    )
    assert write_usage_sessions("codex", [stored]) == 1
    monkeypatch.setattr(app_module, "refresh_pricing", lambda: None)

    with TestClient(app_module.app) as client:
        all_response = client.get("/api/usage?tool=all")
        agy_response = client.get("/api/usage?tool=agy")
        claude_response = client.get("/api/usage?tool=claude-code")

    for response in (all_response, agy_response, claude_response):
        assert response.status_code == 200, response.text
        _assert_response_shape(response.json())
    assert all_response.json()["summary"]["total_tokens"] == 110
    assert all_response.json()["summary"]["session_count"] == 1
    for response in (agy_response, claude_response):
        payload = response.json()
        assert payload["summary"]["total_tokens"] == 0
        assert payload["summary"]["session_count"] == 0
        assert payload["sessions"] == []
        assert payload["models"] == []

    missing_db = tmp_path / "does-not-exist" / "usage.db"
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(missing_db))
    with TestClient(app_module.app) as client:
        missing_response = client.get("/api/usage?tool=all")
    assert missing_response.status_code == 200, missing_response.text
    _assert_response_shape(missing_response.json())
    assert missing_response.json()["summary"]["total_tokens"] == 0
    assert missing_response.json()["summary"]["session_count"] == 0
    assert missing_response.json()["sessions"] == []
    assert not missing_db.exists()
    assert not missing_db.parent.exists()


def test_embedded_cache_write_rows_are_not_double_counted(monkeypatch) -> None:
    """Version 1 Antigravity rows already include cache writes in input."""
    import sqlite3

    from src.usage_store import resolve_db_path

    session = _make_session(
        "antigravity", "agy-embedded", "Embedded", "gemini-2.5-pro",
        input_tokens=1_000, cached_tokens=300, output_tokens=50, cache_write_tokens=400,
    )
    # Embedded semantics: total == input + output (cache writes already inside).
    for usage in (session.usage, session.events[0].usage):
        usage.total_tokens = 1_050
    session.cost = None
    session.events[0].cost = None
    write_usage_sessions("antigravity", [session])
    with sqlite3.connect(resolve_db_path()) as connection:
        connection.execute("UPDATE sessions SET cache_write_mode='embedded_in_input', usage_semantics_version=1")
        connection.execute("UPDATE token_events SET cache_write_mode='embedded_in_input', usage_semantics_version=1")
        db_total = connection.execute("SELECT SUM(total_tokens) FROM sessions").fetchone()[0]
    assert db_total == 1_050

    monkeypatch.setattr(app_module, "refresh_pricing", lambda: None)
    with TestClient(app_module.app) as client:
        response = client.get("/api/usage?tool=agy")
    assert response.status_code == 200, response.text
    summary = response.json()["summary"]
    assert summary["total_tokens"] == db_total
    # The embedded count is agy's estimate of uncached input, not a billable
    # cache write: it is zeroed on read and the remainder priced as input.
    assert summary["cache_write"] == 0
    assert summary["total_input"] == 1_000
    assert summary["cached_input"] == 300
    assert summary["uncached_input"] == 700
    assert summary["output"] == 50
    assert summary["total_input"] + summary["output"] == summary["total_tokens"]
    from src.pricing import calculate_cost_strict

    expected = calculate_cost_strict("gemini-2.5-pro", 700, 300, 50, provider="antigravity")
    assert expected["cost_cached_usd"] is not None
    assert summary["total_cost_usd"] == round(expected["cost_cached_usd"], 6)
