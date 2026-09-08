"""Provider-neutral data contracts for usage extraction.

Parser implementations should translate their native records into these small
dataclasses.  Aggregation and API layers can then work with the same shape for
Codex, Antigravity, Claude Code, and future providers without knowing how a
provider stores telemetry.

The ``to_legacy_dict`` helpers intentionally retain the field names currently
used by the dashboard.  This makes the contract usable incrementally while
the existing dictionary-based parsers are migrated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


def _non_negative_int(value: Any) -> int:
    """Convert a value to a non-negative integer without leaking bad input."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _timestamp(value: Any) -> datetime | None:
    """Parse common epoch/ISO values, returning an aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
                else value.astimezone(timezone.utc))
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 1e11:
                seconds /= 1000.0
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        if seconds > 1e11:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
        return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc))
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(slots=True)
class TokenUsage:
    """Normalized token counts for one call or an entire session.

    ``input_tokens`` is the total input count.  ``cached_input_tokens`` is the
    cache-read portion and ``uncached_input_tokens`` is derived from the two.
    ``cache_read_tokens`` and ``cache_write_tokens`` retain provider-specific
    cache accounting (not every provider reports cache writes).
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        self.input_tokens = _non_negative_int(self.input_tokens)
        self.cached_input_tokens = min(self.input_tokens, _non_negative_int(self.cached_input_tokens))
        self.output_tokens = _non_negative_int(self.output_tokens)
        self.reasoning_output_tokens = _non_negative_int(self.reasoning_output_tokens)
        self.cache_write_tokens = _non_negative_int(self.cache_write_tokens)
        self.cache_read_tokens = (
            self.cached_input_tokens if self.cache_read_tokens is None
            else min(self.input_tokens, _non_negative_int(self.cache_read_tokens))
        )
        self.total_tokens = _non_negative_int(self.total_tokens)
        if self.total_tokens == 0:
            self.total_tokens = self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def uncached_input(self) -> int:
        """Short alias used by the existing dashboard vocabulary."""
        return self.uncached_input_tokens

    @property
    def total_input(self) -> int:
        return self.input_tokens

    @property
    def cached_input(self) -> int:
        return self.cached_input_tokens

    @property
    def output(self) -> int:
        return self.output_tokens

    @property
    def reasoning_output(self) -> int:
        return self.reasoning_output_tokens

    @classmethod
    def from_legacy_dict(cls, value: Mapping[str, Any] | None) -> "TokenUsage":
        value = value or {}
        input_tokens = value.get("input_tokens", value.get("total_input", 0))
        cached = value.get("cached_input_tokens", value.get("cached_input", value.get("cache_read_tokens", 0)))
        output = value.get("output_tokens", value.get("output", 0))
        reasoning = value.get("reasoning_output_tokens", value.get("reasoning_output", 0))
        return cls(
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output,
            reasoning_output_tokens=reasoning,
            total_tokens=value.get("total_tokens", 0),
            cache_read_tokens=value.get("cache_read_tokens"),
            cache_write_tokens=value.get("cache_write_tokens", value.get("cache_creation_tokens", 0)),
        )

    def to_legacy_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens or 0,
            "cache_write_tokens": self.cache_write_tokens,
        }

    as_dict = to_legacy_dict
    from_dict = from_legacy_dict


