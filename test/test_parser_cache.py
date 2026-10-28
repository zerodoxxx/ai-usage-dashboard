"""Behavioral tests for process-local parser result caching."""

from __future__ import annotations

import builtins
from copy import deepcopy
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from src.parsers import claude, codex
from src.parsers.claude import ClaudeCodeSource
from src.parsers.file_cache import ParsedFileCache


def _write_rollout(path: Path, *records: dict[str, Any]) -> str:
    text = "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return text + "\n"


def _rollout_usage(tokens: int, response_id: str) -> dict[str, Any]:
    return {
        "timestamp": "2026-10-01T10:00:00Z",
        "type": "token_usage_record",
        "payload": {
            "response_id": response_id,
            "usage": {
                "input_tokens": tokens - 2,
                "cached_input_tokens": 0,
                "output_tokens": 2,
                "total_tokens": tokens,
            },
        },
    }


def _write_claude_session(path: Path, session_id: str, tokens: int = 100) -> None:
    records = [
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": f"{session_id}-user",
            "timestamp": "2026-10-01T10:00:00Z",
            "message": {"role": "user", "content": "Cache test"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "uuid": f"{session_id}-assistant",
            "parentUuid": f"{session_id}-user",
            "timestamp": "2026-10-01T10:00:01Z",
            "message": {
                "id": f"{session_id}-event",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": tokens - 20,
                    "cache_read_input_tokens": 10,
                    "output_tokens": 10,
                },
            },
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
    )


def test_codex_warm_parse_matches_and_nested_mutations_are_isolated(
    tmp_path: Path, monkeypatch
) -> None:
    rollout = tmp_path / "rollout-test.jsonl"
    _write_rollout(rollout, _rollout_usage(100, "response-1"))
    parse_count = 0
    original_parser = codex._parse_rollout_file_uncached

    def counted_parser(path: Path) -> tuple[dict[str, Any], bool]:
        nonlocal parse_count
        parse_count += 1
        return original_parser(path)

    monkeypatch.setattr(codex, "_parse_rollout_file_uncached", counted_parser)
    first = codex._parse_rollout_file(rollout)
    expected = deepcopy(first)
    expected_tokens = first["total_tokens"]
    first["usage_events"][0]["metadata"]["caller_mutation"] = True
    first["usage_events"][0]["input_tokens"] = 999

    second = codex._parse_rollout_file(rollout)

    assert parse_count == 1
    assert second == expected
    assert second["total_tokens"] == expected_tokens
    assert second["usage_events"][0]["input_tokens"] != 999
    assert "caller_mutation" not in second["usage_events"][0]["metadata"]


def test_codex_cache_reparses_append_and_truncate(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout-changing.jsonl"
    first_text = _write_rollout(rollout, _rollout_usage(10, "response-1"))
    assert codex._parse_rollout_file(rollout)["total_tokens"] == 10

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_rollout_usage(25, "response-2"), separators=(",", ":")) + "\n")
    assert codex._parse_rollout_file(rollout)["total_tokens"] == 35

    rollout.write_text(first_text, encoding="utf-8")
    assert codex._parse_rollout_file(rollout)["total_tokens"] == 10


