"""agy conversation database reader tests, synthetic fixtures only."""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import agy_conversation_usage as reader
from src.agy_conversation_usage import AgyCall, ProtoError, parse_fields, read_conversation_calls
from tests_agy_support import (
    PLANNER, create_conversation_db, field_bytes, field_fixed32, field_fixed64, field_varint,
    gen_row, open_live_wal_writer, planner_metadata, usage_message, varint,
)

UNPAIRED = reader.UNPAIRED_STEP_BASE
SECONDS = 1_790_000_000


def _utc(seconds: int, microsecond: int = 0) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=microsecond)


def _row(idx, steps=(), *, uncached=10, output=5, **kwargs):
    """A (gen idx, blob) pair with a simple non-zero usage."""
    return idx, gen_row(usage_message(uncached=uncached, output=output, **kwargs), steps=steps)


def _planner(idx, seconds=SECONDS, nanos=0):
    return idx, PLANNER, planner_metadata(seconds, nanos)


@pytest.fixture
def folder(tmp_path):
    """A fresh empty directory, so listings are not polluted by the conftest home."""
    path = tmp_path / "conversations"
    path.mkdir()
    return path


def _read(tmp_path, gen_rows, steps=(), **kwargs):
    return read_conversation_calls(create_conversation_db(tmp_path / "conversation.db", gen_rows, steps, **kwargs))


# --- protobuf decoder -------------------------------------------------------

def test_parse_fields_round_trip():
    buf = (
        field_varint(1, 0) + field_varint(2, 127) + field_varint(3, 128) + field_varint(4, 300)
        + field_varint(5, 2**64 - 1) + field_bytes(6, b"") + field_bytes(7, b"\x00\xffabc")
        + field_fixed64(8, 2**63 + 5) + field_fixed32(9, 0xDEADBEEF) + field_varint(1, 9)
        + field_bytes(10, field_varint(1, 42))
    )
    assert parse_fields(buf) == [
        (1, 0, 0), (2, 0, 127), (3, 0, 128), (4, 0, 300), (5, 0, 2**64 - 1), (6, 2, b""),
        (7, 2, b"\x00\xffabc"), (8, 1, 2**63 + 5), (9, 5, 0xDEADBEEF), (1, 0, 9),
        (10, 2, field_varint(1, 42)),
    ]
    assert parse_fields(b"") == []
    assert varint(300) == b"\xac\x02"


@pytest.mark.parametrize("buf", [
    pytest.param(b"\xff" * 20, id="garbage"),
    pytest.param(b"\x08", id="missing-value"),
    pytest.param(b"\x08\x80", id="truncated-varint"),
    pytest.param(b"\x80", id="truncated-tag"),
    pytest.param(b"\x0a\x05ab", id="truncated-length-delimited"),
    pytest.param(b"\x0a", id="missing-length"),
    pytest.param(b"\x09\x01\x02\x03", id="truncated-fixed64"),
    pytest.param(b"\x0d\x01\x02", id="truncated-fixed32"),
    pytest.param(b"\x08" + b"\xff" * 10 + b"\x01", id="overlong-varint"),
    pytest.param(b"\xff" * 10 + b"\x01\x00", id="overlong-tag"),
    pytest.param(field_varint(1, 1) + b"\x0b", id="wire-type-3"),
    pytest.param(b"\x0c", id="wire-type-4"),
    pytest.param(b"\x0e\x00", id="wire-type-6"),
    pytest.param(b"\x0f\x00", id="wire-type-7"),
    pytest.param(b"\x00\x01", id="field-zero"),
    pytest.param(b"\x02\x01a", id="field-zero-length-delimited"),
])
def test_parse_fields_rejects_malformed_input(buf):
    with pytest.raises(ProtoError):
        parse_fields(buf)
    assert issubclass(ProtoError, ValueError)


def test_agy_call_is_frozen():
    call = AgyCall(1, 2, None, None, 3, 4, 5, 6)
    with pytest.raises(FrozenInstanceError):
        call.output = 0  # type: ignore[misc]


