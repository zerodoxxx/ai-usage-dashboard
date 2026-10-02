"""Safe synthetic tests for Codex transcript retention planning and apply guards."""

from __future__ import annotations

import hashlib
import json
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
from src.parsers.codex import extract_codex_session_for_capture
from src.usage_store import LEGACY_DB_RELATIVE_PATH, mark_provider_capture_enabled, read_usage_sessions, write_usage_sessions


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


def _write_rollout(path: Path, thread_id: str, activity: datetime, ordinal: int) -> None:
    usage = {
        "input_tokens": 10,
        "cached_input_tokens": 3,
        "cache_write_input_tokens": 2,
        "output_tokens": 4,
        "reasoning_output_tokens": 1,
        "total_tokens": 14,
    }
    record = {
        "timestamp": activity.isoformat().replace("+00:00", "Z"),
        "ordinal": ordinal,
        "type": "token_usage_record",
        "payload": {
            "thread_id": thread_id,
            "response_id": f"{thread_id}-{ordinal}",
            "usage": usage,
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


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
            _write_rollout(path, thread_id, activity, index + 1)
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
        usage = TokenUsage(
            input_tokens=10 * fragments,
            cached_input_tokens=3 * fragments,
            output_tokens=4 * fragments,
            reasoning_output_tokens=1 * fragments,
            cache_write_tokens=2 * fragments,
            total_tokens=14 * fragments,
            preserve_total=True,
        )
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


def test_provider_wide_capture_flag_does_not_replace_per_file_verification(tmp_path: Path) -> None:
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

    assert len(plan.trees) == 1


def test_missing_session_row_keeps_the_whole_tree(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    _make_capture(codex_root, db_path, now=now)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "DELETE FROM sessions WHERE provider='codex' AND session_id=?",
            (f"codex:{_CHILD_ID}",),
        )
        connection.commit()
    finally:
        connection.close()

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.skipped["sqlite_usage_missing"] == 1


def test_sqlite_total_below_combined_fragments_keeps_the_tree(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    _make_capture(codex_root, db_path, now=now, capture_child=False, root_fragments=2)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """UPDATE sessions
               SET input_tokens=10, cached_input_tokens=3, output_tokens=4,
                   cache_write_tokens=2, reasoning_output_tokens=1, total_tokens=14
               WHERE provider='codex' AND session_id=?""",
            (f"codex:{_ROOT_ID}",),
        )
        connection.commit()
    finally:
        connection.close()

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.skipped["sqlite_usage_below_rollout_totals"] == 1


def test_cached_input_cannot_mask_a_shortfall_in_uncached_input(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    _make_capture(codex_root, db_path, now=now, capture_child=False, root_fragments=1)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """UPDATE sessions SET cached_input_tokens=9
               WHERE provider='codex' AND session_id=?""",
            (f"codex:{_ROOT_ID}",),
        )
        connection.commit()
    finally:
        connection.close()

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.skipped["sqlite_usage_below_rollout_totals"] == 1


def test_missing_shared_database_refuses_apply_before_process_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "missing" / "usage.db"
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    seed_db = tmp_path / "seed.db"
    paths, _ = _make_capture(
        codex_root,
        seed_db,
        now=datetime(2026, 10, 2, 12, tzinfo=timezone.utc),
    )
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(db_path))
    plan = build_retention_plan(codex_dir=codex_root)
    assert plan.trees == []
    assert plan.error == "shared usage database is missing or unreadable"

    checked = False

    def process_check() -> list[str]:
        nonlocal checked
        checked = True
        return []

    deleted, errors = apply_retention_plan(
        plan,
        process_check=process_check,
        codex_executable="/usr/bin/codex",
        expected_codex_version=SAFE_CODEX_VERSION,
    )

    assert deleted == 0
    assert errors == ["shared usage database is missing or unreadable"]
    assert not checked
    assert not db_path.exists()
    assert all(path.is_file() for path in paths)

    monkeypatch.setattr(codex_retention, "find_active_codex_processes", process_check)
    exit_code = codex_retention.main(["--apply", "--codex-dir", str(codex_root)])
    assert exit_code == 2
    assert not checked
    assert all(path.is_file() for path in paths)


def test_legacy_database_path_is_refused_via_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path
    legacy_path = Path.home() / LEGACY_DB_RELATIVE_PATH
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(legacy_path))

    plan = build_retention_plan(codex_dir=Path.home() / ".codex")

    assert plan.trees == []
    assert plan.error == "usage database resolves to the frozen legacy path"
    exit_code = codex_retention.main(["--apply", "--codex-dir", str(Path.home() / ".codex")])
    assert exit_code == 2
    assert not legacy_path.exists()


