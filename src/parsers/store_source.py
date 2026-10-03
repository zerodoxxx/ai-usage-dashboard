"""Usage sources that read exclusively from the shared SQLite usage store.

The dashboard never parses provider transcripts or logs at request time.
Writers (hooks, backfill scripts) import the provider parsers and persist
normalized sessions into ``src.usage_store``; these adapters only read them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .contracts import UsageSession


class StoreUsageSource:
    """Provider adapter backed by ``usage_store.read_usage_sessions``.

    A provider with no stored rows (or no database at all) yields an empty
    list. The ``root`` argument is accepted for ``UsageSource`` compatibility
    and ignored: the database location comes from ``AI_USAGE_DB_PATH`` or the
    shared default, or from ``usage_db_path`` given at construction.
    """

    def __init__(
        self,
        *,
        key: str,
        store_provider: str,
        session_tool: str | None = None,
        session_provider: str | None = None,
        aliases: Iterable[str] = (),
        usage_db_path: str | Path | None = None,
    ) -> None:
        self.key = key
        self.store_provider = store_provider
        self.provider = session_provider or key
        self.session_tool = session_tool or key
        self.aliases = tuple(aliases)
        self.usage_db_path = usage_db_path

    def extract_sessions(self, root: str | Path | None = None) -> list[UsageSession]:
        from src.usage_store import read_usage_sessions

        sessions = []
        for session in read_usage_sessions(self.store_provider, db_path=self.usage_db_path):
            if not isinstance(session, UsageSession):
                continue
            session.tool = self.session_tool
            session.provider = self.provider
            sessions.append(session)
        sessions.sort(
            key=lambda s: str(s.activity_at or s.created_at or s.id),
            reverse=True,
        )
        return sessions


def codex_store_source(usage_db_path: str | Path | None = None) -> StoreUsageSource:
    return StoreUsageSource(
        key="codex", store_provider="codex",
        aliases=("openai-codex", "codex-cli"), usage_db_path=usage_db_path,
    )


def antigravity_store_source(usage_db_path: str | Path | None = None) -> StoreUsageSource:
    return StoreUsageSource(
        key="antigravity", store_provider="antigravity",
        aliases=("agy", "antigravity-cli"), usage_db_path=usage_db_path,
    )


def claude_store_source(usage_db_path: str | Path | None = None) -> StoreUsageSource:
    return StoreUsageSource(
        key="claude-code", store_provider="claude-code",
        session_tool="claude-code", session_provider="claude",
        aliases=("claude", "anthropic", "cc"), usage_db_path=usage_db_path,
    )
