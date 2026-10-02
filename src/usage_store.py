"""Shared, provider-aware SQLite store for normalized token usage.

The store extends Antigravity's existing ``token_usage.db`` in place. It keeps
only normalized usage records and dashboard metadata; transcript bodies are
never stored here. Provider IDs are namespaced in SQLite so two tools can use
the same native session ID without colliding.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from src.parsers.contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession

DB_PATH_ENV_VAR = "AI_USAGE_DB_PATH"
DEFAULT_DB_RELATIVE_PATH = Path(".gemini") / "antigravity-cli" / "token_usage.db"
SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 2000
_WRITE_ATTEMPTS = 3

_SESSION_ADDITIONS: dict[str, str] = {
    "provider": "TEXT NOT NULL DEFAULT 'antigravity'",
    "usage_semantics_version": "INTEGER NOT NULL DEFAULT 1",
    "cache_write_mode": "TEXT NOT NULL DEFAULT 'embedded_in_input'",
    "cache_write_5m_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_1h_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cost_uncached_usd": "REAL NOT NULL DEFAULT 0.0",
    "cost_cached_estimate_usd": "REAL NOT NULL DEFAULT 0.0",
    "savings_usd": "REAL NOT NULL DEFAULT 0.0",
    "reported_cost_usd": "REAL",
    "cost_source": "TEXT NOT NULL DEFAULT 'estimated'",
    "cost_currency": "TEXT NOT NULL DEFAULT 'USD'",
    "created_at": "TEXT",
    "start_time": "TEXT",
    "end_time": "TEXT",
    "activity_at": "TEXT",
    "reasoning_effort": "TEXT",
    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
}

_EVENT_ADDITIONS: dict[str, str] = {
    "provider": "TEXT NOT NULL DEFAULT 'antigravity'",
    "usage_semantics_version": "INTEGER NOT NULL DEFAULT 1",
    "cache_write_mode": "TEXT NOT NULL DEFAULT 'embedded_in_input'",
    "cache_write_5m_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_1h_tokens": "INTEGER NOT NULL DEFAULT 0",
    "event_id": "TEXT",
    "timestamp_missing": "INTEGER NOT NULL DEFAULT 0",
    "cost_uncached_usd": "REAL NOT NULL DEFAULT 0.0",
    "cost_cached_estimate_usd": "REAL NOT NULL DEFAULT 0.0",
    "savings_usd": "REAL NOT NULL DEFAULT 0.0",
    "reported_cost_usd": "REAL",
    "cost_source": "TEXT NOT NULL DEFAULT 'estimated'",
    "cost_currency": "TEXT NOT NULL DEFAULT 'USD'",
    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
}

# The dashboard only needs these compact facts from parser metadata. Keeping an
# allowlist here prevents a future parser from accidentally persisting prompt,
# response, or tool-output text in the shared usage database.
_SESSION_METADATA_KEYS = frozenset({
    "estimated",
    "token_source",
    "merged_session_count",
    "capture_quality",
    "capture_source_hash",
    "capture_sources",
    "capture_mtime_ns",
    "capture_ctime_ns",
    "capture_size",
})
_EVENT_METADATA_KEYS = frozenset({
    "capture_source_hash",
    "tps_duration_seconds",
    "tps_output_tokens",
    "tps_trustworthy",
    "synthetic",
    "call_count",
})


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve an explicit path, environment override, or the shared AGY DB."""
    if db_path is not None:
        return Path(db_path).expanduser()
    override = os.environ.get(DB_PATH_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_DB_RELATIVE_PATH


def _canonical_provider(provider: str) -> str:
    value = str(provider or "").strip().casefold()
    aliases = {"agy": "antigravity", "gemini": "antigravity", "claude": "claude-code"}
    value = aliases.get(value, value)
    if not value:
        raise ValueError("provider must be a non-empty string")
    return value


def _namespaced_id(provider: str, session_id: str) -> str:
    native_id = str(session_id)
    prefix = f"{provider}:"
    if native_id.startswith(prefix):
        native_id = native_id[len(prefix):]
    return f"{prefix}{native_id}"


def _original_id(provider: str, stored_id: str) -> str:
    prefix = f"{provider}:"
    return stored_id[len(prefix):] if stored_id.startswith(prefix) else stored_id


def _metadata_json(value: dict[str, Any] | None, allowed: frozenset[str]) -> str:
    value = value if isinstance(value, dict) else {}
    safe = {key: value[key] for key in allowed if key in value}
    try:
        return json.dumps(safe, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        # Parser metadata is advisory. Preserve only JSON-safe scalar values.
        scalar = {
            key: item
            for key, item in safe.items()
            if item is None or isinstance(item, (str, bool, int, float))
        }
        try:
            return json.dumps(scalar, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return "{}"


def _decode_metadata(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _timestamp_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.isoformat()
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
    except (ValueError, OverflowError):
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _money(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return 0.0
        return float(amount)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return 0.0


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA query_only = ON")
    return connection


def _connect_read_write(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(
    connection: sqlite3.Connection,
    table: str,
    additions: dict[str, str],
) -> None:
    present = _table_columns(connection, table)
    for name, declaration in additions.items():
        if name not in present:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _create_legacy_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            date TEXT NOT NULL,
            model TEXT NOT NULL,
            workspace TEXT,
            title TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            call_count INTEGER NOT NULL DEFAULT 0,
            step_count INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            last_step_index INTEGER NOT NULL DEFAULT -1,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS token_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            UNIQUE(session_id, step_index)
        )"""
    )


def _assert_session_key(connection: sqlite3.Connection) -> None:
    info = list(connection.execute("PRAGMA table_info(sessions)"))
    if not any(str(row[1]) == "session_id" and int(row[5] or 0) > 0 for row in info):
        unique_session_id = False
        for index in connection.execute("PRAGMA index_list(sessions)"):
            if not int(index[2] or 0):
                continue
            columns = [str(row[2]) for row in connection.execute(f"PRAGMA index_info({index[1]})")]
            if columns == ["session_id"]:
                unique_session_id = True
                break
        if not unique_session_id:
            raise RuntimeError("usage database sessions.session_id must be a primary or unique key")


def _recreate_views(connection: sqlite3.Connection) -> None:
    for view_name in ("daily_summary", "model_summary", "provider_daily_summary", "provider_model_summary"):
        existing = connection.execute(
            "SELECT type FROM sqlite_master WHERE name = ?", (view_name,)
        ).fetchone()
        if existing and existing[0] == "view":
            connection.execute(f"DROP VIEW {view_name}")

    metric_columns = """COUNT(*) AS sessions,
        SUM(input_tokens) AS input_tokens,
        SUM(cached_input_tokens) AS cached_input_tokens,
        SUM(output_tokens) AS output_tokens,
        SUM(cache_write_tokens) AS cache_write_tokens,
        SUM(total_tokens) AS total_tokens,
        ROUND(SUM(cost_usd), 4) AS cost_usd,
        CASE WHEN SUM(input_tokens) > 0
             THEN ROUND(CAST(SUM(cached_input_tokens) AS REAL) / SUM(input_tokens) * 100.0, 2)
             ELSE 0.0 END AS cache_hit_rate"""
    connection.execute(
        f"""CREATE VIEW daily_summary AS
            SELECT date, {metric_columns}
            FROM sessions WHERE provider = 'antigravity'
            GROUP BY date ORDER BY date DESC"""
    )
    connection.execute(
        f"""CREATE VIEW model_summary AS
            SELECT model, {metric_columns}
            FROM sessions WHERE provider = 'antigravity'
            GROUP BY model ORDER BY total_tokens DESC"""
    )
    connection.execute(
        f"""CREATE VIEW provider_daily_summary AS
            SELECT provider, date, {metric_columns}
            FROM sessions GROUP BY provider, date ORDER BY date DESC, provider"""
    )
    connection.execute(
        f"""CREATE VIEW provider_model_summary AS
            SELECT provider, model, {metric_columns}
            FROM sessions GROUP BY provider, model ORDER BY total_tokens DESC"""
    )


def ensure_schema(db_path: str | Path | None = None) -> None:
    """Create or migrate the shared database without rewriting old usage rows.

    Existing AGY rows receive additive defaults (provider ``antigravity``,
    semantics version 1). The WAL journal mode is changed only during the
    initial migration, not on each write.
    """
    path = resolve_db_path(db_path)
    for attempt in range(_WRITE_ATTEMPTS):
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_read_write(path)
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            session_columns = _table_columns(connection, "sessions")
            event_columns = _table_columns(connection, "token_events")
            migrated = (
                current_version >= SCHEMA_VERSION
                and set(_SESSION_ADDITIONS).issubset(session_columns)
                and set(_EVENT_ADDITIONS).issubset(event_columns)
                and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'usage_capture_state'"
                ).fetchone() is not None
            )
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"usage database schema {current_version} is newer than supported schema {SCHEMA_VERSION}"
                )
            if migrated:
                return

            # This pragma persists, so keep it out of the per-event write path.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            _create_legacy_tables(connection)
            _add_missing_columns(connection, "sessions", _SESSION_ADDITIONS)
            _add_missing_columns(connection, "token_events", _EVENT_ADDITIONS)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS usage_capture_state (
                    provider TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    backfill_completed_at TEXT,
                    updated_at TEXT NOT NULL
                )"""
            )
            _assert_session_key(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_provider_date ON sessions(provider, date)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_provider_model ON sessions(provider, model)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_provider_session ON token_events(provider, session_id, step_index)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_provider_event_id "
                "ON token_events(provider, session_id, event_id) WHERE event_id IS NOT NULL"
            )
            _recreate_views(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
            return
        except sqlite3.OperationalError as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            locked = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
            if not locked or attempt + 1 >= _WRITE_ATTEMPTS:
                raise
            time.sleep(0.05 * (2 ** attempt))
        except Exception:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()


def _cost_from_row(row: sqlite3.Row, prefix: str = "") -> CostEstimate | None:
    keys = set(row.keys())
    cost_key = f"{prefix}cost_usd"
    if cost_key not in keys:
        return None
    source_key = f"{prefix}cost_source"
    source = str(row[source_key] or "estimated") if source_key in keys else "estimated"
    reported_key = f"{prefix}reported_cost_usd"
    cached_estimate_key = f"{prefix}cost_cached_estimate_usd"
    uncached_key = f"{prefix}cost_uncached_usd"
    savings_key = f"{prefix}savings_usd"
    currency_key = f"{prefix}cost_currency"
    reported = row[reported_key] if reported_key in keys else None
    cached_estimate = row[cached_estimate_key] if cached_estimate_key in keys else row[cost_key]
    if cached_estimate in (None, 0, 0.0) and source != "reported" and row[cost_key]:
        # Legacy Antigravity rows predate the separate estimate field; their
        # cost_usd column already contains the estimate.
        cached_estimate = row[cost_key]
    return CostEstimate(
        cached_usd=cached_estimate or 0.0,
        uncached_usd=row[uncached_key] if uncached_key in keys else 0.0,
        savings_usd=row[savings_key] if savings_key in keys else 0.0,
        reported_usd=reported,
        currency=row[currency_key] if currency_key in keys else "USD",
        source=source,
    )


def read_usage_sessions(
    provider: str,
    db_path: str | Path | None = None,
) -> list[UsageSession]:
    """Read normalized sessions for one provider without creating or migrating.

    Missing databases and older schemas return an empty list. If the provider
    column has not yet been added, existing rows are recognized only as legacy
    Antigravity records; other providers never claim those rows.
    """
    canonical = _canonical_provider(provider)
    path = resolve_db_path(db_path)
    if not path.is_file():
        return []

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_read_only(path)
        connection.execute("BEGIN")
        session_columns = _table_columns(connection, "sessions")
        if "session_id" not in session_columns or "model" not in session_columns:
            return []
        has_provider = "provider" in session_columns
        if has_provider:
            session_rows = connection.execute(
                "SELECT * FROM sessions WHERE provider = ? ORDER BY timestamp, session_id",
                (canonical,),
            ).fetchall()
        elif canonical == "antigravity":
            session_rows = connection.execute(
                "SELECT * FROM sessions ORDER BY timestamp, session_id"
            ).fetchall()
        else:
            return []

        if not session_rows:
            return []

        events_by_session: dict[str, list[sqlite3.Row]] = {}
        event_columns = _table_columns(connection, "token_events")
        if "session_id" in event_columns:
            ids = [str(row["session_id"]) for row in session_rows]
            event_provider_clause = " AND e.provider = ?" if "provider" in event_columns else ""
            for offset in range(0, len(ids), 500):
                batch = ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                parameters: tuple[Any, ...] = tuple(batch) + (
                    (canonical,) if "provider" in event_columns else ()
                )
                event_rows = connection.execute(
                    "SELECT e.* FROM token_events AS e "
                    f"WHERE e.session_id IN ({placeholders}){event_provider_clause} "
                    "ORDER BY e.session_id, e.step_index",
                    parameters,
                ).fetchall()
                for event_row in event_rows:
                    events_by_session.setdefault(str(event_row["session_id"]), []).append(event_row)

        sessions: list[UsageSession] = []
        for row in session_rows:
            row_keys = set(row.keys())
            stored_id = str(row["session_id"] or "")
            raw_events: list[UsageEvent] = []
            for event_row in events_by_session.get(stored_id, []):
                keys = set(event_row.keys())
                event_usage = TokenUsage(
                    input_tokens=event_row["input_tokens"] if "input_tokens" in keys else 0,
                    cached_input_tokens=event_row["cached_input_tokens"] if "cached_input_tokens" in keys else 0,
                    output_tokens=event_row["output_tokens"] if "output_tokens" in keys else 0,
                    reasoning_output_tokens=(
                        event_row["reasoning_output_tokens"]
                        if "reasoning_output_tokens" in keys else 0
                    ),
                    total_tokens=event_row["total_tokens"] if "total_tokens" in keys else 0,
                    cache_write_tokens=(
                        event_row["cache_write_tokens"] if "cache_write_tokens" in keys else 0
                    ),
                    cache_write_5m_tokens=(
                        event_row["cache_write_5m_tokens"]
                        if "cache_write_5m_tokens" in keys else 0
                    ),
                    cache_write_1h_tokens=(
                        event_row["cache_write_1h_tokens"]
                        if "cache_write_1h_tokens" in keys else 0
                    ),
                    preserve_total=True,
                )
                raw_events.append(UsageEvent(
                    timestamp=(
                        None
                        if "timestamp_missing" in keys and bool(event_row["timestamp_missing"])
                        else event_row["timestamp"] if "timestamp" in keys else None
                    ),
                    usage=event_usage,
                    model=event_row["model"] if "model" in keys else row["model"],
                    cost=_cost_from_row(event_row),
                    event_id=event_row["event_id"] if "event_id" in keys else None,
                    metadata=_decode_metadata(event_row["metadata_json"] if "metadata_json" in keys else None),
                ))

            usage = TokenUsage(
                input_tokens=row["input_tokens"] if "input_tokens" in row_keys else 0,
                cached_input_tokens=row["cached_input_tokens"] if "cached_input_tokens" in row_keys else 0,
                output_tokens=row["output_tokens"] if "output_tokens" in row_keys else 0,
                reasoning_output_tokens=(
                    row["reasoning_output_tokens"]
                    if "reasoning_output_tokens" in row_keys else 0
                ),
                total_tokens=row["total_tokens"] if "total_tokens" in row_keys else 0,
                cache_write_tokens=(
                    row["cache_write_tokens"] if "cache_write_tokens" in row_keys else 0
                ),
                cache_write_5m_tokens=(
                    row["cache_write_5m_tokens"]
                    if "cache_write_5m_tokens" in row_keys else 0
                ),
                cache_write_1h_tokens=(
                    row["cache_write_1h_tokens"]
                    if "cache_write_1h_tokens" in row_keys else 0
                ),
                preserve_total=True,
            )
            sessions.append(UsageSession(
                id=_original_id(canonical, stored_id),
                tool=canonical,
                provider=canonical,
                model=row["model"],
                title=row["title"] if "title" in row_keys else None,
                created_at=row["created_at"] if "created_at" in row_keys else None,
                start_time=row["start_time"] if "start_time" in row_keys else row["timestamp"],
                end_time=row["end_time"] if "end_time" in row_keys else row["updated_at"] if "updated_at" in row_keys else None,
                activity_at=row["activity_at"] if "activity_at" in row_keys else None,
                reasoning_effort=row["reasoning_effort"] if "reasoning_effort" in row_keys else None,
                usage=usage,
                events=raw_events,
                cost=_cost_from_row(row),
                metadata=_decode_metadata(row["metadata_json"] if "metadata_json" in row_keys else None),
                call_count=row["call_count"] if "call_count" in row_keys else len(raw_events),
            ))
        return sessions
    except sqlite3.Error:
        # Readers must be safe against an absent or older optional database.
        return []
    finally:
        if connection is not None:
            connection.close()


def is_provider_capture_enabled(
    provider: str,
    db_path: str | Path | None = None,
) -> bool:
    """Return the persisted capture-mode flag without creating or migrating."""
    canonical = _canonical_provider(provider)
    path = resolve_db_path(db_path)
    if not path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_read_only(path)
        connection.execute("BEGIN")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "usage_capture_state" not in tables:
            return False
        row = connection.execute(
            "SELECT enabled FROM usage_capture_state WHERE provider = ?",
            (canonical,),
        ).fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()


def mark_provider_capture_enabled(
    provider: str,
    db_path: str | Path | None = None,
) -> None:
    """Mark a provider's full usage backfill complete, enabling DB-only reads."""
    canonical = _canonical_provider(provider)
    path = resolve_db_path(db_path)
    ensure_schema(path)
    now = datetime.now(timezone.utc).isoformat()
    connection = _connect_read_write(path)
    try:
        with connection:
            connection.execute(
                """INSERT INTO usage_capture_state (
                    provider, enabled, backfill_completed_at, updated_at
                ) VALUES (?, 1, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    enabled = 1,
                    backfill_completed_at = COALESCE(
                        usage_capture_state.backfill_completed_at,
                        excluded.backfill_completed_at
                    ),
                    updated_at = excluded.updated_at""",
                (canonical, now, now),
            )
    finally:
        connection.close()


def get_usage_store_status(db_path: str | Path | None = None) -> dict[str, Any]:
    """Return safe aggregate database diagnostics, without session identities."""
    path = resolve_db_path(db_path)
    if not path.is_file():
        return {"database_exists": False, "providers": {}}

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_read_only(path)
        connection.execute("BEGIN")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "sessions" not in tables:
            return {"database_exists": True, "providers": {}}

        session_columns = _table_columns(connection, "sessions")
        provider_expression = "provider" if "provider" in session_columns else "'antigravity'"
        total_expression = "SUM(total_tokens)" if "total_tokens" in session_columns else "0"
        aggregates = connection.execute(
            f"SELECT {provider_expression} AS provider, COUNT(*) AS sessions, "
            f"COALESCE({total_expression}, 0) AS total_tokens "
            f"FROM sessions GROUP BY {provider_expression}"
        ).fetchall()
        providers: dict[str, dict[str, Any]] = {
            str(row["provider"]): {
                "sessions": int(row["sessions"] or 0),
                "events": 0,
                "total_tokens": int(row["total_tokens"] or 0),
                "capture_enabled": False,
                "backfill_completed_at": None,
            }
            for row in aggregates
        }

        if "token_events" in tables:
            event_columns = _table_columns(connection, "token_events")
            if "provider" in event_columns:
                event_rows = connection.execute(
                    "SELECT provider, COUNT(*) AS events FROM token_events GROUP BY provider"
                ).fetchall()
            elif "provider" in session_columns:
                event_rows = connection.execute(
                    "SELECT s.provider, COUNT(*) AS events FROM token_events AS e "
                    "JOIN sessions AS s ON s.session_id = e.session_id "
                    "GROUP BY s.provider"
                ).fetchall()
            else:
                event_rows = [connection.execute(
                    "SELECT 'antigravity' AS provider, COUNT(*) AS events FROM token_events"
                ).fetchone()]
            for row in event_rows:
                providers.setdefault(
                    str(row["provider"]),
                    {
                        "sessions": 0,
                        "events": 0,
                        "total_tokens": 0,
                        "capture_enabled": False,
                        "backfill_completed_at": None,
                    },
                )["events"] = int(row["events"] or 0)

        if "usage_capture_state" in tables:
            for row in connection.execute(
                "SELECT provider, enabled, backfill_completed_at FROM usage_capture_state"
            ):
                provider = str(row["provider"])
                providers.setdefault(
                    provider,
                    {
                        "sessions": 0,
                        "events": 0,
                        "total_tokens": 0,
                        "capture_enabled": False,
                        "backfill_completed_at": None,
                    },
                )
                providers[provider]["capture_enabled"] = bool(row["enabled"])
                providers[provider]["backfill_completed_at"] = row["backfill_completed_at"]

        return {
            "database_exists": True,
            "providers": dict(sorted(providers.items())),
        }
    except sqlite3.Error:
        return {"database_exists": True, "providers": {}, "read_error": True}
    finally:
        if connection is not None:
            connection.close()


def _session_cost(
    session: UsageSession,
) -> tuple[float, float, float, float, float | None, str, str]:
    cost = session.cost
    if cost is None:
        return 0.0, 0.0, 0.0, 0.0, None, "unpriced", "USD"
    amount = _money(cost.total_usd)
    cached_estimate = _money(cost.cached_usd)
    reported = _money(cost.reported_usd) if cost.reported_usd is not None else None
    return (
        amount,
        cached_estimate,
        _money(cost.uncached_usd),
        _money(cost.savings_usd),
        reported,
        cost.source or "estimated",
        cost.currency or "USD",
    )


def _event_cost(
    event: UsageEvent,
) -> tuple[float, float, float, float, float | None, str, str]:
    cost = event.cost
    if cost is None:
        return 0.0, 0.0, 0.0, 0.0, None, "unpriced", "USD"
    amount = _money(cost.total_usd)
    cached_estimate = _money(cost.cached_usd)
    reported = _money(cost.reported_usd) if cost.reported_usd is not None else None
    return (
        amount,
        cached_estimate,
        _money(cost.uncached_usd),
        _money(cost.savings_usd),
        reported,
        cost.source or "estimated",
        cost.currency or "USD",
    )


def _event_id(provider: str, session_id: str, index: int, event: UsageEvent) -> str:
    if event.event_id:
        return str(event.event_id)
    material = "|".join((
        provider,
        session_id,
        str(index),
        _timestamp_text(event.timestamp) or "",
        event.model or "",
        str(event.usage.input_tokens),
        str(event.usage.cached_input_tokens),
        str(event.usage.output_tokens),
        str(event.usage.total_tokens),
    ))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _session_row(provider: str, session: UsageSession) -> tuple[Any, ...]:
    native_id = _original_id(provider, str(session.id))
    stored_id = _namespaced_id(provider, native_id)
    event_times = [event.timestamp for event in session.events if event.timestamp is not None]
    timestamp = (
        _timestamp_text(session.start_time)
        or _timestamp_text(session.created_at)
        or min((_timestamp_text(value) for value in event_times if value is not None), default=None)
        or _timestamp_text(session.activity_at)
        or datetime.now(timezone.utc).isoformat()
    )
    model = str(session.model or "unknown").strip() or "unknown"
    cost, cached_estimate, uncached_cost, savings, reported_cost, cost_source, currency = _session_cost(session)
    call_count = max(session.call_count, len(session.events))
    metadata = dict(session.metadata or {})
    metadata.setdefault("token_source", "reported")
    metadata.setdefault("estimated", False)
    step_count = max(int(metadata.get("step_count") or 0), call_count)
    now = datetime.now(timezone.utc).isoformat()
    return (
        stored_id,
        timestamp,
        timestamp[:10],
        model,
        None,
        session.title,
        session.usage.input_tokens,
        session.usage.cached_input_tokens,
        session.usage.output_tokens,
        session.usage.cache_write_tokens,
        session.usage.reasoning_output_tokens,
        session.usage.total_tokens,
        call_count,
        step_count,
        cost,
        len(session.events) - 1,
        now,
        provider,
        2,
        "additive",
        session.usage.cache_write_5m_tokens,
        session.usage.cache_write_1h_tokens,
        cached_estimate,
        uncached_cost,
        savings,
        reported_cost,
        cost_source,
        currency,
        _timestamp_text(session.created_at),
        _timestamp_text(session.start_time),
        _timestamp_text(session.end_time),
        _timestamp_text(session.activity_at),
        session.reasoning_effort,
        _metadata_json(metadata, _SESSION_METADATA_KEYS),
    )


_SESSION_WRITE_COLUMNS = (
    "session_id, timestamp, date, model, workspace, title, input_tokens, "
    "cached_input_tokens, output_tokens, cache_write_tokens, reasoning_output_tokens, "
    "total_tokens, call_count, step_count, cost_usd, last_step_index, updated_at, "
    "provider, usage_semantics_version, cache_write_mode, cache_write_5m_tokens, "
    "cache_write_1h_tokens, cost_cached_estimate_usd, cost_uncached_usd, savings_usd, reported_cost_usd, "
    "cost_source, cost_currency, created_at, start_time, end_time, activity_at, "
    "reasoning_effort, metadata_json"
)
_SESSION_WRITE_PLACEHOLDERS = ",".join("?" for _ in range(34))


def _capture_revision(metadata: dict[str, Any]) -> tuple[int, int, int] | None:
    """Return the stat revision used to guard overlapping transcript hooks."""
    if not metadata.get("capture_source_hash"):
        return None
    try:
        return (
            int(metadata["capture_mtime_ns"]),
            int(metadata["capture_ctime_ns"]),
            int(metadata["capture_size"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _source_hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        return None
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-" for character in normalized):
        return None
    return normalized


def _source_manifest(metadata: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Return the safe per-transcript stat revision map from session metadata."""
    manifest: dict[str, dict[str, int]] = {}
    raw_sources = metadata.get("capture_sources")
    if isinstance(raw_sources, dict):
        items = raw_sources.items()
    elif isinstance(raw_sources, list):
        items = (
            (entry.get("capture_source_hash"), entry)
            for entry in raw_sources
            if isinstance(entry, dict)
        )
    else:
        items = ()

    for raw_hash, raw_revision in items:
        source_hash = _source_hash(raw_hash)
        if source_hash is None or not isinstance(raw_revision, dict):
            continue
        try:
            revision = {
                "capture_mtime_ns": int(raw_revision.get("capture_mtime_ns", raw_revision.get("mtime_ns"))),
                "capture_ctime_ns": int(raw_revision.get("capture_ctime_ns", raw_revision.get("ctime_ns"))),
                "capture_size": int(raw_revision.get("capture_size", raw_revision.get("size"))),
            }
        except (TypeError, ValueError, OverflowError):
            continue
        if min(revision.values()) < 0:
            continue
        manifest[source_hash] = revision

    if not manifest:
        source_hash = _source_hash(metadata.get("capture_source_hash"))
        revision = _capture_revision(metadata)
        if source_hash is not None and revision is not None:
            manifest[source_hash] = {
                "capture_mtime_ns": revision[0],
                "capture_ctime_ns": revision[1],
                "capture_size": revision[2],
            }
    return manifest


def _revision_tuple(value: dict[str, int] | None) -> tuple[int, int, int] | None:
    if value is None:
        return None
    return (
        value["capture_mtime_ns"],
        value["capture_ctime_ns"],
        value["capture_size"],
    )


def _prepare_capture_session(session: UsageSession) -> UsageSession:
    """Clone a session and annotate events with its transcript source identity."""
    prepared = deepcopy(session)
    metadata = dict(prepared.metadata or {})
    sources = _source_manifest(metadata)
    if sources:
        metadata["capture_sources"] = sources
        if len(sources) == 1:
            source_hash, revision = next(iter(sources.items()))
            metadata["capture_source_hash"] = source_hash
            metadata.update(revision)
            for event in prepared.events:
                event.metadata = dict(event.metadata or {})
                event.metadata.setdefault("capture_source_hash", source_hash)
        elif metadata.get("capture_quality") is None:
            metadata["capture_quality"] = "merged-fragments"
    prepared.metadata = metadata
    return prepared


def _usage_from_row(row: sqlite3.Row, keys: set[str]) -> TokenUsage:
    return TokenUsage(
        input_tokens=row["input_tokens"] if "input_tokens" in keys else 0,
        cached_input_tokens=row["cached_input_tokens"] if "cached_input_tokens" in keys else 0,
        output_tokens=row["output_tokens"] if "output_tokens" in keys else 0,
        reasoning_output_tokens=(
            row["reasoning_output_tokens"] if "reasoning_output_tokens" in keys else 0
        ),
        total_tokens=row["total_tokens"] if "total_tokens" in keys else 0,
        cache_write_tokens=row["cache_write_tokens"] if "cache_write_tokens" in keys else 0,
        cache_write_5m_tokens=(
            row["cache_write_5m_tokens"] if "cache_write_5m_tokens" in keys else 0
        ),
        cache_write_1h_tokens=(
            row["cache_write_1h_tokens"] if "cache_write_1h_tokens" in keys else 0
        ),
        preserve_total=True,
    )


def _stored_session(
    connection: sqlite3.Connection,
    provider: str,
    stored_id: str,
) -> UsageSession | None:
    row = connection.execute(
        "SELECT * FROM sessions WHERE provider = ? AND session_id = ?",
        (provider, stored_id),
    ).fetchone()
    if row is None:
        return None
    row_keys = set(row.keys())
    event_rows = connection.execute(
        "SELECT * FROM token_events WHERE provider = ? AND session_id = ? ORDER BY step_index",
        (provider, stored_id),
    ).fetchall()
    events: list[UsageEvent] = []
    metadata = _decode_metadata(row["metadata_json"] if "metadata_json" in row_keys else None)
    sources = _source_manifest(metadata)
    only_source = next(iter(sources)) if len(sources) == 1 else None
    for event_row in event_rows:
        event_keys = set(event_row.keys())
        event_metadata = _decode_metadata(
            event_row["metadata_json"] if "metadata_json" in event_keys else None
        )
        if only_source is not None:
            event_metadata.setdefault("capture_source_hash", only_source)
        events.append(UsageEvent(
            timestamp=(
                None
                if "timestamp_missing" in event_keys and bool(event_row["timestamp_missing"])
                else event_row["timestamp"] if "timestamp" in event_keys else None
            ),
            usage=_usage_from_row(event_row, event_keys),
            model=event_row["model"] if "model" in event_keys else row["model"],
            cost=_cost_from_row(event_row),
            event_id=event_row["event_id"] if "event_id" in event_keys else None,
            metadata=event_metadata,
        ))

    return UsageSession(
        id=_original_id(provider, stored_id),
        tool=provider,
        provider=provider,
        model=row["model"],
        title=row["title"] if "title" in row_keys else None,
        created_at=row["created_at"] if "created_at" in row_keys else None,
        start_time=row["start_time"] if "start_time" in row_keys else row["timestamp"],
        end_time=row["end_time"] if "end_time" in row_keys else row["updated_at"] if "updated_at" in row_keys else None,
        activity_at=row["activity_at"] if "activity_at" in row_keys else None,
        reasoning_effort=row["reasoning_effort"] if "reasoning_effort" in row_keys else None,
        usage=_usage_from_row(row, row_keys),
        events=events,
        cost=_cost_from_row(row),
        metadata=metadata,
        call_count=row["call_count"] if "call_count" in row_keys else len(events),
    )


def _sum_event_usage(events: list[UsageEvent]) -> TokenUsage:
    return TokenUsage(
        input_tokens=sum(event.usage.input_tokens for event in events),
        cached_input_tokens=sum(event.usage.cached_input_tokens for event in events),
        output_tokens=sum(event.usage.output_tokens for event in events),
        reasoning_output_tokens=sum(event.usage.reasoning_output_tokens for event in events),
        total_tokens=sum(event.usage.total_tokens for event in events),
        cache_read_tokens=sum(event.usage.cache_read_tokens or 0 for event in events),
        cache_write_tokens=sum(event.usage.cache_write_tokens for event in events),
        cache_write_5m_tokens=sum(event.usage.cache_write_5m_tokens for event in events),
        cache_write_1h_tokens=sum(event.usage.cache_write_1h_tokens for event in events),
        preserve_total=True,
    )


def _sum_event_cost(events: list[UsageEvent], fallback: CostEstimate | None) -> CostEstimate | None:
    costs = [event.cost for event in events if event.cost is not None]
    if not costs:
        return fallback
    reported = (
        sum((cost.reported_usd for cost in costs), Decimal("0"))
        if len(costs) == len(events) and all(cost.reported_usd is not None for cost in costs)
        else None
    )
    currencies = {cost.currency for cost in costs}
    sources = {cost.source for cost in costs}
    return CostEstimate(
        cached_usd=sum((cost.cached_usd for cost in costs), Decimal("0")),
        uncached_usd=sum((cost.uncached_usd for cost in costs), Decimal("0")),
        savings_usd=sum((cost.savings_usd for cost in costs), Decimal("0")),
        reported_usd=reported,
        currency=next(iter(currencies)) if len(currencies) == 1 else "USD",
        source=("reported" if reported is not None else "estimated")
        if sources != {"unpriced"} else "unpriced",
    )


def _time_bound(values: list[datetime | None], *, latest: bool) -> datetime | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return max(present) if latest else min(present)


def _merge_capture_snapshots(
    connection: sqlite3.Connection,
    provider: str,
    stored_id: str,
    incoming: UsageSession,
    stored_sources: dict[str, dict[str, int]],
) -> UsageSession | None:
    """Replace only the source fragments revised by an incoming snapshot."""
    incoming_metadata = dict(incoming.metadata or {})
    incoming_sources = _source_manifest(incoming_metadata)
    if not incoming_sources:
        return None
    if len(incoming_sources) > 1:
        missing_source = any(
            _source_hash(event.metadata.get("capture_source_hash")) not in incoming_sources
            for event in incoming.events
        )
        if missing_source:
            raise ValueError("merged Codex events must identify their source transcript")

    old_session = _stored_session(connection, provider, stored_id)
    if old_session is None:
        return None
    accepted_sources: dict[str, dict[str, int]] = {}
    for source_hash, revision in incoming_sources.items():
        old_revision = _revision_tuple(stored_sources.get(source_hash))
        new_revision = _revision_tuple(revision)
        if old_revision is not None and new_revision is not None and new_revision < old_revision:
            continue
        accepted_sources[source_hash] = revision
    if not accepted_sources:
        return None

    old_event_sources = {
        id(event): _source_hash(event.metadata.get("capture_source_hash"))
        for event in old_session.events
    }
    if len(stored_sources) == 1:
        fallback_source = next(iter(stored_sources))
        old_event_sources = {
            key: value or fallback_source for key, value in old_event_sources.items()
        }
    events = [
        event
        for event in old_session.events
        if old_event_sources[id(event)] not in accepted_sources
    ]
    for event in incoming.events:
        event_source = _source_hash(event.metadata.get("capture_source_hash"))
        if event_source is None and len(incoming_sources) == 1:
            event_source = next(iter(incoming_sources))
            event.metadata = dict(event.metadata or {})
            event.metadata["capture_source_hash"] = event_source
        if event_source in accepted_sources:
            events.append(event)
    events.sort(
        key=lambda event: (
            event.timestamp is None,
            event.timestamp or datetime.max.replace(tzinfo=timezone.utc),
            event.event_id or "",
        )
    )

    sources = dict(stored_sources)
    sources.update(accepted_sources)
    metadata = dict(old_session.metadata or {})
    metadata.update(incoming_metadata)
    metadata["capture_sources"] = sources
    if len(sources) > 1:
        metadata["capture_quality"] = "merged-fragments"
        composite = "merged:" + hashlib.sha256(
            "\n".join(sorted(sources)).encode("utf-8")
        ).hexdigest()
        metadata["capture_source_hash"] = composite
        metadata.pop("capture_mtime_ns", None)
        metadata.pop("capture_ctime_ns", None)
        metadata.pop("capture_size", None)
    else:
        source_hash, revision = next(iter(sources.items()))
        metadata["capture_source_hash"] = source_hash
        metadata.update(revision)

    event_times = [event.timestamp for event in events]
    usage = _sum_event_usage(events)
    return UsageSession(
        id=incoming.id,
        tool=provider,
        provider=provider,
        model=incoming.model or old_session.model,
        title=incoming.title or old_session.title,
        created_at=old_session.created_at or incoming.created_at,
        start_time=_time_bound([old_session.start_time, incoming.start_time, *event_times], latest=False),
        end_time=_time_bound([old_session.end_time, incoming.end_time, *event_times], latest=True),
        activity_at=_time_bound([old_session.activity_at, incoming.activity_at, *event_times], latest=True),
        reasoning_effort=incoming.reasoning_effort or old_session.reasoning_effort,
        usage=usage,
        events=events,
        cost=_sum_event_cost(events, incoming.cost or old_session.cost),
        metadata=metadata,
        call_count=len(events) if events else max(incoming.call_count, old_session.call_count),
    )


def _upsert_session(connection: sqlite3.Connection, provider: str, session: UsageSession) -> bool:
    session = _prepare_capture_session(session)
    values = _session_row(provider, session)
    existing = connection.execute(
        "SELECT metadata_json FROM sessions WHERE session_id = ? AND provider = ?",
        (values[0], provider),
    ).fetchone()
    if existing is not None:
        stored_metadata = _decode_metadata(existing["metadata_json"])
        incoming_metadata = dict(session.metadata or {})
        stored_sources = _source_manifest(stored_metadata)
        incoming_sources = _source_manifest(incoming_metadata)
        stored_source = stored_metadata.get("capture_source_hash")
        incoming_source = incoming_metadata.get("capture_source_hash")
        stored_revision = _capture_revision(stored_metadata)
        incoming_revision = _capture_revision(incoming_metadata)
        if stored_sources and not incoming_sources:
            # A missing transcript can make Codex fall back to the coarse
            # state_5.sqlite total. Never let that unrevisioned fallback erase
            # a richer transcript-backed snapshot.
            return False
        if stored_sources and incoming_sources:
            merged_session = _merge_capture_snapshots(
                connection,
                provider,
                values[0],
                session,
                stored_sources,
            )
            if merged_session is None:
                return False
            session = merged_session
            values = _session_row(provider, session)
        elif stored_source and not incoming_source:
            # Preserve compatibility with rows written before source manifests
            # were introduced.
            return False
        if (
            not stored_sources
            and stored_source
            and incoming_source == stored_source
            and stored_revision is not None
            and incoming_revision is not None
            and incoming_revision < stored_revision
        ):
            # An overlapping Stop/Interrupt/SessionEnd hook may finish later
            # after parsing an older version of an appended rollout.
            return False

    connection.execute(
        f"INSERT INTO sessions ({_SESSION_WRITE_COLUMNS}) VALUES ({_SESSION_WRITE_PLACEHOLDERS}) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "timestamp=excluded.timestamp, date=excluded.date, model=excluded.model, "
        "workspace=excluded.workspace, title=excluded.title, input_tokens=excluded.input_tokens, "
        "cached_input_tokens=excluded.cached_input_tokens, output_tokens=excluded.output_tokens, "
        "cache_write_tokens=excluded.cache_write_tokens, "
        "reasoning_output_tokens=excluded.reasoning_output_tokens, "
        "total_tokens=excluded.total_tokens, call_count=excluded.call_count, "
        "step_count=excluded.step_count, cost_usd=excluded.cost_usd, "
        "last_step_index=excluded.last_step_index, updated_at=excluded.updated_at, "
        "provider=excluded.provider, usage_semantics_version=excluded.usage_semantics_version, "
        "cache_write_mode=excluded.cache_write_mode, "
        "cache_write_5m_tokens=excluded.cache_write_5m_tokens, "
        "cache_write_1h_tokens=excluded.cache_write_1h_tokens, "
        "cost_cached_estimate_usd=excluded.cost_cached_estimate_usd, "
        "cost_uncached_usd=excluded.cost_uncached_usd, savings_usd=excluded.savings_usd, "
        "reported_cost_usd=excluded.reported_cost_usd, cost_source=excluded.cost_source, "
        "cost_currency=excluded.cost_currency, created_at=excluded.created_at, "
        "start_time=excluded.start_time, end_time=excluded.end_time, "
        "activity_at=excluded.activity_at, reasoning_effort=excluded.reasoning_effort, "
        "metadata_json=excluded.metadata_json",
        values,
    )

    stored_id = values[0]
    connection.execute(
        "DELETE FROM token_events WHERE provider = ? AND session_id = ?",
        (provider, stored_id),
    )
    for index, event in enumerate(session.events):
        (
            event_cost,
            event_cached_estimate,
            event_uncached_cost,
            event_savings,
            event_reported_cost,
            source,
            currency,
        ) = _event_cost(event)
        event_model = str(event.model or session.model or "unknown").strip() or "unknown"
        event_timestamp = _timestamp_text(event.timestamp) or values[1]
        usage = event.usage
        connection.execute(
            """INSERT INTO token_events (
                session_id, step_index, timestamp, timestamp_missing, model, input_tokens,
                cached_input_tokens, output_tokens, cache_write_tokens,
                reasoning_output_tokens, total_tokens, cost_usd, provider,
                usage_semantics_version, cache_write_mode, cache_write_5m_tokens,
                cache_write_1h_tokens, event_id, cost_cached_estimate_usd,
                cost_uncached_usd, savings_usd,
                reported_cost_usd, cost_source, cost_currency, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stored_id,
                index,
                event_timestamp,
                int(event.timestamp is None),
                event_model,
                usage.input_tokens,
                usage.cached_input_tokens,
                usage.output_tokens,
                usage.cache_write_tokens,
                usage.reasoning_output_tokens,
                usage.total_tokens,
                event_cost,
                provider,
                2,
                "additive",
                usage.cache_write_5m_tokens,
                usage.cache_write_1h_tokens,
                _event_id(provider, stored_id, index, event),
                event_cached_estimate,
                event_uncached_cost,
                event_savings,
                event_reported_cost,
                source,
                currency,
                _metadata_json(event.metadata, _EVENT_METADATA_KEYS),
            ),
        )
    return True


def write_usage_sessions(
    provider: str,
    sessions: Iterable[UsageSession],
    db_path: str | Path | None = None,
) -> int:
    """Atomically upsert full session snapshots and replace their event rows.

    Replaying a snapshot is idempotent. Replacing its event list also removes
    stale tail events after a corrected or shorter transcript parse.
    """
    canonical = _canonical_provider(provider)
    prepared = list(sessions)
    if not prepared:
        return 0
    for session in prepared:
        session_provider = _canonical_provider(session.provider or session.tool)
        if session_provider != canonical:
            raise ValueError(
                f"session provider {session_provider!r} does not match writer provider {canonical!r}"
            )
        if not str(session.id).strip():
            raise ValueError("usage session id must be non-empty")

    path = resolve_db_path(db_path)
    ensure_schema(path)
    for attempt in range(_WRITE_ATTEMPTS):
        connection = _connect_read_write(path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            written = 0
            for session in prepared:
                written += int(_upsert_session(connection, canonical, session))
            connection.commit()
            return written
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            locked = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
            if not locked or attempt + 1 >= _WRITE_ATTEMPTS:
                raise
            time.sleep(0.05 * (2 ** attempt))
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


def find_event_owners(
    provider: str,
    event_ids: Iterable[str],
    db_path: str | Path | None = None,
) -> dict[str, str]:
    """Map stored event IDs to the (native) session ID that currently owns them.

    Read-only and additive. Writers use it to avoid counting the same provider
    response under a second session, such as when a resumed transcript copies
    earlier history. An absent or unreadable database yields an empty mapping.
    """
    canonical = _canonical_provider(provider)
    wanted = sorted({str(value) for value in event_ids if value})
    path = resolve_db_path(db_path)
    if not wanted or not path.is_file():
        return {}
    owners: dict[str, str] = {}
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_read_only(path)
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in connection.execute(
                "SELECT event_id, session_id FROM token_events "
                f"WHERE provider = ? AND event_id IN ({marks})",
                (canonical, *chunk),
            ):
                owners.setdefault(str(row["event_id"]), _original_id(canonical, str(row["session_id"])))
    except sqlite3.Error:
        return {}
    finally:
        if connection is not None:
            connection.close()
    return owners


__all__ = [
    "DB_PATH_ENV_VAR",
    "DEFAULT_DB_RELATIVE_PATH",
    "ensure_schema",
    "find_event_owners",
    "get_usage_store_status",
    "is_provider_capture_enabled",
    "mark_provider_capture_enabled",
    "read_usage_sessions",
    "resolve_db_path",
    "write_usage_sessions",
]