def test_legacy_database_symlink_alias_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_path = Path.home() / LEGACY_DB_RELATIVE_PATH
    alias = tmp_path / "usage-db-alias"
    alias.symlink_to(legacy_path)
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(alias))

    plan = build_retention_plan(codex_dir=Path.home() / ".codex")

    assert plan.trees == []
    assert plan.error == "usage database resolves to the frozen legacy path"


def test_database_without_codex_rows_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "usage.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, provider TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sessions (session_id, provider) VALUES ('agy:one', 'antigravity')"
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(db_path))

    plan = build_retention_plan(codex_dir=Path.home() / ".codex")

    assert plan.trees == []
    assert plan.error == "shared usage database has no provider-tagged Codex rows"


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


def test_apply_uses_codex_delete_and_keeps_sqlite_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(db_path))
    paths, _ = _make_capture(codex_root, db_path, now=now)
    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root)
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
    target_payload = paths[1].read_text(encoding="utf-8")
    source.unlink()
    source.symlink_to(paths[1])

    plan = build_retention_plan(days=15, now=now, codex_dir=codex_root, db_path=db_path)

    assert plan.trees == []
    assert plan.skipped["transcript_not_exactly_captured"] == 1
    assert paths[1].read_text(encoding="utf-8") == target_payload


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


def _capture_raw_records(tmp_path: Path, records: list[dict]) -> tuple[Path, Path, Path, datetime]:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    root = tmp_path / "codex"
    root.mkdir()
    db = tmp_path / "usage.db"
    paths, activity = _make_capture(root, db, now=now, capture_child=False, root_fragments=1)
    path = paths[0]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    old_ns = int(activity.timestamp() * 1_000_000_000)
    os.utime(path, ns=(old_ns, old_ns))
    captured = extract_codex_session_for_capture(path, _ROOT_ID, codex_dir=root)
    assert captured is not None
    write_usage_sessions("codex", [captured], db_path=db)
    return root, db, path, now


def _raw_token(response: str, **counters: object) -> dict:
    usage = {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 4,
             "reasoning_output_tokens": 1, "total_tokens": 14}
    return {"timestamp": "2026-09-12T12:00:00Z", "type": "token_usage_record",
            "payload": {"thread_id": _ROOT_ID, "response_id": response,
                        "usage": usage, **counters}}


@pytest.mark.parametrize("variant", ["stale_thread", "unscoped_turn", "mixed", "decreasing", "unreconciled"])
def test_independent_verification_keeps_ambiguous_usage(tmp_path: Path, variant: str) -> None:
    usage = _raw_token("one")["payload"]["usage"]
    doubled = {name: value * 2 for name, value in usage.items()}
    variants = {
        "stale_thread": [_raw_token("one", thread_token_usage=usage), _raw_token("two")],
        "unscoped_turn": [_raw_token("one", turn_token_usage=usage), _raw_token("two", turn_token_usage=usage)],
        "mixed": [_raw_token("one", thread_token_usage=usage), _raw_token("two", turn_token_usage=usage)],
        "decreasing": [_raw_token("one", thread_token_usage=doubled), _raw_token("two", thread_token_usage=usage)],
        "unreconciled": [_raw_token("one", thread_token_usage=doubled)],
    }
    root, db, path, now = _capture_raw_records(tmp_path, variants[variant])
    if variant in {"stale_thread", "unscoped_turn"}:
        # Both identities represent real usage, even though the scoped
        # counters are incomplete. Retention must still reject that ambiguity.
        assert read_usage_sessions("codex", db_path=db)[0].usage.total_tokens == 28
    plan = build_retention_plan(now=now, codex_dir=root, db_path=db)
    assert plan.trees == []
    assert plan.skipped["usage_ambiguous"] >= 1
    assert path.exists()


