"""Safe synthetic tests for Codex transcript retention planning and apply guards."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.codex_retention as codex_retention
from scripts.codex_retention import (
    SAFE_CODEX_VERSION,
    apply_retention_plan,
    build_retention_plan,
    find_active_codex_processes,
)
from src.parsers.contracts import TokenUsage, UsageEvent, UsageSession
from src.usage_store import mark_provider_capture_enabled, read_usage_sessions, write_usage_sessions


_ROOT_ID = "11111111-1111-4111-8111-111111111111"
_CHILD_ID = "22222222-2222-4222-8222-222222222222"


def _state_db(path: Path, threads: list[tuple[str, str | None, str]], edges: list[tuple[str, str]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT,
                updated_at TEXT,
                recency_at TEXT
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT,
                child_thread_id TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO threads (id, rollout_path, updated_at, recency_at) VALUES (?, ?, ?, ?)",
            [(thread_id, rollout_path, activity, activity) for thread_id, rollout_path, activity in threads],
        )
        connection.executemany(
            "INSERT INTO thread_spawn_edges VALUES (?, ?)",
            edges,
        )
        connection.commit()
    finally:
        connection.close()


def _make_capture(
    codex_root: Path,
    db_path: Path,
    *,
    now: datetime,
    root_recent: bool = False,
    child_recent: bool = False,
    capture_child: bool = True,
    root_fragments: int = 2,
) -> tuple[list[Path], datetime]:
    sessions_root = codex_root / "sessions" / "2026" / "09" / "01"
    sessions_root.mkdir(parents=True)
    cutoff_old = now - timedelta(days=20)
    root_activity = now - timedelta(days=1) if root_recent else cutoff_old
    child_activity = now - timedelta(days=1) if child_recent else cutoff_old
    root_paths: list[Path] = []
    all_paths: list[Path] = []
    captures: list[UsageSession] = []

    def add_thread(thread_id: str, activity: datetime, fragments: int) -> None:
        paths: list[Path] = []
        for index in range(fragments):
            filename = f"rollout-2026-09-01-{thread_id}-{index}.jsonl"
            path = sessions_root / filename
            path.write_text("synthetic transcript body not parsed by retention\n", encoding="utf-8")
            old_ns = int((now - timedelta(days=20)).timestamp() * 1_000_000_000)
            os.utime(path, ns=(old_ns, old_ns))
            paths.append(path)
            all_paths.append(path)
        sources: dict[str, dict[str, int]] = {}
        source_hashes: list[str] = []
        for path in paths:
            stat = path.stat()
            source_hash = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
            source_hashes.append(source_hash)
            sources[source_hash] = {
                "capture_mtime_ns": stat.st_mtime_ns,
                "capture_ctime_ns": stat.st_ctime_ns,
                "capture_size": stat.st_size,
            }
        usage = TokenUsage(input_tokens=10, output_tokens=4, total_tokens=14, preserve_total=True)
        events = [UsageEvent(
            timestamp=activity,
            usage=usage,
            model="gpt-5-codex",
            event_id=f"{thread_id}-event",
            metadata={"capture_source_hash": source_hashes[0]},
        )]
        captures.append(UsageSession(
            id=thread_id,
            tool="codex",
            provider="codex",
            model="gpt-5-codex",
            start_time=activity,
            end_time=activity,
            activity_at=activity,
            usage=usage,
            events=events,
            metadata={"capture_sources": sources},
        ))
        if thread_id == _ROOT_ID:
            root_paths.extend(paths)

    add_thread(_ROOT_ID, root_activity, root_fragments)
    child_paths: list[Path] = []
    if capture_child:
        add_thread(_CHILD_ID, child_activity, 1)
        child_paths = [all_paths[-1]]

    thread_rows: list[tuple[str, str | None, str]] = [
        (_ROOT_ID, str(root_paths[0]), root_activity.isoformat()),
    ]
    edges: list[tuple[str, str]] = []
    if capture_child:
        thread_rows.append((_CHILD_ID, str(child_paths[0]), child_activity.isoformat()))
        edges.append((_ROOT_ID, _CHILD_ID))
    _state_db(codex_root / "state_5.sqlite", thread_rows, edges)
    write_usage_sessions("codex", captures, db_path=db_path)
    mark_provider_capture_enabled("codex", db_path=db_path)
    return all_paths, min(root_activity, child_activity) if capture_child else root_activity


def test_plan_requires_exact_sqlite_capture_for_entire_old_thread_tree(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    paths, _ = _make_capture(codex_root, db_path, now=now)

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert len(plan.trees) == 1
    assert plan.trees[0].root_id == _ROOT_ID
    assert set(plan.trees[0].thread_ids) == {_ROOT_ID, _CHILD_ID}
    assert len(plan.trees[0].sources) == 3
    assert plan.reclaimable_bytes == sum(path.stat().st_size for path in paths)


def test_recent_descendant_protects_the_whole_tree(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    _make_capture(codex_root, db_path, now=now, child_recent=True)

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.skipped["within_retention_window"] == 1


def test_changed_transcript_revision_is_not_eligible(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    paths, _ = _make_capture(codex_root, db_path, now=now)
    paths[0].write_text("changed after usage was captured\n", encoding="utf-8")

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.skipped["transcript_not_exactly_captured"] == 1


def test_disabled_capture_does_not_qualify_transcripts(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    _make_capture(codex_root, db_path, now=now)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE usage_capture_state SET enabled=0 WHERE provider='codex'")
        connection.commit()
    finally:
        connection.close()

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.skipped["sqlite_capture_not_enabled"] == 1


def test_apply_refuses_while_codex_is_running(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    paths, _ = _make_capture(codex_root, db_path, now=now)
    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)
    calls: list[list[str]] = []

    deleted, errors = apply_retention_plan(
        plan,
        days=15,
        codex_dir=codex_root,
        db_path=db_path,
        process_check=lambda: ["Codex"],
        run=lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0),
        codex_executable="/usr/bin/codex",
        expected_codex_version=SAFE_CODEX_VERSION,
        now=lambda: now,
    )

    assert deleted == 0
    assert errors == ["Codex is running; no conversations were deleted"]
    assert calls == []
    assert all(path.is_file() for path in paths)


def test_apply_uses_codex_delete_and_keeps_sqlite_usage(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    paths, _ = _make_capture(codex_root, db_path, now=now)
    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)
    calls: list[list[str]] = []
    process_checks = 0

    def check_closed() -> list[str]:
        nonlocal process_checks
        process_checks += 1
        return []

    def delete_with_codex(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout=f"codex-cli {SAFE_CODEX_VERSION}\n", stderr="")
        for path in paths:
            path.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    deleted, errors = apply_retention_plan(
        plan,
        days=15,
        codex_dir=codex_root,
        db_path=db_path,
        process_check=check_closed,
        run=delete_with_codex,
        codex_executable="/usr/bin/codex",
        expected_codex_version=SAFE_CODEX_VERSION,
        now=lambda: now,
    )

    assert deleted == 1
    assert errors == []
    assert process_checks >= 3
    assert calls == [
        ["/usr/bin/codex", "--version"],
        ["/usr/bin/codex", "--no-daemon", "delete", _ROOT_ID, "--force"],
    ]
    assert read_usage_sessions("codex", db_path=db_path)
    assert not any(path.exists() for path in paths)


def test_apply_refuses_unreviewed_codex_version(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    paths, _ = _make_capture(codex_root, db_path, now=now)
    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)
    calls: list[list[str]] = []

    def changed_version(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="codex-cli 0.160.0\n", stderr="")

    deleted, errors = apply_retention_plan(
        plan,
        days=15,
        codex_dir=codex_root,
        db_path=db_path,
        process_check=lambda: [],
        run=changed_version,
        codex_executable="/usr/bin/codex",
        expected_codex_version=SAFE_CODEX_VERSION,
        now=lambda: now,
    )

    assert deleted == 0
    assert errors == ["Codex CLI version changed; no conversations were deleted"]
    assert calls == [["/usr/bin/codex", "--version"]]
    assert all(path.is_file() for path in paths)


def test_symlink_rollout_is_never_followed_or_eligible(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    paths, _ = _make_capture(codex_root, db_path, now=now)
    source = paths[0]
    payload = source.read_text(encoding="utf-8")
    source.unlink()
    source.symlink_to(paths[1])

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.skipped["transcript_not_exactly_captured"] == 1
    assert paths[1].read_text(encoding="utf-8") == payload


def test_symlinked_transcript_directory_fails_closed_for_the_plan(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    _make_capture(codex_root, db_path, now=now)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (codex_root / "sessions" / "2026" / "09" / "01" / "linked").symlink_to(
        elsewhere,
        target_is_directory=True,
    )

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.error == "retention scan failed (RuntimeError)"


def test_transcript_walk_error_fails_closed_for_the_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    _make_capture(codex_root, db_path, now=now)

    def fail_walk(_path: Path, *, followlinks: bool, onerror: object):
        del followlinks
        assert callable(onerror)
        onerror(PermissionError("synthetic inaccessible directory"))
        yield  # pragma: no cover - the callback raises before traversal

    monkeypatch.setattr(codex_retention.os, "walk", fail_walk)

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.error == "retention scan failed (RuntimeError)"


def test_process_scan_fails_closed_when_ps_omits_this_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_retention.os, "getpid", lambda: 77)
    result = subprocess.CompletedProcess(
        ["ps"],
        0,
        stdout="12 /usr/bin/python3\n",
        stderr="",
    )

    with pytest.raises(RuntimeError, match="could not verify"):
        find_active_codex_processes(run=lambda *_args, **_kwargs: result)