# --- decoding and pairing ---------------------------------------------------

def test_decodes_usage_model_and_timestamp(tmp_path):
    usage = usage_message(uncached=100, output=30, cache_read=40, reasoning=10, visible=20)
    # Field 9 of a planner step repeats the usage; the reader must ignore it.
    metadata = planner_metadata(SECONDS, 123_456_789, extra=field_bytes(9, usage))
    calls = _read(
        tmp_path, [(7, gen_row(usage, model="gemini-3.8-flash", steps=[3]))], [(3, PLANNER, metadata)],
    )
    assert calls == [AgyCall(
        step_index=3, gen_index=7, timestamp=_utc(SECONDS, 123_456), model="gemini-3.8-flash",
        uncached_input=100, cache_read=40, output=30, reasoning=10,
    )]
    assert calls[0].timestamp.utcoffset().total_seconds() == 0


def test_calls_are_ordered_by_gen_index_and_paired_by_refs_not_order(tmp_path):
    gen_rows = [_row(9, [20]), _row(2, [30]), _row(5, [10])]
    steps = [_planner(10, SECONDS + 1), _planner(20, SECONDS + 2), _planner(30, SECONDS + 3)]
    calls = _read(tmp_path, gen_rows, steps)
    assert [(c.gen_index, c.step_index, c.timestamp) for c in calls] == [
        (2, 30, _utc(SECONDS + 3)), (5, 10, _utc(SECONDS + 1)), (9, 20, _utc(SECONDS + 2)),
    ]


def test_two_planner_refs_make_one_call_with_the_lowest_ref(tmp_path):
    gen_rows = [_row(1, [7, 3], uncached=60, output=40)]
    steps = [_planner(3, SECONDS), _planner(7, SECONDS + 9)]
    calls = _read(tmp_path, gen_rows, steps)
    assert len(calls) == 1
    assert calls[0].step_index == 3 and calls[0].timestamp == _utc(SECONDS)
    assert (calls[0].uncached_input, calls[0].output) == (60, 40)


def test_non_planner_refs_are_ignored_when_pairing(tmp_path):
    steps = [(1, 5, planner_metadata(SECONDS - 100)), _planner(4, SECONDS), (6, 0, None)]
    calls = _read(tmp_path, [_row(1, [1, 4, 6])], steps)
    assert calls[0].step_index == 4 and calls[0].timestamp == _utc(SECONDS)


def test_repeated_varint_refs_are_accepted_like_packed_refs(tmp_path):
    blob = gen_row(usage_message(uncached=1, output=1), steps=[8, 6], packed=False)
    assert blob != gen_row(usage_message(uncached=1, output=1), steps=[8, 6])
    calls = _read(tmp_path, [(1, blob)], [_planner(6), _planner(8)])
    assert calls[0].step_index == 6


@pytest.mark.parametrize("steps_refs, planner_steps", [
    pytest.param([], [_planner(3)], id="no-refs"),
    pytest.param([99], [_planner(3)], id="ref-to-missing-step"),
    pytest.param([3], [(3, 1, planner_metadata(SECONDS))], id="ref-to-non-planner-step"),
])
def test_unpaired_calls_use_the_fallback_step_index(tmp_path, steps_refs, planner_steps):
    calls = _read(tmp_path, [_row(4, steps_refs)], planner_steps)
    assert [(c.gen_index, c.step_index, c.timestamp) for c in calls] == [(4, UNPAIRED + 4, None)]


def test_step_collision_keeps_the_first_call_and_falls_back_for_the_rest(tmp_path):
    gen_rows = [_row(1, [5]), _row(2, [5]), _row(3, [5, 8])]
    calls = _read(tmp_path, gen_rows, [_planner(5, SECONDS), _planner(8, SECONDS + 5)])
    # Call 3 refs steps 5 and 8; the lowest planner ref (5) is already taken.
    assert [c.step_index for c in calls] == [5, UNPAIRED + 2, UNPAIRED + 3]
    assert len({c.step_index for c in calls}) == 3
    # The timestamp still comes from the referenced step.
    assert [c.timestamp for c in calls] == [_utc(SECONDS)] * 3


