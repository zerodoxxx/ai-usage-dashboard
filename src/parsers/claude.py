"""Parser for Claude Code session transcripts.

Claude Code stores one JSONL transcript per session beneath ``~/.claude``.
Assistant records contain the model response and its provider usage object.
The same response can appear in several transcript records while tool
orchestration is persisted, so message IDs make usage events idempotent.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..pricing import calculate_cost_strict
from .contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession, _timestamp
from .file_cache import ParsedFileCache

logger = logging.getLogger(__name__)
def _pricing_provider(model: str) -> str:
    """Return the billing provider for a model recorded in Claude logs."""
    return "deepseek" if model.casefold().startswith("deepseek") else "claude"


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _clean_title(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] if text else ""


def _cache_creation_components(usage: dict[str, Any]) -> tuple[int, int, int]:
    """Return total, five-minute, and one-hour cache-write tokens."""
    direct = _as_int(usage.get("cache_creation_input_tokens"))
    nested = usage.get("cache_creation")
    if not isinstance(nested, dict):
        nested = {}
    writes_5m = _as_int(nested.get("ephemeral_5m_input_tokens"))
    writes_1h = _as_int(nested.get("ephemeral_1h_input_tokens"))
    if direct and not (writes_5m or writes_1h):
        writes_5m = direct
    return max(direct, writes_5m + writes_1h), writes_5m, writes_1h


def _usage_event(
    record: dict[str, Any], usage: dict[str, Any], model: str | None, ordinal: int
) -> UsageEvent | None:
    """Normalize one Claude API usage observation."""
    base_input = _as_int(usage.get("input_tokens"))
    cache_read = _as_int(usage.get("cache_read_input_tokens"))
    cache_write, cache_write_5m, cache_write_1h = _cache_creation_components(usage)
    output = _as_int(usage.get("output_tokens"))
    reasoning = _as_int(
        usage.get("reasoning_output_tokens")
        or (usage.get("output_tokens_details") or {}).get("thinking_tokens")
    )
    if not any((base_input, cache_read, cache_write, output, reasoning)):
        return None

    # Cache writes are provider-specific and remain separate from input_tokens
    # so the dashboard's input and cache-hit metrics stay comparable.
    token_usage = TokenUsage(
        input_tokens=base_input + cache_read,
        cached_input_tokens=cache_read,
        output_tokens=output,
        reasoning_output_tokens=reasoning,
        total_tokens=base_input + cache_read + cache_write + output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cache_write_5m_tokens=cache_write_5m,
        cache_write_1h_tokens=cache_write_1h,
    )

    cost: CostEstimate | None = None
    if model:
        resolved = calculate_cost_strict(
            model,
            base_input,
            cache_read,
            output,
            provider=_pricing_provider(model),
            cache_write_5m=cache_write_5m,
            cache_write_1h=cache_write_1h,
            timestamp=record.get("timestamp"),
        )
        if resolved.get("status") == "known":
            cost = CostEstimate(
                cached_usd=resolved.get("cost_cached_usd") or 0.0,
                uncached_usd=resolved.get("cost_uncached_usd") or 0.0,
                savings_usd=resolved.get("savings_usd") or 0.0,
                source="estimated",
            )

    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    event_id = message.get("id") or record.get("uuid") or f"event-{ordinal}"
    return UsageEvent(
        timestamp=record.get("timestamp"),
        usage=token_usage,
        model=model,
        cost=cost,
        event_id=event_id,
        metadata={
            "cache_write_tokens": cache_write,
            "cache_write_5m_tokens": cache_write_5m,
            "cache_write_1h_tokens": cache_write_1h,
        },
    )


def _session_title(record: dict[str, Any]) -> str:
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = _text_content(message.get("content", record.get("content")))
    if not content or "<local-command-stdout>" in content or "<local-command-caveat>" in content:
        return ""
    return _clean_title(content)


def _sum_event_usage(events: list[UsageEvent]) -> TokenUsage:
    """Rebuild usage after grouping or removing copied response observations."""
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
    )


def _parse_session_file_uncached(path: Path) -> tuple[UsageSession | None, bool]:
    events: list[UsageEvent] = []
    responses: dict[str, tuple[Any, dict[str, Any], int]] = {}
    ancestors: dict[str, tuple[Any, str, Any]] = {}
    models: Counter[str] = Counter()
    session_id: str | None = None
    title = ""
    first_timestamp: Any = None
    last_timestamp: Any = None
    cwd: str | None = None
    git_branch: str | None = None
    is_subagent = "subagents" in path.parts

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict) or (record.get("isSidechain") and not is_subagent):
                    continue

                session_id = session_id or (
                    path.stem
                    if is_subagent
                    else str(record.get("sessionId") or record.get("session_id") or path.stem)
                )
                cwd = cwd or (str(record["cwd"]) if record.get("cwd") else None)
                git_branch = git_branch or (
                    str(record["gitBranch"]) if record.get("gitBranch") else None
                )
                timestamp = record.get("timestamp")
                if timestamp is not None:
                    first_timestamp = first_timestamp if first_timestamp is not None else timestamp
                    last_timestamp = timestamp

                uuid = record.get("uuid")
                if uuid:
                    ancestors[str(uuid)] = (
                        record.get("parentUuid"), str(record.get("type") or ""), timestamp
                    )

                if record.get("type") == "ai-title" or record.get("aiTitle"):
                    ai_title = _clean_title(str(record.get("aiTitle") or record.get("title") or ""))
                    if ai_title:
                        title = ai_title
                elif record.get("type") == "user" and not title and not record.get("isMeta"):
                    title = _session_title(record)

                message = record.get("message")
                if not isinstance(message, dict):
                    message = {}
                usage = message.get("usage") or record.get("usage")
                if not isinstance(usage, dict):
                    continue
                if record.get("type") not in (None, "assistant") and message.get("role") != "assistant":
                    continue

                message_id = message.get("id") or record.get("uuid")
                key = str(message_id) if message_id is not None else f"{path.resolve()}:event-{line_number}"
                first_parent = responses[key][0] if key in responses else record.get("parentUuid")
                # Usage is cumulative across blocks. Keep the final observation
                # and completion timestamp, while preserving the first ancestor.
                responses[key] = (first_parent, record, line_number)
    except (OSError, UnicodeError) as exc:
        logger.debug("Unable to read Claude Code session %s: %s", path, exc)
        return None, False

    for key, (parent, record, ordinal) in responses.items():
        message = record.get("message")
        if not isinstance(message, dict):
            message = {}
        usage = message.get("usage") or record.get("usage")
        model_value = message.get("model") or record.get("model")
        model = str(model_value).strip() if model_value else None
        event = _usage_event(record, usage, model, ordinal)
        if event is None:
            continue
        event.event_id = key
        start = None
        visited: set[str] = set()
        while parent is not None and str(parent) not in visited:
            parent_key = str(parent)
            visited.add(parent_key)
            ancestor = ancestors.get(parent_key)
            if ancestor is None:
                break
            parent, kind, timestamp = ancestor
            if kind == "user":
                start = _timestamp(timestamp)
                break
        event.metadata.update({
            "tps_duration_seconds": (
                (event.timestamp - start).total_seconds()
                if event.timestamp is not None and start is not None else None
            ),
            "tps_output_tokens": event.usage.output_tokens,
            "tps_trustworthy": bool(message.get("stop_reason")),
        })
        events.append(event)
        if model:
            models[model] += 1

    result: UsageSession | None
    if not events:
        result = None
    else:
        model = models.most_common(1)[0][0] if models else None
        fallback_title = (
            f"Claude Code Subagent {path.stem[:8]}"
            if is_subagent
            else f"Claude Code Session {(session_id or path.stem)[:8]}"
        )
        result = UsageSession(
            id=session_id or path.stem,
            tool="claude-code",
            provider="claude",
            model=model,
            title=title or fallback_title,
            created_at=first_timestamp,
            start_time=first_timestamp,
            end_time=last_timestamp,
            activity_at=last_timestamp,
            usage=_sum_event_usage(events),
            events=events,
            metadata={"cwd": cwd, "git_branch": git_branch} if cwd or git_branch else {},
            call_count=len(events),
        )

    return result, True


def _refresh_session_prices(session: UsageSession) -> None:
    """Refresh estimated event and session costs from the current catalog."""
    for event in session.events:
        if event.cost is not None and (
            event.cost.source == "reported" or event.cost.reported_usd is not None
        ):
            continue
        model = event.model or session.model
        usage = event.usage
        resolved = calculate_cost_strict(
            model,
            usage.uncached_input_tokens,
            usage.cached_input_tokens,
            usage.output_tokens,
            provider=_pricing_provider(model or ""),
            cache_write_5m=usage.cache_write_5m_tokens,
            cache_write_1h=usage.cache_write_1h_tokens,
            timestamp=event.timestamp,
        )
        if resolved.get("status") == "known":
            event.cost = CostEstimate(
                cached_usd=resolved.get("cost_cached_usd") or 0.0,
                uncached_usd=resolved.get("cost_uncached_usd") or 0.0,
                savings_usd=resolved.get("savings_usd") or 0.0,
                source="estimated",
            )
        else:
            event.cost = None

    if session.cost is not None and (
        session.cost.source == "reported" or session.cost.reported_usd is not None
    ):
        return
    if not session.events or any(event.cost is None for event in session.events):
        session.cost = None
        return

    event_costs = [event.cost for event in session.events if event.cost is not None]
    reported_costs = [cost for cost in event_costs if cost.reported_usd is not None]
    all_reported = len(reported_costs) == len(event_costs)
    has_reported = bool(reported_costs)
    session.cost = CostEstimate(
        cached_usd=sum((cost.total_usd for cost in event_costs), Decimal("0")),
        uncached_usd=sum((cost.uncached_usd for cost in event_costs), Decimal("0")),
        savings_usd=sum((cost.savings_usd for cost in event_costs), Decimal("0")),
        reported_usd=(
            sum((cost.reported_usd for cost in reported_costs if cost.reported_usd is not None), Decimal("0"))
            if all_reported else None
        ),
        currency=event_costs[0].currency,
        source="reported" if all_reported else "mixed" if has_reported else "estimated",
    )


_SESSION_PARSE_CACHE: ParsedFileCache[UsageSession | None] = ParsedFileCache(max_entries=1024)


def _parse_session_file(path: Path) -> UsageSession | None:
    """Parse a Claude transcript from the cache and refresh current pricing."""
    session = _SESSION_PARSE_CACHE.parse(path, _parse_session_file_uncached)
    if session is not None:
        _refresh_session_prices(session)
    return session


def _session_files(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    if base_dir.is_file() and base_dir.suffix == ".jsonl":
        return [base_dir]
    files: set[Path] = set()
    projects_dir = base_dir / "projects"
    if projects_dir.is_dir():
        files.update(projects_dir.rglob("*.jsonl"))
    # Also discover session jsonl files directly in base_dir or sessions/
    for p in base_dir.glob("*.jsonl"):
        if p.name != "history.jsonl":
            files.add(p)
    sessions_dir = base_dir / "sessions"
    if sessions_dir.is_dir():
        files.update(sessions_dir.rglob("*.jsonl"))
    return sorted(files)


class ClaudeCodeSource:
    """Provider adapter for Claude Code's local JSONL transcripts."""

    key = "claude-code"
    provider = "claude"
    aliases = ("claude", "anthropic", "cc")
    default_source_path = Path.home() / ".claude"
    default_root = default_source_path
    default_path = default_source_path

    def extract_sessions(self, root: str | Path | None = None) -> list[UsageSession]:
        if root is not None:
            base_dir = Path(root).expanduser()
        elif os.environ.get("CLAUDE_CONFIG_DIR"):
            base_dir = Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()
        elif os.environ.get("CLAUDE_DIR"):
            base_dir = Path(os.environ["CLAUDE_DIR"]).expanduser()
        else:
            base_dir = self.default_source_path
        session_files = _session_files(base_dir)
        _SESSION_PARSE_CACHE.retain_paths(session_files)
        sessions = [
            session
            for path in session_files
            if (session := _parse_session_file(path)) is not None
        ]
        final_events: dict[str, UsageEvent] = {}
        for session in sessions:
            for event in session.events:
                if event.event_id is None:
                    continue
                previous = final_events.get(event.event_id)
                if previous is None or (
                    event.timestamp is not None
                    and (previous.timestamp is None or event.timestamp > previous.timestamp)
                ) or (
                    event.timestamp == previous.timestamp
                    and previous.metadata.get("tps_duration_seconds") is None
                    and event.metadata.get("tps_duration_seconds") is not None
                ):
                    final_events[event.event_id] = event
        unique_sessions: list[UsageSession] = []
        for session in sessions:
            retained = [
                event for event in session.events
                if event.event_id is None or final_events[event.event_id] is event
            ]
            if not retained:
                continue
            if len(retained) != len(session.events):
                session = replace(
                    session, events=retained, usage=_sum_event_usage(retained),
                    cost=None, call_count=len(retained),
                )
            unique_sessions.append(session)
        sessions = unique_sessions
        sessions.sort(
            key=lambda session: str(session.activity_at or session.created_at or session.id),
            reverse=True,
        )
        return sessions


ClaudeUsageSource = ClaudeCodeSource


def parse_claude_code_usage(root: str | Path | None = None) -> list[UsageSession]:
    return ClaudeCodeSource().extract_sessions(root)


__all__ = ["ClaudeCodeSource", "ClaudeUsageSource", "parse_claude_code_usage"]
