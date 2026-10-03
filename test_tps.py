"""Effective output throughput from synthetic, isolated provider transcripts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.parsers.aggregator import _build_usage_data, _filter_usage_data, get_tool_usage
from src.parsers.claude import _parse_session_file
from src.parsers.codex import CodexSource, _parse_rollout_file
from tests_support import legacy_file_source_registry

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
TPS_FIELDS = {
    "tps", "tps_median", "tps_p10", "tps_p90", "tps_calls",
    "tps_output_tokens", "tps_duration_seconds", "tps_status",
}


def _ts(seconds: float) -> str:
    return (NOW + timedelta(seconds=seconds)).isoformat()


def _usage(output: int = 100, reasoning: int = 40, input_tokens: int = 10) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output,
    }


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def _item(seconds: float, kind: str, **payload) -> dict:
    return {"timestamp": _ts(seconds), "type": "response_item", "payload": {"type": kind, **payload}}


def _token(seconds: float, usage: dict, **payload) -> dict:
    return {"timestamp": _ts(seconds), "type": "token_usage_record", "payload": {"usage": usage, **payload}}


def _status(seconds: float, last: dict, cumulative: dict) -> dict:
    return {
        "timestamp": _ts(seconds), "type": "event_msg",
        "payload": {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": cumulative}},
    }


def _rollout(root: Path, records: list[dict]) -> Path:
    return _write(root / "sessions" / "rollout-test.jsonl", [
        {"timestamp": _ts(0), "type": "session_meta", "payload": {"model": "gpt-6-luna"}},
        *records,
    ])


def _user(seconds: float, uuid: str = "user", parent: str | None = None, **extra) -> dict:
    return {
        "type": "user", "sessionId": "session", "uuid": uuid, "parentUuid": parent,
        "timestamp": _ts(seconds), "message": {"role": "user", "content": "fixture input"},
        **extra,
    }


def _assistant(
    seconds: float, output: int = 100, uuid: str = "assistant", parent: str | None = "user",
    message_id: str = "message", complete: bool = True,
) -> dict:
    return {
        "type": "assistant", "sessionId": "session", "uuid": uuid, "parentUuid": parent,
        "timestamp": _ts(seconds),
        "message": {
            "id": message_id, "role": "assistant", "model": "claude-sonnet-5-5",
            "stop_reason": "end_turn" if complete else None, "usage": _usage(output),
        },
    }


def _raw_session(events: list[dict], tool: str = "codex", model: str = "gpt-6-luna") -> dict:
    return {
        "id": "session", "tool": tool, "model": model, "created_at": _ts(-100),
        "call_count": len(events), "output": sum(e.get("output_tokens", 0) for e in events),
        "total_tokens": sum(e.get("total_tokens", 0) for e in events), "usage_events": events,
    }


def _timed(output: int, duration, **metadata) -> dict:
    return {
        "timestamp": _ts(0), **_usage(output),
        "metadata": {"tps_duration_seconds": duration, "tps_output_tokens": output, "tps_trustworthy": True, **metadata},
    }


def _null_tps(row: dict) -> None:
    assert {key for key in row if key.startswith("tps")} == TPS_FIELDS
    assert row["tps_calls"] == row["tps_output_tokens"] == 0
    assert row["tps_duration_seconds"] == 0.0
    assert row["tps_status"] == "unavailable"
    assert all(row[key] is None for key in ("tps", "tps_median", "tps_p10", "tps_p90"))


def test_tps_ratio_of_sums_percentiles_and_reasoning_once() -> None:
    result = _build_usage_data([_raw_session([_timed(100, 1), _timed(100, 9)])], "codex")
    row = result["models"][0]
    assert row["tps"] == 20.0
    assert row["tps_median"] == 55.6
    assert row["tps_p10"] == 20.0
    assert row["tps_p90"] == 91.1
    assert row["tps_output_tokens"] == row["output"] == 200
    assert row["reasoning_output"] == 80
    assert row["tps_duration_seconds"] == 10.0
    assert row["tps_calls"] == 2
    assert row["tps_status"] == "approximate"
    assert {key for key in row if key.startswith("tps")} == TPS_FIELDS
    assert "usage_events" not in result["sessions"][0]
    assert "timing" not in row
    assert "tps_trustworthy" not in json.dumps(result)


@pytest.mark.parametrize("duration,output,metadata", [
    (0.5, 100, {}), (301, 100, {}), (10, 31, {}), (None, 100, {}),
    (0, 100, {}), (-1, 100, {}), (float("nan"), 100, {}),
    (float("inf"), 100, {}), (True, 100, {}),
    (10, 100, {"tps_trustworthy": False}), (10, 100, {"synthetic": "session-residual"}),
    (10, 100, {"call_count": 2}),
])
def test_tps_filters_and_zero_accepted_calls(duration, output: int, metadata: dict) -> None:
    result = _build_usage_data([_raw_session([_timed(output, duration, **metadata)])], "codex")
    _null_tps(result["models"][0])


@pytest.mark.parametrize("duration", [1, 300])
def test_tps_filter_boundaries_are_inclusive(duration: int) -> None:
    row = _build_usage_data([_raw_session([_timed(32, duration)])], "codex")["models"][0]
    assert row["tps_calls"] == 1
    assert row["tps_output_tokens"] == 32
    assert row["tps_duration_seconds"] == duration


def test_codex_modern_pairing_excludes_tool_execution_and_duplicate_status(tmp_path: Path) -> None:
    one = _usage()
    cumulative = _usage(output=200, reasoning=80, input_tokens=20)
    path = _rollout(tmp_path, [
        _item(0, "message", role="user"),
        _item(2, "reasoning"), _item(5, "custom_tool_call", call_id="tool"),
        _token(5, one, response_id="response-1", thread_token_usage=one),
        _item(200, "custom_tool_call_output", call_id="tool"),
        _status(201, one, one),
        _item(202, "message", role="assistant"),
        _token(205, one, response_id="response-2", thread_token_usage=cumulative),
        _status(206, one, cumulative),
    ])
    parsed = _parse_rollout_file(path)
    assert parsed["call_count"] == 2
    assert [e["metadata"]["tps_duration_seconds"] for e in parsed["usage_events"]] == [5, 5]
    assert [e["event_id"] for e in parsed["usage_events"]] == ["response-1", "response-2"]
    result = get_tool_usage(
        "codex", codex_dir=tmp_path, registry=legacy_file_source_registry()
    )
    assert result["models"][0]["tps"] == 20
    assert result["models"][0]["tps_calls"] == 2


def test_codex_legacy_completion_precedes_tool_result_and_status(tmp_path: Path) -> None:
    one = _usage()
    path = _rollout(tmp_path, [
        _item(0, "message", role="user"), _item(2, "reasoning"),
        _item(5, "function_call", call_id="tool"),
        _item(25, "function_call_output", call_id="tool"), _status(26, one, one),
        _status(27, one, one),
        _item(30, "message", role="assistant"),
        _status(31, one, _usage(output=200, reasoning=80, input_tokens=20)),
    ])
    events = _parse_rollout_file(path)["usage_events"]
    assert [e["timestamp"] for e in events] == [_ts(5), _ts(30)]
    assert [e["metadata"]["tps_duration_seconds"] for e in events] == [5, 5]
    assert all(e["metadata"]["tps_trustworthy"] for e in events)


def test_codex_reconciled_usage_is_not_timed(tmp_path: Path) -> None:
    _rollout(tmp_path, [
        _item(0, "message", role="user"), _item(5, "message", role="assistant"),
        _token(5, _usage(), thread_token_usage=_usage(output=200, input_tokens=20)),
    ])
    result = get_tool_usage(
        "codex", codex_dir=tmp_path, registry=legacy_file_source_registry()
    )
    assert result["models"][0]["output"] == 200
    _null_tps(result["models"][0])


def test_codex_cumulative_delta_with_missing_calls_is_not_timed(tmp_path: Path) -> None:
    _rollout(tmp_path, [
        _item(0, "message", role="user"), _item(5, "message", role="assistant"),
        _status(5, _usage(), _usage(output=300, input_tokens=30)),
    ])
    _null_tps(
        get_tool_usage(
            "codex", codex_dir=tmp_path, registry=legacy_file_source_registry()
        )["models"][0]
    )


def test_codex_model_switch_and_missing_input(tmp_path: Path) -> None:
    _rollout(tmp_path, [
        _item(0, "message", role="user"), _item(5, "message", role="assistant"),
        _token(5, _usage(), response_id="first"),
        {"type": "turn_context", "timestamp": _ts(6), "payload": {"model": "gpt-6-sol"}},
        _item(10, "message", role="assistant"), _token(10, _usage(), response_id="second"),
    ])
    rows = {
        r["model"]: r
        for r in get_tool_usage(
            "codex", codex_dir=tmp_path, registry=legacy_file_source_registry()
        )["models"]
    }
    assert rows["gpt-6-luna"]["tps_calls"] == 1
    _null_tps(rows["gpt-6-sol"])


def test_claude_final_usage_and_last_block_timestamp(tmp_path: Path) -> None:
    path = _write(tmp_path / "session.jsonl", [
        _user(0), _assistant(2, 8, uuid="thinking", complete=False),
        _assistant(10, 200, parent="thinking", complete=True),
    ])
    session = _parse_session_file(path)
    assert session is not None
    assert session.call_count == 1
    assert session.usage.output_tokens == 200
    assert session.usage.total_tokens == 210
    event = session.events[0]
    assert event.timestamp == NOW + timedelta(seconds=10)
    assert event.metadata["tps_duration_seconds"] == 10
    assert event.metadata["tps_output_tokens"] == 200
    assert event.metadata["tps_trustworthy"]
    row = get_tool_usage(
        "claude-code", claude_dir=tmp_path, registry=legacy_file_source_registry()
    )["models"][0]
    assert row["tps"] == 20


def test_claude_nearest_tool_result_ancestor_through_attachment(tmp_path: Path) -> None:
    tool_result = _user(100, uuid="result", parent="tool-call")
    tool_result["message"]["content"] = [{"type": "tool_result", "tool_use_id": "tool", "content": "fixture result"}]
    path = _write(tmp_path / "session.jsonl", [
        _user(0), _assistant(5, uuid="tool-call", message_id="first"), tool_result,
        {"type": "attachment", "uuid": "attachment", "parentUuid": "result", "timestamp": _ts(101)},
        _user(104, uuid="unrelated", parent=None),
        _assistant(110, uuid="second", parent="attachment", message_id="second"),
    ])
    session = _parse_session_file(path)
    assert session is not None
    assert session.events[1].metadata["tps_duration_seconds"] == 10


def test_claude_incomplete_response_is_not_timed(tmp_path: Path) -> None:
    _write(tmp_path / "session.jsonl", [_user(0), _assistant(5, complete=False)])
    result = get_tool_usage(
        "claude-code", claude_dir=tmp_path, registry=legacy_file_source_registry()
    )
    assert result["models"][0]["output"] == 100
    _null_tps(result["models"][0])


def test_claude_copied_message_id_uses_final_usage_once(tmp_path: Path) -> None:
    _write(tmp_path / "projects" / "test" / "first.jsonl", [_user(0), _assistant(2, 8, complete=False)])
    _write(tmp_path / "projects" / "test" / "second.jsonl", [_user(0), _assistant(10, 200)])
    result = get_tool_usage(
        "claude-code", claude_dir=tmp_path, registry=legacy_file_source_registry()
    )
    assert result["summary"]["call_count"] == 1
    assert result["summary"]["output"] == 200
    assert result["models"][0]["tps_calls"] == 1


def test_completion_inside_window_counts_entire_call(tmp_path: Path) -> None:
    _rollout(tmp_path, [
        _item(-86410, "message", role="user"),
        _item(-86390, "message", role="assistant"), _token(-86390, _usage()),
        _item(-30, "message", role="user"),
        _item(10, "message", role="assistant"), _token(10, _usage()),
    ])
    sessions = [s.to_legacy_dict() for s in CodexSource().extract_sessions(tmp_path)]
    result = _filter_usage_data({"tool": "codex", "sessions": sessions}, "24h", now=NOW)
    row = result["models"][0]
    assert row["tps_calls"] == 1
    assert row["tps_duration_seconds"] == 20
    assert row["tps_output_tokens"] == 100
    assert row["tps"] == 5


def test_tool_filter_and_agy_rows_are_unavailable(tmp_path: Path) -> None:
    codex_root, claude_root, agy_root = (tmp_path / name for name in ("codex", "claude", "agy"))
    _rollout(codex_root, [_item(0, "message", role="user"), _item(5, "message", role="assistant"), _token(5, _usage())])
    _write(claude_root / "projects" / "test" / "session.jsonl", [_user(0), _assistant(10)])
    _write(agy_root / "brain" / "session" / "transcript.jsonl", [
        {"created_at": _ts(0), "type": "USER_INPUT", "source": "USER", "content": "fixture input"},
        {"created_at": _ts(5), "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "fixture output " * 50},
    ])
    registry = legacy_file_source_registry()
    roots = {"codex": codex_root, "claude-code": claude_root, "antigravity": agy_root}
    codex_only = get_tool_usage("codex", registry=registry, source_dirs=roots)
    assert len(codex_only["models"]) == 1
    assert codex_only["models"][0]["tool"] == "codex"
    assert codex_only["models"][0]["tps_calls"] == 1
    combined = get_tool_usage("all", registry=registry, source_dirs=roots)
    assert len(combined["models"]) == 3
    for row in combined["models"]:
        assert {key for key in row if key.startswith("tps")} == TPS_FIELDS
        if row["tool"] == "antigravity":
            _null_tps(row)
        else:
            assert row["tps_status"] == "approximate"
    # Unsupported sources cannot opt into timing by supplying metadata.
    row = _build_usage_data([_raw_session([_timed(100, 5)], "antigravity", "Gemini 3.8 Flash (High)")], "antigravity")["models"][0]
    _null_tps(row)