@pytest.mark.parametrize("metadata", [
    pytest.param(None, id="null"),
    pytest.param(b"", id="empty"),
    pytest.param(b"\xff\xff", id="garbage"),
    pytest.param(field_bytes(2, b"x"), id="no-field-1"),
    pytest.param(field_varint(1, SECONDS), id="field-1-not-a-message"),
    pytest.param(field_bytes(1, field_varint(2, 5)), id="no-seconds"),
    pytest.param(planner_metadata(SECONDS, 1_000_000_000), id="nanos-out-of-range"),
    pytest.param(planner_metadata(2**62), id="seconds-out-of-range"),
])
def test_unusable_step_metadata_leaves_a_call_without_a_timestamp(tmp_path, metadata):
    calls = _read(tmp_path, [_row(1, [2])], [(2, PLANNER, metadata)])
    assert [(c.step_index, c.timestamp) for c in calls] == [(2, None)]


def test_missing_nanoseconds_default_to_zero(tmp_path):
    calls = _read(tmp_path, [_row(1, [2])], [(2, PLANNER, planner_metadata(SECONDS, None))])
    assert calls[0].timestamp == _utc(SECONDS)


# --- usage semantics --------------------------------------------------------

def test_zero_usage_rows_are_skipped(tmp_path):
    gen_rows = [
        (1, gen_row(usage_message(uncached=0, output=0, cache_read=0, reasoning=0), steps=[1])),
        (2, gen_row(usage_message())),
        (3, gen_row(None)),
        (4, field_bytes(2, varint(1))),
        (5, b""),
        (6, None),
        _row(7),
    ]
    calls = _read(tmp_path, gen_rows, [_planner(1)])
    assert [c.gen_index for c in calls] == [7]


def test_cache_read_alone_is_not_zero_usage(tmp_path):
    calls = _read(tmp_path, [(1, gen_row(usage_message(cache_read=9)))])
    assert [(c.uncached_input, c.cache_read, c.output) for c in calls] == [(0, 9, 0)]


def test_missing_cache_read_is_zero(tmp_path):
    calls = _read(tmp_path, [(1, gen_row(usage_message(uncached=12, output=3)))])
    assert (calls[0].uncached_input, calls[0].cache_read, calls[0].output) == (12, 0, 3)


def test_missing_model_is_none(tmp_path):
    assert _read(tmp_path, [_row(1)])[0].model is None


@pytest.mark.parametrize("model", [
    "gemini-3.8-flash", "claude-opus-4-6-thinking", "models/gemini-2.5:pro", "a", "x" * 80,
])
def test_valid_model_ids_are_kept(tmp_path, model):
    blob = gen_row(usage_message(uncached=1), model=model)
    assert _read(tmp_path, [(1, blob)])[0].model == model


@pytest.mark.parametrize("model", [
    "", "-leading-dash", ".dot", "has space", "line\n", "gemini\n", "x" * 81, "naïve", "tab\tbed",
    b"\xff\xfe", b"gemini\x00",
])
def test_bad_model_strings_become_none(tmp_path, model):
    blob = gen_row(usage_message(uncached=1), model=model)
    assert _read(tmp_path, [(1, blob)])[0].model is None


def test_model_field_with_the_wrong_wire_type_is_none(tmp_path):
    message = field_bytes(4, usage_message(uncached=1)) + field_varint(19, 7)
    calls = _read(tmp_path, [(1, field_bytes(1, message))])
    assert calls[0].model is None and calls[0].uncached_input == 1


def test_reasoning_is_clamped_to_output(tmp_path):
    calls = _read(tmp_path, [_row(1, output=30, reasoning=50), _row(2, output=30, reasoning=10), _row(3, output=30)])
    assert [(c.output, c.reasoning) for c in calls] == [(30, 30), (30, 10), (30, 0)]


def test_output_is_total_output_not_visible_output(tmp_path):
    calls = _read(tmp_path, [_row(1, output=30, reasoning=10, visible=20)])
    assert calls[0].output == 30


