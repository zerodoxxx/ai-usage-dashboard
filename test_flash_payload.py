"""Additive frontend payload contracts, using only synthetic usage records."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.parsers import aggregator
from src.parsers.claude import ClaudeCodeSource
from src.parsers.contracts import UsageSession
from src.parsers.source_registry import SourceRegistry

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
SERIES_FIELDS = {"total_tokens", "call_count", "cost_cached_usd"}
ZERO = {"total_tokens": 0, "call_count": 0, "cost_cached_usd": 0.0}


@pytest.fixture(autouse=True)
def local_timezone(monkeypatch):
    monkeypatch.setattr(aggregator, "local_timezone", lambda: LOCAL_TZ)


def _event(timestamp, model=None, *, cost=None, metadata=None):
    event = {
        "timestamp": timestamp,
        "input_tokens": 100,
        "cached_input_tokens": 25,
        "cache_write_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 130,
    }
    if model is not None:
        event["model"] = model
    if cost is not None:
        event["reported_cost_usd"] = cost
    if metadata is not None:
        event["metadata"] = metadata
    return event


def _session(session_id, tool, model, events, *, provider=None, estimated=False):
    return {
        "id": session_id,
        "tool": tool,
        "provider": provider or tool,
        "model": model,
        "created_at": "2026-09-01T12:00:00+00:00",
        "call_count": sum((event.get("metadata") or {}).get("call_count", 1) for event in events),
        "uncached_input": 75 * len(events),
        "cached_input": 25 * len(events),
        "cache_write": 10 * len(events),
        "output": 20 * len(events),
        "total_tokens": 130 * len(events),
        "estimated": estimated,
        "token_source": "estimated" if estimated else "reported",
        "usage_events": events,
    }


def _build(sessions, time_range="all", **kwargs):
    return aggregator._filter_usage_data(
        {"tool": "all", "sessions": deepcopy(sessions)}, time_range, now=NOW, **kwargs,
    )


def _check_series(payload, daily_name, series_name):
    daily = payload[daily_name]
    series = payload[series_name]
    assert {row["key"] for row in payload["models"]} <= series.keys()
    for values in series.values():
        assert len(values) == len(daily)
        for value in values:
            assert set(value) == SERIES_FIELDS
            assert type(value["total_tokens"]) is int
            assert type(value["call_count"]) is int
            assert type(value["cost_cached_usd"]) is float
    for index, row in enumerate(daily):
        assert sum(values[index]["total_tokens"] for values in series.values()) == row["total_tokens"]
        assert sum(values[index]["call_count"] for values in series.values()) == row["call_count"]
        assert sum(values[index]["cost_cached_usd"] for values in series.values()) == pytest.approx(
            row["cost_cached_usd"], abs=1e-6, rel=0,
        )


def _series_sessions():
    return [
        _session("mixed", "codex", "gpt-6-luna", [
            _event("2026-09-27T20:00:00+00:00", "gpt-6-luna", cost=0.1234564),
            _event("2026-09-29T20:00:00+00:00", "deepseek-v4", cost=0.2345674),
        ]),
        _session("old", "antigravity", "gemini-3.8-flash", [
            _event("2026-09-15T12:00:00+00:00", cost=0.3456784),
        ], estimated=True),
        _session("unknown-a", "vendor-a", "mystery-model", [
            _event("2026-09-30T12:00:00+00:00"),
        ]),
        _session("unknown-b", "vendor-b", "mystery-model", [
            _event("2026-09-30T12:00:00+00:00"),
        ]),
    ]


def test_model_keys_unique_and_stable_across_builds_and_ranges():
    sessions = _series_sessions()
    first, second, bounded = _build(sessions), _build(sessions), _build(sessions, "7d")
    keys = [row["key"] for row in first["models"]]
    assert len(keys) == len(set(keys))
    assert all(isinstance(key, str) and "|" in key for key in keys)
    assert "vendor-a|mystery-model" in keys
    assert "vendor-b|mystery-model" in keys
    for row in first["models"]:
        assert row["key"] == f"{row['provider']}|{row['canonical_model']}"
    identity = lambda data: {(row["provider"], row["canonical_model"]): row["key"] for row in data["models"]}
    assert identity(first) == identity(second)
    assert all(identity(first)[model] == key for model, key in identity(bounded).items())


@pytest.mark.parametrize("time_range,kwargs", [
    ("all", {}), ("7d", {}),
    ("custom", {"start": "2026-09-14", "end": "2026-09-16"}),
])
def test_model_index_covers_all_public_keys_and_matches_display_metadata(time_range, kwargs):
    sessions = _series_sessions() + [
        _session("latest", "codex", "gpt-6-luna", [
            _event((NOW - timedelta(minutes=index + 1)).isoformat()) for index in range(45)
        ]),
        _session("claude-heatmap", "claude-code", "claude-sonnet-5-5", [
            _event("2026-09-20T12:00:00+00:00"),
        ]),
        _session("shared-old", "other-tool", "gpt-6-luna", [
            _event("2026-09-20T13:00:00+00:00"),
        ]),
    ]
    payload = _build(sessions, time_range, **kwargs)
    keys = (
        {model["key"] for model in payload["models"]}
        | set(payload["timeline_by_model"])
        | set(payload["heatmap_by_model"])
    )
    assert set(payload["model_index"]) == keys
    for metadata in payload["model_index"].values():
        assert set(metadata) == {"tool", "model"}
        assert all(isinstance(value, str) for value in metadata.values())
    for model in payload["models"]:
        assert payload["model_index"][model["key"]] == {"tool": model["tool"], "model": model["model"]}
    claude_key = next(key for key in payload["heatmap_by_model"] if key.startswith("claude|"))
    assert payload["model_index"][claude_key] == {"tool": "claude-code", "model": "Claude Sonnet 5.5"}
    if time_range != "all":
        assert claude_key not in {model["key"] for model in payload["models"]}


@pytest.mark.parametrize("time_range,kwargs", [
    ("all", {}), ("30d", {}), ("7d", {}), ("24h", {}),
    ("custom", {"start": "2026-09-14", "end": "2026-09-16"}),
])
def test_model_series_alignment_sums_and_zero_fill(time_range, kwargs):
    payload = _build(_series_sessions(), time_range, **kwargs)
    assert set(payload["timeline_by_model"]) == {row["key"] for row in payload["models"]}
    _check_series(payload, "timeline", "timeline_by_model")
    _check_series(payload, "heatmap_daily", "heatmap_by_model")
    assert len(payload["heatmap_daily"]) == 30
    assert payload["heatmap_daily"][0]["date"] == "2026-09-02"
    assert payload["heatmap_daily"][-1]["date"] == "2026-10-01"
    heatmap = payload["heatmap_by_model"]
    model_key = next(key for key in heatmap if key.startswith("codex|"))
    index = next(i for i, row in enumerate(payload["heatmap_daily"]) if row["date"] == "2026-09-28")
    assert heatmap[model_key][index] == {
        "total_tokens": 130, "call_count": 1, "cost_cached_usd": 0.1234564,
    }
    assert heatmap[model_key][index + 1] == ZERO
    if time_range == "all":
        timeline_index = next(i for i, row in enumerate(payload["timeline"]) if row["date"] == "2026-09-28")
        assert payload["timeline_by_model"][model_key][timeline_index] == heatmap[model_key][index]
    if time_range == "custom":
        assert model_key not in payload["timeline_by_model"]
    if time_range == "7d":
        old_key = next(key for key in heatmap if key.startswith("antigravity|"))
        assert old_key not in payload["timeline_by_model"]
        assert old_key == next(row["key"] for row in _build(_series_sessions())["models"] if row["tool"] == "antigravity")


def test_model_series_cost_precision_with_many_models():
    sessions = [
        _session(str(index), "vendor", f"model-{index}", [
            _event("2026-09-30T12:00:00+00:00", cost=0.0000004),
        ]) for index in range(12)
    ]
    payload = _build(sessions)
    _check_series(payload, "timeline", "timeline_by_model")
    _check_series(payload, "heatmap_daily", "heatmap_by_model")


def test_session_fallbacks_and_zero_call_residuals():
    session = _session("untimed", "codex", "gpt-6-luna", [_event(None)])
    session["created_at"] = "2026-09-30T12:00:00+00:00"
    no_events = {**session, "id": "aggregate", "usage_events": []}
    residual = _session("residual", "codex", "gpt-6-luna", [
        _event("2026-09-30T13:00:00+00:00", metadata={"call_count": 0, "synthetic": "session-residual"}),
    ])
    for time_range in ("all", "7d"):
        payload = _build([session, no_events, residual], time_range)
        _check_series(payload, "timeline", "timeline_by_model")
        _check_series(payload, "heatmap_daily", "heatmap_by_model")


class _Source:
    aliases = ()

    def __init__(self, key, session):
        self.key, self.session = key, session

    def extract_sessions(self, root=None):
        return [UsageSession.from_legacy_dict(deepcopy(self.session))]


@pytest.mark.parametrize("tool", ["codex", "antigravity"])
def test_model_series_respect_registered_tool_filter(tool):
    registry = SourceRegistry([
        _Source("codex", _session("codex", "codex", "gpt-6-luna", [_event("2026-09-25T12:00:00+00:00")])),
        _Source("antigravity", _session("agy", "antigravity", "gemini-3.8-flash", [_event("2026-09-25T13:00:00+00:00")], estimated=True)),
    ])
    payload = aggregator.get_tool_usage(tool, registry=registry)
    assert len(payload["models"]) == 1
    assert payload["models"][0]["tool"] == tool
    assert set(payload["model_index"]) == {payload["models"][0]["key"]}
    _check_series(payload, "timeline", "timeline_by_model")


def test_claude_global_message_dedup_is_shared_with_new_fields(tmp_path):
    for index in range(2):
        directory = tmp_path / "projects" / f"project-{index}"
        directory.mkdir(parents=True)
        record = {
            "type": "assistant", "sessionId": f"session-{index}", "uuid": f"uuid-{index}",
            "timestamp": "2026-09-25T12:00:00+00:00",
            "message": {
                "id": "shared-message", "role": "assistant", "model": "claude-sonnet-5-5",
                "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        }
        (directory / "session.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    payload = aggregator.get_tool_usage("claude-code", claude_dir=tmp_path, registry=SourceRegistry([ClaudeCodeSource()]))
    assert payload["summary"]["call_count"] == 1
    assert len(payload["models"]) == 1
    assert payload["models"][0]["total_tokens"] == 120
    _check_series(payload, "timeline", "timeline_by_model")
    _check_series(payload, "heatmap_daily", "heatmap_by_model")


def test_empty_payload_has_aligned_empty_maps():
    payload = _build([], "7d")
    assert payload["models"] == []
    assert payload["timeline_by_model"] == payload["heatmap_by_model"] == {}
    assert payload["model_index"] == {}
    _check_series(payload, "timeline", "timeline_by_model")
    _check_series(payload, "heatmap_daily", "heatmap_by_model")
