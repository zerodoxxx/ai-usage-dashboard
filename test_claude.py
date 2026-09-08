"""Focused tests for Claude Code transcript extraction."""

import json
from pathlib import Path

from src.parsers.aggregator import get_tool_usage
from src.parsers.claude import ClaudeCodeSource


def _write_session(root: Path) -> Path:
    session_dir = root / "projects" / "-tmp-project"
    session_dir.mkdir(parents=True)
    path = session_dir / "session-1.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "session-1",
            "cwd": "/tmp/project",
            "timestamp": "2026-09-08T10:00:00Z",
            "message": {"role": "user", "content": "Track this Claude Code session"},
        },
        {
            "type": "assistant",
            "uuid": "wrapper-1",
            "timestamp": "2026-09-08T10:00:01Z",
            "message": {
                "id": "message-1",
                "role": "assistant",
                "model": "claude-sonnet-4-20250514",
                "usage": {
                    "input_tokens": 1_000,
                    "cache_read_input_tokens": 2_000,
                    "cache_creation_input_tokens": 500,
                    "output_tokens": 100,
                },
            },
        },
        # Claude Code can persist the same response in another wrapper record.
        {
            "type": "assistant",
            "uuid": "wrapper-2",
            "timestamp": "2026-09-08T10:00:02Z",
            "message": {
                "id": "message-1",
                "role": "assistant",
                "model": "claude-sonnet-4-20250514",
                "usage": {
                    "input_tokens": 1_000,
                    "cache_read_input_tokens": 2_000,
                    "cache_creation_input_tokens": 500,
                    "output_tokens": 100,
                },
            },
        },
        {
            "type": "assistant",
            "uuid": "wrapper-3",
            "timestamp": "2026-09-08T10:01:00Z",
            "message": {
                "id": "message-2",
                "role": "assistant",
                "model": "claude-sonnet-4-20250514",
                "usage": {
                    "input_tokens": 400,
                    "cache_read_input_tokens": 2_600,
                    "output_tokens": 200,
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def test_claude_source_deduplicates_and_preserves_cache_metrics(tmp_path: Path) -> None:
    _write_session(tmp_path / ".claude")
    sessions = ClaudeCodeSource().extract_sessions(tmp_path / ".claude")

    assert len(sessions) == 1
    session = sessions[0]
    assert session.tool == "claude-code"
    assert session.provider == "claude"
    assert session.title == "Track this Claude Code session"
    assert session.call_count == 2
    assert session.usage.input_tokens == 6_000
    assert session.usage.cached_input_tokens == 4_600
    assert session.usage.cache_write_tokens == 500
    assert session.usage.output_tokens == 300
    assert session.cost is not None
    assert session.cost.source == "estimated"
    assert session.events[0].cost is not None


def test_claude_source_flows_through_registered_aggregator(tmp_path: Path) -> None:
    _write_session(tmp_path / ".claude")
    result = get_tool_usage(
        "cc",
        source_dirs={"claude-code": tmp_path / ".claude"},
    )

    assert result["tool"] == "claude-code"
    assert result["summary"]["session_count"] == 1
    assert result["summary"]["call_count"] == 2
    assert result["summary"]["total_tokens"] == 6_300
    assert result["sessions"][0]["pricing_status"] == "estimated"