# --- validation and schema drift -------------------------------------------

def _with_bad_row(bad, good):
    """Gen row 1 holds ``bad``; rows 2.. are ``good`` decodable calls."""
    return [(1, bad)] + [_row(idx) for idx in range(2, 2 + good)]


@pytest.mark.parametrize("bad", [
    pytest.param(b"\xff\xff\xff", id="garbage"),
    pytest.param(field_bytes(1, field_bytes(4, field_bytes(2, b"x"))), id="uncached-not-a-varint"),
    pytest.param(gen_row(usage_message(uncached=reader.MAX_TOKEN_COUNT + 1, output=1)), id="implausible-count"),
    pytest.param(field_varint(1, 1), id="message-not-length-delimited"),
    pytest.param(field_bytes(1, field_bytes(4, field_fixed32(3, 5))), id="output-not-a-varint"),
    pytest.param(field_varint(2, 1) + b"\x0b", id="wire-type-3"),
    pytest.param(
        field_bytes(1, field_bytes(4, field_varint(3, 5) + field_fixed32(3, 5))), id="duplicate-wrong-wire-type",
    ),
])
def test_an_undecodable_row_is_dropped_but_others_survive(tmp_path, bad):
    calls = _read(tmp_path, _with_bad_row(bad, good=20))
    assert [c.gen_index for c in calls] == list(range(2, 22))


def test_the_token_count_limit_itself_is_accepted(tmp_path):
    calls = _read(tmp_path, [(1, gen_row(usage_message(uncached=reader.MAX_TOKEN_COUNT, output=1)))])
    assert [c.uncached_input for c in calls] == [reader.MAX_TOKEN_COUNT]


def test_one_failing_row_in_ten_is_schema_drift(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=reader.logger.name)
    assert _read(tmp_path, _with_bad_row(b"\xff\xff\xff", good=9)) is None
    assert [r.getMessage() for r in caplog.records] == [
        "agy conversation schema not recognised: 1 of 10 rows failed to decode",
    ]


def test_one_failing_row_in_forty_keeps_the_rest_and_warns(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=reader.logger.name)
    calls = _read(tmp_path, _with_bad_row(b"\xff\xff\xff", good=39))
    assert [c.gen_index for c in calls] == list(range(2, 41))
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "agy conversation: dropped 1 of 40 rows that failed to decode"),
    ]


@pytest.mark.parametrize("failing, good, recognised", [
    (1, 19, True), (1, 18, False), (2, 38, True), (2, 37, False), (3, 57, True), (3, 56, False),
])
def test_five_percent_failing_rows_is_the_limit(tmp_path, failing, good, recognised):
    rows = [(idx, b"\xff\xff\xff") for idx in range(1, failing + 1)]
    rows += [_row(idx) for idx in range(failing + 1, failing + good + 1)]
    calls = _read(tmp_path, rows)
    assert (calls is not None) is recognised
    if recognised:
        assert len(calls) == good


@pytest.mark.parametrize("zero_rows, recognised", [(19, True), (18, False)])
def test_zero_usage_rows_that_decode_are_not_failures(tmp_path, zero_rows, recognised):
    rows = [(1, b"\xff\xff\xff")] + [
        (idx, gen_row(usage_message(uncached=0, output=0))) for idx in range(2, 2 + zero_rows)
    ]
    # The failing row counts against all non-empty rows, zero-usage ones included.
    assert _read(tmp_path, rows) == ([] if recognised else None)