def test_codex_cache_detects_same_size_atomic_replacement(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout-replaced.jsonl"
    original = _rollout_usage(123, "response-1")
    replacement = _rollout_usage(321, "response-1")
    original_text = _write_rollout(rollout, original)
    assert codex._parse_rollout_file(rollout)["total_tokens"] == 123

    replacement_path = tmp_path / "replacement.jsonl"
    replacement_path.write_text(
        json.dumps(replacement, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    assert replacement_path.stat().st_size == len(original_text.encode("utf-8"))
    os.replace(replacement_path, rollout)

    assert codex._parse_rollout_file(rollout)["total_tokens"] == 321


def test_codex_failed_read_does_not_return_or_cache_stale_data(
    tmp_path: Path, monkeypatch
) -> None:
    rollout = tmp_path / "rollout-unreadable.jsonl"
    _write_rollout(rollout, _rollout_usage(10, "response-1"))
    assert codex._parse_rollout_file(rollout)["total_tokens"] == 10

    _write_rollout(rollout, _rollout_usage(20, "response-1"))
    original_open = builtins.open
    attempts = 0

    def failing_open(file: Any, *args: Any, **kwargs: Any):
        nonlocal attempts
        if Path(file).resolve() == rollout.resolve():
            attempts += 1
            raise PermissionError("simulated unreadable rollout")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", failing_open)
    first = codex._parse_rollout_file(rollout)
    second = codex._parse_rollout_file(rollout)

    assert first["total_tokens"] == 0
    assert second["total_tokens"] == 0
    assert attempts == 2


def test_claude_warm_parse_matches_and_nested_mutations_are_isolated(
    tmp_path: Path, monkeypatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_claude_session(transcript, "session-one")
    parse_count = 0
    original_parser = claude._parse_session_file_uncached

    def counted_parser(path: Path) -> tuple[Any, bool]:
        nonlocal parse_count
        parse_count += 1
        return original_parser(path)

    monkeypatch.setattr(claude, "_parse_session_file_uncached", counted_parser)
    first = claude._parse_session_file(transcript)
    assert first is not None
    expected = deepcopy(first)
    original_event_tokens = first.events[0].usage.input_tokens
    first.events[0].usage.input_tokens = 999
    first.events[0].metadata["caller_mutation"] = True

    second = claude._parse_session_file(transcript)

    assert second is not None
    assert parse_count == 1
    assert second == expected
    assert second.events[0].usage.input_tokens == original_event_tokens
    assert "caller_mutation" not in second.events[0].metadata


def test_claude_catalog_changes_reprice_cached_sessions(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "session-pricing.jsonl"
    _write_claude_session(transcript, "pricing-session")
    current_price = 1.0

    def changing_catalog(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "known",
            "cost_cached_usd": current_price,
            "cost_uncached_usd": current_price + 1,
            "savings_usd": 1,
        }

    monkeypatch.setattr(claude, "calculate_cost_strict", changing_catalog)
    first = claude._parse_session_file(transcript)
    assert first is not None and first.cost is not None and first.events[0].cost is not None
    assert float(first.cost.cached_usd) == 1.0
    assert float(first.events[0].cost.cached_usd) == 1.0

    current_price = 9.0
    second = claude._parse_session_file(transcript)

    assert second is not None and second.cost is not None and second.events[0].cost is not None
    assert float(second.cost.cached_usd) == 9.0
    assert float(second.events[0].cost.cached_usd) == 9.0


def test_claude_discovery_adds_and_drops_cached_sessions(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    first_path = root / "projects" / "project-a" / "session-a.jsonl"
    second_path = root / "projects" / "project-b" / "session-b.jsonl"
    _write_claude_session(first_path, "session-a")
    source = ClaudeCodeSource()

    assert [session.id for session in source.extract_sessions(root)] == ["session-a"]

    _write_claude_session(second_path, "session-b")
    assert {session.id for session in source.extract_sessions(root)} == {"session-a", "session-b"}

    first_path.unlink()
    assert [session.id for session in source.extract_sessions(root)] == ["session-b"]


def test_failed_reads_are_never_cached(tmp_path: Path) -> None:
    path = tmp_path / "present.jsonl"
    path.write_text("content\n", encoding="utf-8")
    cache: ParsedFileCache[dict[str, int]] = ParsedFileCache(max_entries=2)
    attempts = 0

    def failing_parser(_path: Path) -> tuple[dict[str, int], bool]:
        nonlocal attempts
        attempts += 1
        return {"attempt": attempts}, False

    assert cache.parse(path, failing_parser) == {"attempt": 1}
    assert cache.parse(path, failing_parser) == {"attempt": 2}
    assert attempts == 2


def test_overlapping_requests_share_one_parse(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.jsonl"
    path.write_text("stable\n", encoding="utf-8")
    cache: ParsedFileCache[dict[str, list[int]]] = ParsedFileCache()
    worker_count = 6
    all_ready = threading.Barrier(worker_count + 1)
    parser_started = threading.Event()
    release_parser = threading.Event()
    attempts = 0
    attempts_lock = threading.Lock()

    def parser(_path: Path) -> tuple[dict[str, list[int]], bool]:
        nonlocal attempts
        with attempts_lock:
            attempts += 1
        parser_started.set()
        assert release_parser.wait(timeout=2)
        return {"values": [1, 2, 3]}, True

    def request() -> dict[str, list[int]]:
        all_ready.wait(timeout=2)
        return cache.parse(path, parser)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(request) for _ in range(worker_count)]
        all_ready.wait(timeout=2)
        assert parser_started.wait(timeout=2)
        time.sleep(0.05)
        release_parser.set()
        results = [future.result(timeout=2) for future in futures]

    assert attempts == 1
    assert results == [{"values": [1, 2, 3]}] * worker_count