def test_apply_pins_verified_home_with_same_uuid_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    root = tmp_path / "verified"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    db = tmp_path / "usage.db"
    paths, _ = _make_capture(root, db, now=now)
    other_paths, _ = _make_capture(other, tmp_path / "other.db", now=now)
    monkeypatch.setenv("CODEX_HOME", str(other))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(other))
    plan = build_retention_plan(now=now, codex_dir=root, db_path=db)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout=f"codex-cli {SAFE_CODEX_VERSION}", stderr="")
        child_env = kwargs.get("env", os.environ)
        assert child_env["CODEX_HOME"] == str(root.resolve())
        assert child_env["CODEX_SQLITE_HOME"] == str(root.resolve())
        for path in paths:
            path.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    deleted, errors = apply_retention_plan(plan, codex_dir=root, db_path=db, process_check=lambda: [],
                                         run=run, codex_executable="codex", expected_codex_version=SAFE_CODEX_VERSION,
                                         now=lambda: now)
    assert (deleted, errors) == (1, [])
    assert all(path.exists() for path in other_paths)


def test_apply_requires_backup_before_first_delete(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    root = tmp_path / "codex"
    root.mkdir()
    db = tmp_path / "usage.db"
    paths, _ = _make_capture(root, db, now=now)
    plan = build_retention_plan(now=now, codex_dir=root, db_path=db)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout=f"codex-cli {SAFE_CODEX_VERSION}", stderr="")
        backups = list((db.parent / "backups").glob("usage-*.db"))
        assert len(backups) == 1
        assert len(read_usage_sessions("codex", db_path=backups[0])) == 2
        for path in paths:
            path.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    assert apply_retention_plan(plan, codex_dir=root, db_path=db, process_check=lambda: [], run=run,
                                codex_executable="codex", expected_codex_version=SAFE_CODEX_VERSION,
                                now=lambda: now) == (1, [])


@pytest.mark.parametrize("scope", ["thread", "turn"])
def test_independent_verification_distinguishes_valid_scoped_totals(tmp_path: Path, scope: str) -> None:
    usage = _raw_token("one")["payload"]["usage"]
    doubled = {name: value * 2 for name, value in usage.items()}
    if scope == "thread":
        records = [_raw_token("one", thread_token_usage=usage), _raw_token("two", thread_token_usage=doubled)]
    else:
        records = [_raw_token("one", turn_id="turn-one", turn_token_usage=usage),
                   _raw_token("two", turn_id="turn-two", turn_token_usage=usage)]
    root, db, path, now = _capture_raw_records(tmp_path, records)
    assert codex_retention._independent_rollout_usage(path, _ROOT_ID)["total_tokens"] == 28
    captured = read_usage_sessions("codex", db_path=db)[0]
    assert captured.usage.total_tokens == 28
    plan = build_retention_plan(now=now, codex_dir=root, db_path=db)
    assert len(plan.trees) == 1

    # Deliberately persist only the first response under the same source
    # revision. Independent verification must catch an under-captured DB for
    # either scope, without relying on a bug in the production parser.
    captured.events = captured.events[:1]
    captured.usage = captured.events[0].usage
    captured.call_count = 1
    assert write_usage_sessions("codex", [captured], db_path=db) == 1
    assert read_usage_sessions("codex", db_path=db)[0].usage.total_tokens == 14
    plan = build_retention_plan(now=now, codex_dir=root, db_path=db)
    assert plan.trees == []
    assert plan.skipped["sqlite_usage_below_rollout_totals"] == 1
    assert path.exists()