@pytest.mark.parametrize("refs", [
    pytest.param(field_bytes(2, b"\x80"), id="truncated-packed-varint"),
    pytest.param(field_bytes(2, b"\x03" + b"\xff" * 10 + b"\x01"), id="overlong-packed-varint"),
    pytest.param(field_bytes(2, varint(3)) + field_bytes(2, b"\x80"), id="good-then-bad-list"),
    pytest.param(field_fixed32(2, 3), id="fixed-width-refs"),
    pytest.param(field_fixed64(2, 3), id="fixed64-refs"),
])
def test_malformed_step_refs_keep_the_call_without_pairing(tmp_path, refs):
    blob = field_bytes(1, field_bytes(4, usage_message(uncached=11, output=4, reasoning=2))) + refs
    calls = _read(tmp_path, [(5, blob), _row(6, [3])], [_planner(3, SECONDS)])
    # Pairing for the broken row is skipped entirely, so the later row owns step 3.
    assert calls == [
        AgyCall(UNPAIRED + 5, 5, None, None, 11, 0, 4, 2),
        AgyCall(3, 6, _utc(SECONDS), None, 10, 0, 5, 0),
    ]


def test_malformed_step_refs_on_a_zero_usage_row_are_not_a_failure(tmp_path):
    zero = field_bytes(1, field_bytes(4, usage_message(uncached=0))) + field_bytes(2, b"\x80")
    assert _read(tmp_path, [(1, zero)]) == []


def test_dropped_rows_are_logged_as_warnings_with_counts_only(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=reader.logger.name)
    secret = b"super secret conversation text"
    clean = create_conversation_db(tmp_path / "clean.db", [_row(1)])
    partial = create_conversation_db(tmp_path / "partial.db", _with_bad_row(secret, good=39))
    broken = create_conversation_db(tmp_path / "broken.db", [(1, secret), (2, b"\x0b")])

    assert len(read_conversation_calls(clean)) == 1
    assert caplog.records == []

    assert len(read_conversation_calls(partial)) == 39
    assert [(r.levelno, r.name, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, reader.logger.name, "agy conversation: dropped 1 of 40 rows that failed to decode"),
    ]

    caplog.clear()
    assert read_conversation_calls(broken) is None
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "agy conversation schema not recognised: 2 of 2 rows failed to decode"),
    ]
    assert "secret" not in caplog.text


def test_duplicate_scalar_fields_use_the_last_occurrence(tmp_path):
    usage = b"".join(field_varint(number, value) for number, value in (
        (2, 5), (2, 9), (3, 4), (3, 8), (5, 2), (5, 6), (9, 1), (9, 3),
    ))
    calls = _read(tmp_path, [(1, gen_row(usage))])
    assert [(c.uncached_input, c.cache_read, c.output, c.reasoning) for c in calls] == [(9, 6, 8, 3)]


def test_duplicate_messages_are_not_merged_and_the_last_wins(tmp_path):
    first = field_bytes(1, field_bytes(4, usage_message(uncached=5, output=1)) + field_bytes(19, b"first-model"))
    second = field_bytes(1, field_bytes(4, usage_message(uncached=7, output=2)))
    no_usage = field_bytes(1, field_bytes(19, b"second-model"))
    twice_in_message = field_bytes(1, field_bytes(4, usage_message(uncached=1)) + field_bytes(4, usage_message(uncached=3)))
    calls = _read(tmp_path, [(1, first + second), (2, first + no_usage), (3, twice_in_message)])
    # Row 2's last message has no usage and is not merged with the earlier one.
    assert [(c.gen_index, c.uncached_input, c.model) for c in calls] == [(1, 7, None), (3, 3, None)]


def test_duplicate_model_fields_use_the_last_occurrence(tmp_path):
    usage = field_bytes(4, usage_message(uncached=1))
    good_last = field_bytes(1, usage + field_bytes(19, b"first-model") + field_bytes(19, b"last-model"))
    bad_last = field_bytes(1, usage + field_bytes(19, b"first-model") + field_bytes(19, b"bad model"))
    calls = _read(tmp_path, [(1, good_last), (2, bad_last)])
    assert [c.model for c in calls] == ["last-model", None]


def test_duplicate_timestamp_fields_use_the_last_occurrence(tmp_path):
    stamp = b"".join(field_varint(number, value) for number, value in (
        (1, SECONDS), (1, SECONDS + 5), (2, 1000), (2, 3000),
    ))
    calls = _read(tmp_path, [_row(1, [2])], [(2, PLANNER, field_bytes(1, stamp))])
    assert calls[0].timestamp == _utc(SECONDS + 5, 3)


