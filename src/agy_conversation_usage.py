"""Read real per-call token usage from an agy conversation SQLite database.

agy (Google Antigravity CLI) keeps one SQLite database per conversation under
``<agy_dir>/conversations/<conversationId>.db``. ``gen_metadata`` holds one
protobuf row per model call and ``steps`` holds the conversation steps. The
schema is undocumented and was reverse engineered, so every field is validated
and anything unexpected degrades to "no data" instead of wrong numbers.

Field map (gen_metadata.data):
  1      message M
  1.4      usage message U
  1.4.2      uncached input tokens (input minus cache read)
  1.4.3      total output tokens, thinking included (== 1.4.9 + 1.4.10)
  1.4.5      cache-read tokens (absent means 0)
  1.4.9      thinking/reasoning output tokens
  1.19     per-call model name
  2      step indexes the call belongs to (packed varints or repeated varint)

steps.metadata (step_type 15, planner responses): 1.1 epoch seconds and 1.2
nanoseconds give the call timestamp.

Scalar and message fields follow protobuf semantics for duplicates: the last
occurrence wins (messages are not merged); step refs accumulate across
occurrences. A field with the wrong wire type is a decode failure.

The reader opens the database with SQLite's read-only modes. It never modifies
the database or its WAL, never copies or deletes files, and never creates a
``-shm``: a live WAL (non-empty ``-wal``) is read through agy's existing
``-shm`` (SQLite may update that shared-memory index, as for any WAL reader),
and when that file is missing the database is not opened at all. It returns
numbers, a validated model id and timestamps only, never conversation text.

If more than 5% of the non-empty ``gen_metadata`` rows fail to decode the
database is treated as an unrecognised schema (``None``), so partial drift never
yields confident but low numbers.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

# step_index for calls that cannot be paired with a planner step; real step
# indexes are far below this, so the fallback never collides with them.
UNPAIRED_STEP_BASE = 10_000_000
MAX_TOKEN_COUNT = 10_000_000_000
_MAX_VARINT_BYTES = 10
_BUSY_TIMEOUT_SECONDS = 0.25
_RETRY_DELAY_SECONDS = 0.05
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SQLITE_BUSY, _SQLITE_LOCKED = 5, 6

Field = tuple[int, int, int | bytes]


class ProtoError(ValueError):
    """Raised when a buffer is not a well-formed protobuf message."""


@dataclass(frozen=True)
class AgyCall:
    """One model call. ``timestamp`` is timezone-aware UTC or ``None``."""

    step_index: int
    gen_index: int
    timestamp: datetime | None
    model: str | None
    uncached_input: int
    cache_read: int
    output: int
    reasoning: int


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for count in range(_MAX_VARINT_BYTES):
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << (7 * count)
        if not byte & 0x80:
            return result, pos
    raise ProtoError("over-long varint")


def parse_fields(buf: bytes) -> list[Field]:
    """Return ``(field_number, wire_type, value)`` for each top-level field.

    Length-delimited values are returned as raw bytes; nothing is decoded
    recursively. Only wire types 0, 1, 2 and 5 are accepted.
    """
    fields: list[Field] = []
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos)
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ProtoError("field number 0")
        value: int | bytes
        if wire_type == 0:
            value, pos = _read_varint(buf, pos)
        elif wire_type in (1, 5):
            size = 8 if wire_type == 1 else 4
            if pos + size > end:
                raise ProtoError("truncated fixed-width field")
            value = int.from_bytes(buf[pos:pos + size], "little")
            pos += size
        elif wire_type == 2:
            size, pos = _read_varint(buf, pos)
            if size > end - pos:
                raise ProtoError("truncated length-delimited field")
            value = bytes(buf[pos:pos + size])
            pos += size
        else:
            raise ProtoError(f"unsupported wire type {wire_type}")
        fields.append((number, wire_type, value))
    return fields


def _last(fields: list[Field], number: int, wire_type: int) -> int | bytes | None:
    """Last value of a field (protobuf: the last duplicate wins), ``None`` if absent.

    ``ProtoError`` if any occurrence has the wrong wire type.
    """
    found: int | bytes | None = None
    for field_number, actual, value in fields:
        if field_number == number:
            if actual != wire_type:
                raise ProtoError(f"field {number} has wire type {actual}, expected {wire_type}")
            found = value
    return found


def _message(fields: list[Field], number: int) -> bytes | None:
    value = _last(fields, number, 2)
    return value if isinstance(value, bytes) else None


def _varint(fields: list[Field], number: int) -> int | None:
    value = _last(fields, number, 0)
    return value if isinstance(value, int) else None


def _count(fields: list[Field], number: int) -> int:
    value = _varint(fields, number) or 0
    if value > MAX_TOKEN_COUNT:
        raise ProtoError(f"field {number} is implausibly large")
    return value


def _step_refs(fields: list[Field]) -> list[int]:
    """Collect field 2, accepting both packed and repeated varint encodings."""
    refs: list[int] = []
    for number, wire_type, value in fields:
        if number != 2:
            continue
        if isinstance(value, int) and wire_type == 0:
            refs.append(value)
        elif isinstance(value, bytes) and wire_type == 2:
            pos = 0
            while pos < len(value):
                ref, pos = _read_varint(value, pos)
                refs.append(ref)
        else:
            raise ProtoError("step refs are neither packed nor varint")
    return refs


def _model_name(message: list[Field]) -> str | None:
    try:
        raw = _message(message, 19)
        name = raw.decode("utf-8") if raw is not None else None
    except (ProtoError, UnicodeDecodeError):
        return None
    return name if name is not None and _MODEL_PATTERN.fullmatch(name) else None


def _step_timestamp(metadata: object) -> datetime | None:
    """Decode a planner step's 1.1 seconds + 1.2 nanoseconds, or ``None``."""
    if not isinstance(metadata, bytes) or not metadata:
        return None
    try:
        inner = _message(parse_fields(metadata), 1)
        if inner is None:
            return None
        fields = parse_fields(inner)
        seconds, nanos = _varint(fields, 1), _varint(fields, 2) or 0
        if seconds is None or nanos >= 1_000_000_000:
            return None
        return _EPOCH + timedelta(seconds=seconds, microseconds=nanos // 1000)
    except (ProtoError, OverflowError):
        return None


def _decode_call(gen_index: int, data: bytes) -> tuple[AgyCall | None, list[int]]:
    """Decode one gen_metadata row to ``(call, step_refs)``; ``call`` is None for zero usage.

    The returned call carries a placeholder ``step_index``; the caller pairs it.
    """
    top = parse_fields(data)
    try:
        refs = _step_refs(top)
    except ProtoError:
        refs = []  # malformed refs cost the pairing, never the call's usage
    message_bytes = _message(top, 1)
    message = parse_fields(message_bytes) if message_bytes is not None else []
    usage_bytes = _message(message, 4)
    if usage_bytes is None:
        return None, refs
    usage = parse_fields(usage_bytes)
    uncached, cache_read, output = _count(usage, 2), _count(usage, 5), _count(usage, 3)
    reasoning = min(_count(usage, 9), output)
    if uncached + cache_read + output == 0:
        return None, refs
    call = AgyCall(
        step_index=-1, gen_index=gen_index, timestamp=None, model=_model_name(message),
        uncached_input=uncached, cache_read=cache_read, output=output, reasoning=reasoning,
    )
    return call, refs


def _connect(uri: str) -> sqlite3.Connection:
    return sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_SECONDS)