@dataclass(slots=True)
class CostEstimate:
    """Cost values and their provenance, represented in USD by default."""

    cached_usd: Decimal | float | int = Decimal("0")
    uncached_usd: Decimal | float | int = Decimal("0")
    savings_usd: Decimal | float | int = Decimal("0")
    reported_usd: Decimal | float | int | None = None
    currency: str = "USD"
    source: str = "estimated"

    def __post_init__(self) -> None:
        self.cached_usd = _decimal(self.cached_usd) or Decimal("0")
        self.uncached_usd = _decimal(self.uncached_usd) or Decimal("0")
        self.savings_usd = _decimal(self.savings_usd) or Decimal("0")
        self.reported_usd = _decimal(self.reported_usd)
        self.currency = str(self.currency or "USD").upper()
        self.source = str(self.source or "estimated")

    @property
    def total_usd(self) -> Decimal:
        return self.reported_usd if self.reported_usd is not None else self.cached_usd

    # Names used by the current API, exposed as read-only aliases during the
    # migration to the typed contract.
    @property
    def cost_cached_usd(self) -> Decimal:
        return self.cached_usd

    @property
    def cost_uncached_usd(self) -> Decimal:
        return self.uncached_usd

    @property
    def reported_cost_usd(self) -> Decimal | None:
        return self.reported_usd

    @classmethod
    def from_legacy_dict(cls, value: Mapping[str, Any] | None) -> "CostEstimate":
        value = value or {}
        return cls(
            cached_usd=value.get("cost_cached_usd", value.get("est_cost_cached_usd", 0)),
            uncached_usd=value.get("cost_uncached_usd", value.get("est_cost_uncached_usd", 0)),
            savings_usd=value.get("savings_usd", value.get("est_savings_usd", 0)),
            reported_usd=value.get("reported_cost_usd"),
            currency=value.get("currency", "USD"),
            source=value.get("cost_source", "estimated"),
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        # Floats preserve the JSON/API behavior of the current dashboard.
        return {
            # The dashboard treats cost_cached_usd as the actual payable cost.
            # Prefer a provider-reported amount when one is available.
            "cost_cached_usd": float(self.total_usd),
            "cost_uncached_usd": float(self.uncached_usd),
            "savings_usd": float(self.savings_usd),
            "total_cost_usd": float(self.total_usd),
            "reported_cost_usd": float(self.reported_usd) if self.reported_usd is not None else None,
            "currency": self.currency,
            "cost_source": self.source,
        }

    as_dict = to_legacy_dict
    from_dict = from_legacy_dict


@dataclass(slots=True)
class UsageEvent:
    """One provider usage observation, normally corresponding to one call."""

    timestamp: datetime | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str | None = None
    cost: CostEstimate | None = None
    event_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.timestamp = _timestamp(self.timestamp)
        if not isinstance(self.usage, TokenUsage):
            self.usage = TokenUsage.from_legacy_dict(self.usage)  # type: ignore[arg-type]
        if self.cost is not None and not isinstance(self.cost, CostEstimate):
            self.cost = CostEstimate.from_legacy_dict(self.cost)  # type: ignore[arg-type]
        self.model = str(self.model).strip() if self.model is not None and str(self.model).strip() else None
        self.event_id = str(self.event_id) if self.event_id is not None else None
        self.metadata = dict(self.metadata or {})

    @property
    def tokens(self) -> TokenUsage:
        """Alias for callers that use ``event.tokens`` terminology."""
        return self.usage

    @property
    def token_usage(self) -> TokenUsage:
        return self.usage

    @classmethod
    def from_legacy_dict(cls, value: Mapping[str, Any]) -> "UsageEvent":
        return cls(
            timestamp=value.get("timestamp", value.get("created_at", value.get("start_time"))),
            usage=TokenUsage.from_legacy_dict(value),
            model=value.get("model"),
            cost=CostEstimate.from_legacy_dict(value) if any(key in value for key in ("cost_cached_usd", "cost_uncached_usd", "savings_usd", "reported_cost_usd")) else None,
            event_id=value.get("event_id", value.get("id")),
            metadata=value.get("metadata", {}),
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = self.usage.to_legacy_dict()
        if self.timestamp is not None:
            result["timestamp"] = self.timestamp.isoformat()
        if self.model is not None:
            result["model"] = self.model
        if self.event_id is not None:
            result["event_id"] = self.event_id
        if self.cost is not None:
            result.update(self.cost.to_legacy_dict())
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result

    as_dict = to_legacy_dict
    from_dict = from_legacy_dict


@dataclass(slots=True)
class UsageSession:
    """A normalized session containing aggregate usage and per-call events."""

    id: str
    tool: str
    model: str | None = None
    title: str | None = None
    created_at: datetime | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    activity_at: datetime | None = None
    reasoning_effort: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    events: list[UsageEvent] = field(default_factory=list)
    cost: CostEstimate | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    call_count: int = 0

    def __post_init__(self) -> None:
        self.id = str(self.id)
        self.tool = str(self.tool or self.provider or "").strip()
        self.provider = str(self.provider or self.tool).strip() or None
        self.model = str(self.model).strip() if self.model is not None and str(self.model).strip() else None
        self.title = str(self.title) if self.title is not None else None
        self.created_at = _timestamp(self.created_at)
        self.start_time = _timestamp(self.start_time)
        self.end_time = _timestamp(self.end_time)
        self.activity_at = _timestamp(self.activity_at)
        self.reasoning_effort = str(self.reasoning_effort) if self.reasoning_effort is not None else None
        if not isinstance(self.usage, TokenUsage):
            self.usage = TokenUsage.from_legacy_dict(self.usage)  # type: ignore[arg-type]
        self.events = [
            event if isinstance(event, UsageEvent) else UsageEvent.from_legacy_dict(event)
            for event in (self.events or [])
            if isinstance(event, UsageEvent) or isinstance(event, Mapping)
        ]  # type: ignore[arg-type]
        self.call_count = _non_negative_int(self.call_count)
        if self.events:
            # Some sources retain an authoritative call count even when only
            # a subset of historical calls has timestamped token events.
            self.call_count = max(self.call_count, len(self.events))
            # Extractors may provide only events.  Derive the session aggregate
            # in that case so all consumers see useful dashboard metrics.
            if self.usage.total_tokens == 0 and any(event.usage.total_tokens for event in self.events):
                self.usage = TokenUsage(
                    input_tokens=sum(event.usage.input_tokens for event in self.events),
                    cached_input_tokens=sum(event.usage.cached_input_tokens for event in self.events),
                    output_tokens=sum(event.usage.output_tokens for event in self.events),
                    reasoning_output_tokens=sum(event.usage.reasoning_output_tokens for event in self.events),
                    total_tokens=sum(event.usage.total_tokens for event in self.events),
                    cache_read_tokens=sum(event.usage.cache_read_tokens or 0 for event in self.events),
                    cache_write_tokens=sum(event.usage.cache_write_tokens for event in self.events),
                )
            if self.cost is None and any(event.cost is not None for event in self.events):
                event_costs = [event.cost for event in self.events if event.cost is not None]
                self.cost = CostEstimate(
                    cached_usd=sum((cost.cached_usd for cost in event_costs), Decimal("0")),
                    uncached_usd=sum((cost.uncached_usd for cost in event_costs), Decimal("0")),
                    savings_usd=sum((cost.savings_usd for cost in event_costs), Decimal("0")),
                    reported_usd=(
                        sum((cost.reported_usd for cost in event_costs if cost.reported_usd is not None), Decimal("0"))
                        if any(cost.reported_usd is not None for cost in event_costs) else None
                    ),
                    currency=event_costs[0].currency,
                    source="reported" if any(cost.reported_usd is not None for cost in event_costs) else "estimated",
                )
        if self.cost is not None and not isinstance(self.cost, CostEstimate):
            self.cost = CostEstimate.from_legacy_dict(self.cost)  # type: ignore[arg-type]
        self.metadata = dict(self.metadata or {})

    @property
    def usage_events(self) -> list[UsageEvent]:
        """Compatibility alias matching the current parser field name."""
        return self.events

    @property
    def session_id(self) -> str:
        return self.id

    @property
    def tokens(self) -> TokenUsage:
        return self.usage

    @property
    def token_usage(self) -> TokenUsage:
        return self.usage

    @property
    def source(self) -> str | None:
        """Provider alias for code that calls the origin a source."""
        return self.provider

    @classmethod
    def from_legacy_dict(cls, value: Mapping[str, Any]) -> "UsageSession":
        raw_events = value.get("usage_events", value.get("events", []))
        return cls(
            id=value.get("id", value.get("session_id", "")),
            tool=value.get("tool", value.get("provider", "")),
            provider=value.get("provider"),
            model=value.get("model"),
            title=value.get("title", value.get("name")),
            created_at=value.get("created_at"),
            start_time=value.get("start_time"),
            end_time=value.get("end_time"),
            activity_at=value.get("activity_at"),
            reasoning_effort=value.get("reasoning_effort"),
            usage=TokenUsage.from_legacy_dict(value),
            events=[UsageEvent.from_legacy_dict(event) for event in raw_events if isinstance(event, Mapping)],
            cost=CostEstimate.from_legacy_dict(value) if any(key in value for key in ("cost_cached_usd", "cost_uncached_usd", "savings_usd", "reported_cost_usd")) else None,
            metadata=value.get("metadata", {}),
            call_count=value.get("call_count", len(raw_events) if isinstance(raw_events, list) else 0),
        )

    def to_legacy_dict(self, *, include_events: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "tool": self.tool,
            "model": self.model,
            "title": self.title,
            "created_at": _iso(self.created_at),
            "start_time": _iso(self.start_time),
            "end_time": _iso(self.end_time),
            "activity_at": _iso(self.activity_at),
            "reasoning_effort": self.reasoning_effort,
            "call_count": self.call_count,
            "uncached_input": self.usage.uncached_input_tokens,
            "cached_input": self.usage.cached_input_tokens,
            "total_input": self.usage.input_tokens,
            "output": self.usage.output_tokens,
            "reasoning_output": self.usage.reasoning_output_tokens,
            "total_tokens": self.usage.total_tokens,
            "cache_hit_rate": round(self.usage.cached_input_tokens / self.usage.input_tokens * 100.0, 2) if self.usage.input_tokens else 0.0,
        }
        if self.cost is not None:
            result.update(self.cost.to_legacy_dict())
        if include_events:
            result["usage_events"] = [event.to_legacy_dict() for event in self.events]
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        if self.provider and self.provider != self.tool:
            result["provider"] = self.provider
        return result

    as_dict = to_legacy_dict
    from_dict = from_legacy_dict


@runtime_checkable
class UsageSource(Protocol):
    """Interface implemented by each provider-specific usage extractor."""

    key: str
    aliases: Sequence[str]

    def extract_sessions(self, root: str | Path | None = None) -> Sequence[UsageSession]:
        """Extract normalized sessions from the provider's local data."""
        ...


# Descriptive aliases make the public contract easier to discover without
# coupling callers to one particular naming convention.
UsageCost = CostEstimate
UsageCostEstimate = CostEstimate
Usage = TokenUsage


# Friendly serializer aliases for callers migrating from dictionary parsers.
def token_usage_to_legacy(usage: TokenUsage) -> dict[str, int]:
    return usage.to_legacy_dict()


def usage_event_to_legacy(event: UsageEvent) -> dict[str, Any]:
    return event.to_legacy_dict()


def usage_session_to_legacy(session: UsageSession, *, include_events: bool = True) -> dict[str, Any]:
    return session.to_legacy_dict(include_events=include_events)