def test_step_refs_accumulate_across_occurrences(tmp_path):
    blob = field_bytes(1, field_bytes(4, usage_message(uncached=1))) + field_bytes(2, varint(8)) + field_bytes(2, varint(6))
    calls = _read(tmp_path, [(1, blob)], [_planner(6), _planner(8)])
    assert calls[0].step_index == 6


@pytest.mark.parametrize("rows", [
    pytest.param([(1, b"\xff\xff\xff")], id="garbage"),
    pytest.param([(1, b"\xff\xff\xff"), (2, b"\x0b"), (3, field_bytes(1, field_bytes(4, b"\x80")))], id="all-garbage"),
    pytest.param([(1, gen_row(usage_message(uncached=reader.MAX_TOKEN_COUNT + 1)))], id="implausible"),
    pytest.param([(1, "text instead of a blob")], id="text-blob"),
])
def test_every_non_empty_row_failing_means_schema_drift(tmp_path, rows):
    assert _read(tmp_path, rows) is None


def test_empty_blobs_do_not_count_as_failures(tmp_path):
    assert _read(tmp_path, [(1, b""), (2, None)]) == []


def test_missing_file_returns_none(folder):
    assert read_conversation_calls(folder / "missing.db") is None
    assert read_conversation_calls(folder) is None
    assert os.listdir(folder) == []


def test_garbage_file_returns_none_and_creates_nothing(folder):
    path = folder / "garbage.db"
    path.write_bytes(b"this is not a sqlite database\n" * 64)
    assert read_conversation_calls(path) is None
    assert os.listdir(folder) == ["garbage.db"]


def test_empty_file_returns_none(folder):
    path = folder / "empty.db"
    path.write_bytes(b"")
    assert read_conversation_calls(path) is None
    assert path.stat().st_size == 0 and os.listdir(folder) == ["empty.db"]