@pytest.mark.parametrize("variant", ["valid", "decrease", "mismatch", "dual_mismatch", "conflicting_id"])
def test_independent_verification_legacy_and_conflicting_streams(tmp_path: Path, variant: str) -> None:
    usage = _raw_token("one")["payload"]["usage"]
    doubled = {name: value * 2 for name, value in usage.items()}
    def legacy(last: dict, total: dict) -> dict:
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": last, "total_token_usage": total}}}
    records = [legacy(usage, usage), legacy(usage, doubled)]
    if variant == "decrease":
        records.append(legacy(usage, usage))
    elif variant == "mismatch":
        records = [legacy(usage, doubled)]
    elif variant == "dual_mismatch":
        records.append(_raw_token("one"))
    elif variant == "conflicting_id":
        records = [_raw_token("one"), _raw_token("one", usage=doubled)]
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    if variant == "valid":
        assert codex_retention._independent_rollout_usage(path, _ROOT_ID)["total_tokens"] == 28
    else:
        with pytest.raises(codex_retention.UsageAmbiguous):
            codex_retention._independent_rollout_usage(path, _ROOT_ID)


def test_backup_failure_blocks_all_deletion_and_reports_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    root = tmp_path / "codex"
    root.mkdir()
    db = tmp_path / "usage.db"
    paths, _ = _make_capture(root, db, now=now)
    plan = build_retention_plan(now=now, codex_dir=root, db_path=db)
    calls = []
    def fail_backup(_db: Path) -> None:
        raise codex_retention.BackupError("synthetic backup failure")
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert "delete" not in command
        return subprocess.CompletedProcess(command, 0, stdout=f"codex-cli {SAFE_CODEX_VERSION}", stderr="")
    monkeypatch.setattr(codex_retention, "backup_usage_db", fail_backup)
    original_apply = apply_retention_plan
    def apply(plan: codex_retention.RetentionPlan, **kwargs: object) -> tuple[int, list[str]]:
        return original_apply(plan, **kwargs, process_check=lambda: [], run=run, now=lambda: now)
    monkeypatch.setattr(codex_retention, "apply_retention_plan", apply)
    monkeypatch.setattr(codex_retention, "find_active_codex_processes", lambda: [])
    monkeypatch.setattr(codex_retention, "build_retention_plan", lambda **kwargs: plan)
    assert codex_retention.main(["--apply", "--json", "--codex-dir", str(root), "--db", str(db),
                                 "--codex-cli", "codex", "--codex-version", SAFE_CODEX_VERSION]) == 1
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["status"] == "backup_failed"
    assert result["deleted_trees"] == 0
    assert all(path.exists() for path in paths)
    assert len(calls) == 1


def test_empty_or_changed_plan_does_not_take_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    root = tmp_path / "codex"
    root.mkdir()
    db = tmp_path / "usage.db"
    paths, _ = _make_capture(root, db, now=now)
    plan = build_retention_plan(now=now, codex_dir=root, db_path=db)
    monkeypatch.setattr(codex_retention, "backup_usage_db", lambda *_args: pytest.fail("unexpected backup"))
    empty = codex_retention.RetentionPlan(cutoff=plan.cutoff)
    assert apply_retention_plan(empty, db_path=db) == (0, [])
    paths[0].write_text("changed")
    run = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=f"codex-cli {SAFE_CODEX_VERSION}", stderr="")
    deleted, errors = apply_retention_plan(plan, codex_dir=root, db_path=db, process_check=lambda: [], run=run,
                                          codex_executable="codex", expected_codex_version=SAFE_CODEX_VERSION, now=lambda: now)
    assert deleted == 0 and errors


