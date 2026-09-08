"""Parser for Claude Code session transcripts.

Claude Code stores one JSONL transcript per session beneath ``~/.claude``.
Assistant records contain the model response and its provider usage object.
The same response can appear in several transcript records while tool
orchestration is persisted, so message IDs make usage events idempotent.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

from ..pricing import calculate_cost_strict
from .contracts import CostEstimate, TokenUsage, UsageEvent, UsageSession

logger = logging.getLogger(__name__)
_CLAUDE_PARSE_CACHE: dict[tuple[str, int, int], UsageSession | None] = {}


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


def _cache_creation_tokens(usage: dict[str, Any]) -> int:
    direct = _as_int(usage.get("cache_creation_input_tokens"))
    if direct:
        return direct
    nested = usage.get("cache_creation")
    if not isinstance(nested, dict):
        return 0
    return _as_int(nested.get("ephemeral_5m_input_tokens")) + _as_int(
        nested.get("ephemeral_1h_input_tokens")
    )


def _usage_event(
    record: dict[str, Any], usage: dict[str, Any], model: str | None, ordinal: int
) -> UsageEvent | None:
    """Normalize one Claude API usage observation."""
    base_input = _as_int(usage.get("input_tokens"))
    cache_read = _as_int(usage.get("cache_read_input_tokens"))
    cache_write = _cache_creation_tokens(usage)
    output = _as_int(usage.get("output_tokens"))
    reasoning = _as_int(usage.get("reasoning_output_tokens"))
    if not any((base_input, cache_read, cache_write, output, reasoning)):
        return None

    # Cache writes are provider-specific and remain separate from input_tokens
    # so the dashboard's input and cache-hit metrics stay comparable.
    token_usage = TokenUsage(
        input_tokens=base_input + cache_read,
        cached_input_tokens=cache_read,
        output_tokens=output,
        reasoning_output_tokens=reasoning,
        total_tokens=base_input + cache_read + output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )

    cost: CostEstimate | None = None
    if model:
        resolved = calculate_cost_strict(
            model,
            base_input,
            cache_read,
            output,
            provider=_pricing_provider(model),
            cache_write=cache_write,
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
        metadata={"cache_write_tokens": cache_write},
    )


def _session_title(record: dict[str, Any]) -> str:
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = _text_content(message.get("content", record.get("content")))
    if not content or "<local-command-stdout>" in content or "<local-command-caveat>" in content:
        return ""
    return _clean_title(content)


def _parse_session_file(path: Path) -> UsageSession | None:
    try:
        stat = path.stat()
        cache_key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        if cache_key in _CLAUDE_PARSE_CACHE:
            return copy.deepcopy(_CLAUDE_PARSE_CACHE[cache_key])
    except OSError:
        return None

    events: list[UsageEvent] = []
    seen_message_ids: set[str] = set()
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

                if record.get("type") == "user" and not title and not record.get("isMeta"):
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
                if message_id is not None and str(message_id) in seen_message_ids:
                    continue
                if message_id is not None:
                    seen_message_ids.add(str(message_id))

                model_value = message.get("model") or record.get("model")
                model = str(model_value).strip() if model_value else None
                event = _usage_event(record, usage, model, line_number)
                if event is None:
                    continue
                events.append(event)
                if model:
                    models[model] += 1
    except (OSError, UnicodeError) as exc:
        logger.debug("Unable to read Claude Code session %s: %s", path, exc)
        return None

    result: UsageSession | None
    if not events:
        result = None
    else:
        model = models.most_common(1)[0][0] if models else None
        result = UsageSession(
            id=session_id or path.stem,
            tool="claude-code",
            provider="claude",
            model=model,
            title=title or f"Claude Code Session {(session_id or path.stem)[:8]}",
            created_at=first_timestamp,
            start_time=first_timestamp,
            end_time=last_timestamp,
            activity_at=last_timestamp,
            usage=TokenUsage(
                input_tokens=sum(event.usage.input_tokens for event in events),
                cached_input_tokens=sum(event.usage.cached_input_tokens for event in events),
                output_tokens=sum(event.usage.output_tokens for event in events),
                reasoning_output_tokens=sum(event.usage.reasoning_output_tokens for event in events),
                total_tokens=sum(event.usage.total_tokens for event in events),
                cache_read_tokens=sum(event.usage.cache_read_tokens or 0 for event in events),
                cache_write_tokens=sum(event.usage.cache_write_tokens for event in events),
            ),
            events=events,
            metadata={"cwd": cwd, "git_branch": git_branch} if cwd or git_branch else {},
            call_count=len(events),
        )

    _CLAUDE_PARSE_CACHE[cache_key] = copy.deepcopy(result)
    for old_key in list(_CLAUDE_PARSE_CACHE):
        if old_key != cache_key and old_key[0] == cache_key[0]:
            del _CLAUDE_PARSE_CACHE[old_key]
    return copy.deepcopy(result)


def _session_files(base_dir: Path) -> list[Path]:
    projects_dir = base_dir / "projects" if (base_dir / "projects").is_dir() else base_dir
    if not projects_dir.exists():
        return []
    # Subagent transcripts contain billable Claude calls of their own and are
    # intentionally included. Repeated wrapper records are deduplicated while
    # parsing each file by message ID.
    return sorted(projects_dir.rglob("*.jsonl"))


class ClaudeCodeSource:
    """Provider adapter for Claude Code's local JSONL transcripts."""

    key = "claude-code"
    provider = "claude"
    aliases = ("claude", "anthropic", "cc")
    default_source_path = Path.home() / ".claude"
    default_root = default_source_path
    default_path = default_source_path

    def extract_sessions(self, root: str | Path | None = None) -> list[UsageSession]:
        base_dir = Path(root).expanduser() if root is not None else self.default_source_path
        sessions = [
            session
            for path in _session_files(base_dir)
            if (session := _parse_session_file(path)) is not None
        ]
        sessions.sort(
            key=lambda session: str(session.activity_at or session.created_at or session.id),
            reverse=True,
        )
        return sessions


ClaudeUsageSource = ClaudeCodeSource


def parse_claude_code_usage(root: str | Path | None = None) -> list[UsageSession]:
    return ClaudeCodeSource().extract_sessions(root)


__all__ = ["ClaudeCodeSource", "ClaudeUsageSource", "parse_claude_code_usage"]