def test_os_errors_return_none_instead_of_raising(folder, monkeypatch):
    # A real OSError: the name exceeds the filesystem's limit.
    assert read_conversation_calls(folder / ("x" * 300)) is None

    path = create_conversation_db(folder / "c.db", [_row(1)])
    assert len(read_conversation_calls(path)) == 1

    def denied(self, *args, **kwargs):
        raise PermissionError("denied")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_file", denied)
        assert read_conversation_calls(path) is None

    real_stat = os.stat

    def stat(target, *args, **kwargs):
        if str(target).endswith("-wal"):
            raise PermissionError("denied")
        return real_stat(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(reader.os, "stat", stat)
        assert read_conversation_calls(path) is None
    assert os.listdir(folder) == ["c.db"]


def test_missing_tables_or_columns_return_none(tmp_path):
    unrelated = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE other(idx integer, data blob)")
    only_gen = tmp_path / "only-gen.db"
    with sqlite3.connect(only_gen) as connection:
        connection.execute("CREATE TABLE gen_metadata(idx integer, data blob)")
    no_data_column = tmp_path / "no-data-column.db"
    with sqlite3.connect(no_data_column) as connection:
        connection.execute("CREATE TABLE gen_metadata(idx integer, payload blob)")
        connection.execute("CREATE TABLE steps(idx integer, step_type integer, metadata blob)")
    no_step_type = tmp_path / "no-step-type.db"
    with sqlite3.connect(no_step_type) as connection:
        connection.execute("CREATE TABLE gen_metadata(idx integer, data blob)")
        connection.execute("CREATE TABLE steps(idx integer, kind integer, metadata blob)")
    for path in (unrelated, only_gen, no_data_column, no_step_type):
        assert read_conversation_calls(path) is None


def test_readable_database_without_calls_returns_an_empty_list(tmp_path):
    assert _read(tmp_path, []) == []


def test_only_zero_rows_return_an_empty_list(tmp_path):
    gen_rows = [(1, gen_row(usage_message(uncached=0, output=0), steps=[1])), (2, gen_row(usage_message()))]
    assert _read(tmp_path, gen_rows, [_planner(1)]) == []


def test_accepts_str_paths(tmp_path):
    path = create_conversation_db(tmp_path / "c.db", [_row(1)])
    assert len(read_conversation_calls(str(path))) == 1  # type: ignore[arg-type]


def test_reader_only_touches_the_documented_columns(tmp_path, monkeypatch):
    touched = set()

    def authorizer(action, table, column, *_):
        if action == sqlite3.SQLITE_READ:
            touched.add((table, column))
        return sqlite3.SQLITE_OK

    real = reader._connect

    def connect(uri):
        connection = real(uri)
        connection.set_authorizer(authorizer)
        return connection

    monkeypatch.setattr(reader, "_connect", connect)
    path = create_conversation_db(tmp_path / "c.db", [_row(1, [2])], [_planner(2)])
    assert len(read_conversation_calls(path)) == 1
    assert touched
    assert touched <= {
        ("gen_metadata", "idx"), ("gen_metadata", "data"),
        ("steps", "idx"), ("steps", "metadata"), ("steps", "step_type"),
    }
    assert ("gen_metadata", "data") in touched and ("steps", "metadata") in touched


# --- read-only access, WAL and retries -------------------------------------

@pytest.fixture
def opened_uris(monkeypatch):
    uris = []
    real = reader._connect

    def connect(uri):
        uris.append(uri)
        return real(uri)

    monkeypatch.setattr(reader, "_connect", connect)
    return uris


def _clean_wal_db(folder):
    return create_conversation_db(
        folder / "wal.db", [_row(1, [2]), _row(3, [4])], [_planner(2), _planner(4)], wal=True,
    )


def test_clean_wal_database_is_read_without_creating_side_files(folder, opened_uris):
    path = _clean_wal_db(folder)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert os.listdir(folder) == ["wal.db"]
    before = path.stat()

    calls = read_conversation_calls(path)

    assert [c.step_index for c in calls] == [2, 4]
    assert os.listdir(folder) == ["wal.db"]
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert len(opened_uris) == 1 and opened_uris[0].endswith("?immutable=1")


def test_zero_byte_wal_uses_the_immutable_branch(folder, opened_uris):
    path = _clean_wal_db(folder)
    wal = Path(f"{path}-wal")
    wal.write_bytes(b"")
    assert len(read_conversation_calls(path)) == 2
    assert sorted(os.listdir(folder)) == ["wal.db", "wal.db-wal"]
    assert wal.stat().st_size == 0
    assert opened_uris[0].endswith("?immutable=1")


def test_live_uncheckpointed_wal_is_read_completely(tmp_path, opened_uris):
    path = create_conversation_db(tmp_path / "live.db", [_row(1, [2])], [_planner(2)], wal=True)
    writer = open_live_wal_writer(
        path, [_row(3, [4], uncached=70), _row(5, [6], uncached=80)], [_planner(4, SECONDS + 4), _planner(6, SECONDS + 6)],
    )
    try:
        wal = Path(f"{path}-wal")
        assert wal.stat().st_size > 0
        # Rows 3 and 5 live only in the WAL; an immutable read would miss them.
        calls = read_conversation_calls(path)
        assert [(c.gen_index, c.step_index, c.uncached_input) for c in calls] == [
            (1, 2, 10), (3, 4, 70), (5, 6, 80),
        ]
        assert calls[2].timestamp == _utc(SECONDS + 6)
        assert opened_uris[0].endswith("?mode=ro") and "immutable" not in opened_uris[0]
        assert wal.stat().st_size > 0
    finally:
        writer.close()


def test_live_wal_without_shm_is_not_opened_and_creates_nothing(folder, tmp_path, opened_uris):
    source = tmp_path / "source"
    source.mkdir()
    path = create_conversation_db(source / "live.db", [_row(1, [2])], [_planner(2)], wal=True)
    writer = open_live_wal_writer(path, [_row(3, [4])], [_planner(4)])
    try:
        # What a copy of a running agy's directory looks like: a live -wal, no -shm.
        copy = folder / "live.db"
        shutil.copyfile(path, copy)
        shutil.copyfile(f"{path}-wal", f"{copy}-wal")
    finally:
        writer.close()
    assert Path(f"{copy}-wal").stat().st_size > 0 and not Path(f"{copy}-shm").exists()

    def listing():
        return sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in folder.iterdir())

    before = listing()
    assert read_conversation_calls(copy) is None
    assert listing() == before and sorted(os.listdir(folder)) == ["live.db", "live.db-wal"]
    assert opened_uris == []


