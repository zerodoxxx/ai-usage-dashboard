"""Helpers for building synthetic agy conversation databases in tests.

Everything here is synthetic: a tiny protobuf encoder plus the real agy table
layout (schema only, copied from a conversation database; no data).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

# agy's real conversation schema (``.schema`` of a conversation database).
AGY_SCHEMA = """
CREATE TABLE `trajectory_meta` (`trajectory_id` text,`cascade_id` text,`trajectory_type` integer,`source` integer,PRIMARY KEY (`trajectory_id`));
CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL DEFAULT false,`metadata` blob,`error_details` blob,`permissions` blob,`task_details` blob,`render_info` blob,`step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));
CREATE INDEX `idx_steps_status` ON `steps`(`status`);
CREATE INDEX `idx_steps_step_type` ON `steps`(`step_type`);
CREATE TABLE `gen_metadata` (`idx` integer,`data` blob,`size` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));
CREATE TABLE `executor_metadata` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
CREATE TABLE `parent_references` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
CREATE TABLE `trajectory_metadata_blob` (`id` text DEFAULT "main",`data` blob,PRIMARY KEY (`id`));
CREATE TABLE `battle_mode_infos` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
"""

PLANNER = 15


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(number: int, wire_type: int) -> bytes:
    return varint((number << 3) | wire_type)


def field_varint(number: int, value: int) -> bytes:
    return _tag(number, 0) + varint(value)


def field_bytes(number: int, payload: bytes) -> bytes:
    return _tag(number, 2) + varint(len(payload)) + payload


def field_fixed64(number: int, value: int) -> bytes:
    return _tag(number, 1) + value.to_bytes(8, "little")


def field_fixed32(number: int, value: int) -> bytes:
    return _tag(number, 5) + value.to_bytes(4, "little")


def usage_message(
    *, uncached: int | None = None, output: int | None = None, cache_read: int | None = None,
    reasoning: int | None = None, visible: int | None = None,
) -> bytes:
    """Usage message U; ``None`` leaves a field absent."""
    parts = ((2, uncached), (3, output), (5, cache_read), (9, reasoning), (10, visible))
    return b"".join(field_varint(number, value) for number, value in parts if value is not None)


def gen_row(
    usage: bytes | None, *, model: str | bytes | None = None, steps: Sequence[int] = (), packed: bool = True,
) -> bytes:
    """A ``gen_metadata.data`` blob: M (usage + model) plus the step refs."""
    message = b"" if usage is None else field_bytes(4, usage)
    if model is not None:
        message += field_bytes(19, model.encode() if isinstance(model, str) else model)
    row = field_bytes(1, message)
    if steps and packed:
        row += field_bytes(2, b"".join(varint(step) for step in steps))
    elif steps:
        row += b"".join(field_varint(2, step) for step in steps)
    return row


def planner_metadata(seconds: int, nanos: int | None = 0, extra: bytes = b"") -> bytes:
    """A ``steps.metadata`` blob with the call timestamp in 1.1 / 1.2."""
    stamp = field_varint(1, seconds) + (b"" if nanos is None else field_varint(2, nanos))
    return field_bytes(1, stamp) + extra


GenRow = tuple[int, bytes | None]
StepRow = tuple[int, int, bytes | None]


def _insert(connection: sqlite3.Connection, gen_rows: Iterable[GenRow], steps: Iterable[StepRow]) -> None:
    connection.executemany(
        "INSERT INTO gen_metadata(idx, data, size) VALUES (?, ?, ?)",
        [(idx, data, len(data or b"")) for idx, data in gen_rows],
    )
    connection.executemany(
        "INSERT INTO steps(idx, step_type, metadata) VALUES (?, ?, ?)", list(steps)
    )
    connection.commit()


def create_conversation_db(
    path: Path, gen_rows: Iterable[GenRow] = (), steps: Iterable[StepRow] = (), *, wal: bool = False,
) -> Path:
    """Create a conversation database; ``wal=True`` leaves a WAL-mode file with no side files."""
    connection = sqlite3.connect(path)
    try:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(AGY_SCHEMA)
        _insert(connection, gen_rows, steps)
    finally:
        connection.close()
    return path


def open_live_wal_writer(
    path: Path, gen_rows: Iterable[GenRow] = (), steps: Iterable[StepRow] = (),
) -> sqlite3.Connection:
    """Commit rows that stay only in the WAL; the caller closes the returned connection.

    The writer keeps the database open with auto-checkpointing disabled, which
    is what a running agy looks like to the reader.
    """
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    _insert(connection, gen_rows, steps)
    return connection


def write_conversation(
    base: Path, session_id: str, calls: Sequence[dict], *, start: int = 1_790_000_000,
) -> Path:
    """Create ``<base>/conversations/<session_id>.db`` with one planner step per call.

    Each call is a dict with ``uncached`` and ``output`` plus optional
    ``cache_read``, ``reasoning`` and ``model``. Call ``i`` is paired with
    planner step ``2 * i + 1`` and stamped ``start + 60 * i`` seconds. An
    existing database for the session is replaced.
    """
    path = base / "conversations" / f"{session_id}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)  # rewriting the same conversation replaces it
    gen_rows, steps = [], []
    for index, call in enumerate(calls):
        usage = usage_message(
            uncached=call.get("uncached", 0), output=call.get("output", 0),
            cache_read=call.get("cache_read"), reasoning=call.get("reasoning"),
        )
        step = 2 * index + 1
        gen_rows.append((index, gen_row(usage, model=call.get("model"), steps=[step])))
        steps.append((step, PLANNER, planner_metadata(start + 60 * index)))
    return create_conversation_db(path, gen_rows, steps)