def _database_uri(path: Path) -> str | None:
    """Return the read-only URI for ``path``, or ``None`` if it cannot be opened without side files."""
    quoted = quote(str(path), safe="/")
    try:
        live_wal = os.stat(f"{path}-wal").st_size > 0
    except FileNotFoundError:
        live_wal = False
    # Without a live WAL, immutable stops SQLite creating -wal/-shm files. With
    # one, the data only exists through SQLite's WAL reader, so open read-only,
    # but only if agy's -shm exists: a read-only open would create it otherwise.
    if not live_wal:
        return f"file:{quoted}?immutable=1"
    return f"file:{quoted}?mode=ro" if os.path.exists(f"{path}-shm") else None


def _read_once(path: Path) -> list[AgyCall] | None:
    uri = _database_uri(path)
    if uri is None:
        return None
    connection = _connect(uri)
    try:
        # One read snapshot so gen_metadata rows and steps stay consistent.
        connection.execute("BEGIN")
        planner_steps: dict[int, object] = {}
        for idx, metadata in connection.execute("SELECT idx, metadata FROM steps WHERE step_type = 15"):
            if isinstance(idx, int):
                planner_steps[idx] = metadata
        calls: list[AgyCall] = []
        used_steps: set[int] = set()
        non_empty = failed = 0
        for gen_index, data in connection.execute("SELECT idx, data FROM gen_metadata ORDER BY idx"):
            if data is None or data == b"":
                continue
            non_empty += 1
            if not isinstance(gen_index, int) or not isinstance(data, bytes):
                failed += 1
                continue
            try:
                call, refs = _decode_call(gen_index, data)
            except ProtoError:
                failed += 1
                continue
            if call is None:
                continue
            paired = min((ref for ref in refs if ref in planner_steps), default=None)
            if paired is None or paired in used_steps:
                step_index = UNPAIRED_STEP_BASE + gen_index
            else:
                step_index = paired
            used_steps.add(step_index)
            timestamp = _step_timestamp(planner_steps[paired]) if paired is not None else None
            calls.append(replace(call, step_index=step_index, timestamp=timestamp))
        # More than 5% undecodable rows is drift, not noise: fail closed.
        if failed * 20 > non_empty:
            logger.warning(
                "agy conversation schema not recognised: %d of %d rows failed to decode", failed, non_empty,
            )
            return None
        if failed:
            logger.warning("agy conversation: dropped %d of %d rows that failed to decode", failed, non_empty)
        return calls
    finally:
        connection.close()


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (_SQLITE_BUSY, _SQLITE_LOCKED)
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def read_conversation_calls(db_path: Path) -> list[AgyCall] | None:
    """Return the non-zero model calls of one conversation database, by gen_index.

    ``None`` means the file is missing, unreadable, has an unrecognised schema
    (missing tables or columns, or more than 5% of the non-empty rows fail to
    decode), or has a live WAL without its ``-shm`` (not opened, so nothing is
    created); an empty list means it is readable but holds no non-zero calls.
    Up to 5% undecodable rows are dropped with a WARNING log of the counts.
    """
    for attempt in (1, 2):
        try:
            path = Path(db_path).absolute()
            if not path.is_file():
                return None
            return _read_once(path)
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                return None
        except sqlite3.DatabaseError:
            pass  # torn read, e.g. a checkpoint ran mid-read
        except (sqlite3.Error, OSError):
            return None
        if attempt == 1:
            time.sleep(_RETRY_DELAY_SECONDS)
    return None