def test_uri_is_percent_quoted(tmp_path, opened_uris):
    folder = tmp_path / "odd dir#1?x"
    folder.mkdir()
    path = create_conversation_db(folder / "c d.db", [_row(1)])
    assert len(read_conversation_calls(path)) == 1
    assert "%20" in opened_uris[0] and "%23" in opened_uris[0] and "%3F" in opened_uris[0]


@pytest.fixture
def sleeps(monkeypatch):
    delays = []
    monkeypatch.setattr(reader.time, "sleep", delays.append)
    return delays


def _failing_connects(monkeypatch, errors):
    real = reader._connect
    attempts = []

    def connect(uri):
        attempts.append(uri)
        if len(attempts) <= len(errors):
            raise errors[len(attempts) - 1]
        return real(uri)

    monkeypatch.setattr(reader, "_connect", connect)
    return attempts


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("database is locked"),
    sqlite3.OperationalError("database table is locked"),
    sqlite3.OperationalError("database is busy"),
    sqlite3.DatabaseError("database disk image is malformed"),
])
def test_busy_or_torn_reads_are_retried_once(tmp_path, monkeypatch, sleeps, error):
    path = create_conversation_db(tmp_path / "c.db", [_row(1)])
    attempts = _failing_connects(monkeypatch, [error])
    assert len(read_conversation_calls(path)) == 1
    assert len(attempts) == 2 and sleeps == [0.05]


def test_a_second_busy_failure_gives_up(tmp_path, monkeypatch, sleeps):
    path = create_conversation_db(tmp_path / "c.db", [_row(1)])
    attempts = _failing_connects(
        monkeypatch, [sqlite3.OperationalError("database is locked")] * 3,
    )
    assert read_conversation_calls(path) is None
    assert len(attempts) == 2 and sleeps == [0.05]


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("unable to open database file"),
    sqlite3.OperationalError("no such table: gen_metadata"),
    sqlite3.InterfaceError("bad parameter"),
    sqlite3.Error("generic failure"),
])
def test_other_sqlite_errors_are_not_retried(tmp_path, monkeypatch, sleeps, error):
    path = create_conversation_db(tmp_path / "c.db", [_row(1)])
    attempts = _failing_connects(monkeypatch, [error])
    assert read_conversation_calls(path) is None
    assert len(attempts) == 1 and sleeps == []


def test_garbage_file_is_retried_once_then_gives_up(tmp_path, sleeps, opened_uris):
    path = tmp_path / "garbage.db"
    path.write_bytes(b"\x00not sqlite at all" * 100)
    assert read_conversation_calls(path) is None
    assert len(opened_uris) == 2 and sleeps == [0.05]


def test_every_connection_is_closed(tmp_path, monkeypatch, sleeps):
    class Spy(sqlite3.Connection):
        opened = closed = 0

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            type(self).opened += 1

        def close(self):
            type(self).closed += 1
            super().close()

    monkeypatch.setattr(
        reader, "_connect", lambda uri: sqlite3.connect(uri, uri=True, timeout=0.25, factory=Spy),
    )
    good = create_conversation_db(tmp_path / "good.db", [_row(1)])
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"nope" * 200)
    broken = tmp_path / "broken.db"
    with sqlite3.connect(broken) as connection:
        connection.execute("CREATE TABLE other(x)")
    assert len(read_conversation_calls(good)) == 1
    assert read_conversation_calls(garbage) is None
    assert read_conversation_calls(broken) is None
    assert Spy.opened == 4 and Spy.closed == Spy.opened