def test_apply_rejects_outside_home_even_if_fresh_plan_returns_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    root = tmp_path / "codex"
    root.mkdir()
    db = tmp_path / "usage.db"
    paths, _ = _make_capture(root, db, now=now)
    plan = build_retention_plan(now=now, codex_dir=root, db_path=db)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("keep")
    tree = replace(plan.trees[0], sources=(replace(plan.trees[0].sources[0], path=outside),))
    monkeypatch.setattr(codex_retention, "_tree_still_eligible", lambda *_args, **kwargs: tree)
    monkeypatch.setattr(codex_retention, "backup_usage_db", lambda *_args: pytest.fail("unexpected backup"))
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "delete" not in command
        return subprocess.CompletedProcess(command, 0, stdout=f"codex-cli {SAFE_CODEX_VERSION}", stderr="")
    deleted, errors = apply_retention_plan(plan, codex_dir=root, db_path=db, process_check=lambda: [], run=run,
                                          codex_executable="codex", expected_codex_version=SAFE_CODEX_VERSION, now=lambda: now)
    assert deleted == 0 and "outside" in errors[0]
    assert outside.exists() and all(path.exists() for path in paths)


def _u(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
            "output_tokens": out, "reasoning_output_tokens": 0, "total_tokens": inp + out}


def _tc(total: dict | None, last: dict | None) -> dict:
    info = None if total is None else {"total_token_usage": total, "last_token_usage": last}
    return {"type": "event_msg", "payload": {"type": "token_count", "info": info}}


def _verify(tmp_path: Path, records: list[dict]) -> dict:
    from scripts.codex_retention import _independent_rollout_usage

    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return _independent_rollout_usage(path, "t1")


def test_real_pattern_records_carry_both_thread_and_turn_totals(tmp_path: Path) -> None:
    def rec(rid: str, usage: dict, cumulative: dict) -> dict:
        return {"type": "token_usage_record", "payload": {
            "thread_id": "t1", "turn_id": "turn1", "response_id": rid, "usage": usage,
            "thread_token_usage": cumulative, "turn_token_usage": cumulative}}

    totals = _verify(tmp_path, [rec("a", _u(10, 1), _u(10, 1)), rec("b", _u(20, 2), _u(30, 3))])
    assert totals["total_tokens"] == 33
    with pytest.raises(ValueError):  # a total below the summed responses is still rejected
        _verify(tmp_path, [rec("a", _u(10, 1), _u(10, 1)), rec("b", _u(20, 2), _u(20, 2))])


def test_real_pattern_repeated_token_count_and_null_info_counted_once(tmp_path: Path) -> None:
    zeroed_last = _u(0, 0) | {"total_tokens": 7}
    totals = _verify(tmp_path, [
        _tc(_u(10, 1), _u(10, 1)),
        _tc(None, None),
        _tc(_u(10, 1), _u(10, 1)),
        _tc(_u(10, 1), zeroed_last),
        _tc(_u(25, 3), _u(15, 2)),
    ])
    assert totals["total_tokens"] == 28


def test_real_pattern_modern_responses_may_exceed_legacy_events_only(tmp_path: Path) -> None:
    def rec(rid: str, usage: dict) -> dict:
        return {"type": "token_usage_record", "payload": {"thread_id": "t1", "response_id": rid, "usage": usage}}

    totals = _verify(tmp_path, [rec("a", _u(10, 1)), rec("b", _u(5, 1)), _tc(_u(10, 1), _u(10, 1))])
    assert totals["total_tokens"] == 17
    with pytest.raises(ValueError):  # legacy events exceed responses: unexplained
        _verify(tmp_path, [rec("a", _u(10, 1)), _tc(_u(10, 1), _u(10, 1)), _tc(_u(20, 2), _u(10, 1))])


def test_genuine_legacy_contradictions_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):  # cumulative above summed last usage (forked/gap)
        _verify(tmp_path, [_tc(_u(10, 1), _u(10, 1)), _tc(_u(50, 5), _u(10, 1))])
    with pytest.raises(ValueError):  # counter decreases
        _verify(tmp_path, [_tc(_u(20, 2), _u(20, 2)), _tc(_u(10, 1), _u(0, 0))])
