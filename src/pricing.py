"""Provider-aware model pricing and cost calculation helpers.

The public ``MODEL_PRICING``, ``get_pricing`` and ``calculate_cost`` symbols
are intentionally kept compatible with the original dashboard. New code
should use :class:`PricingCatalog` and ``resolve_pricing_strict`` so that an
unknown (or registered-but-unpriced) model is never accidentally charged at a
different model's rate.

Rates are USD per 1,000,000 tokens. Cache-write and cache-creation rates are
optional because providers use different names for that charge and many
providers do not charge for it.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, time, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Literal, Mapping
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from src.litellm_pricing import (
    LITELLM_ALLOWED_HOST,
    LITELLM_MAX_RESPONSE_BYTES,
    LITELLM_PRICING_URL,
    build_index,
    litellm_key_candidates,
    lookup,
    parse_feed,
    round_rate,
    validate_index,
)
from src.timezones import as_utc


# Offline fallback only; LiteLLM is the runtime source of truth.
# This is the legacy, provider-less representation consumed by the existing
# API and frontend. Keep its keys and the three standard rate fields stable.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Codex / OpenAI
    "gpt-6-astra": {"uncached_input": 10.0, "cached_input": 1.0, "output": 50.0},
    "gpt-6-sol": {"uncached_input": 2.00, "cached_input": 0.20, "output": 10.00},
    "gpt-6-luna": {"uncached_input": 0.10, "cached_input": 0.01, "output": 0.50},
    "gpt-5.6-luna": {"uncached_input": 0.20, "cached_input": 0.02, "output": 1.20},
    "gpt-5.6-sol": {"uncached_input": 4.00, "cached_input": 0.40, "output": 20.00},
    "gpt-5.6-terra": {"uncached_input": 2.00, "cached_input": 0.20, "output": 12.00},
    "gpt-5.5": {"uncached_input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-4o": {"uncached_input": 2.50, "cached_input": 1.25, "output": 10.00},
    "gpt-4o-mini": {"uncached_input": 0.15, "cached_input": 0.075, "output": 0.60},
    "o1": {"uncached_input": 15.00, "cached_input": 7.50, "output": 60.00},
    "o1-mini": {"uncached_input": 1.10, "cached_input": 0.55, "output": 4.40},
    "o3-mini": {"uncached_input": 1.10, "cached_input": 0.55, "output": 4.40},
    # Google Gemini / Antigravity (AGY)
    "Gemini 3.8 Flash (High)": {"uncached_input": 0.75, "cached_input": 0.075, "output": 3.75},
    "Gemini 2.5 Flash": {"uncached_input": 0.30, "cached_input": 0.075, "output": 2.50},
    "Gemini 2.5 Pro": {"uncached_input": 1.25, "cached_input": 0.3125, "output": 10.00},
    "Gemini 1.5 Flash": {"uncached_input": 0.075, "cached_input": 0.01875, "output": 0.30},
    "Gemini 1.5 Pro": {"uncached_input": 1.25, "cached_input": 0.3125, "output": 5.00},
    # Claude Code / Anthropic. Rates are USD per million tokens.
    "Claude Opus 5.5": {"uncached_input": 4.00, "cached_input": 0.20, "output": 20.00},
    "Claude Opus 5": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.8": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.7": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.6": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.5": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.1": {"uncached_input": 15.00, "cached_input": 1.50, "output": 75.00},
    "Claude Opus 4": {"uncached_input": 15.00, "cached_input": 1.50, "output": 75.00},
    "Claude Sonnet 5.5": {"uncached_input": 2.00, "cached_input": 0.20, "output": 10.00},
    "Claude Sonnet 5": {"uncached_input": 2.00, "cached_input": 0.20, "output": 10.00},
    "Claude Sonnet 4.6": {"uncached_input": 3.00, "cached_input": 0.30, "output": 15.00},
    "Claude Sonnet 4.5": {"uncached_input": 3.00, "cached_input": 0.30, "output": 15.00},
    "Claude Sonnet 4": {"uncached_input": 3.00, "cached_input": 0.30, "output": 15.00},
    "Claude Haiku 4.5": {"uncached_input": 1.00, "cached_input": 0.10, "output": 5.00},
    "Claude Haiku 3.5": {"uncached_input": 0.80, "cached_input": 0.08, "output": 4.00},
    # DeepSeek V4. The official API publishes peak and off-peak rates; the
    # static catalog uses the latest peak rates so usage is not understated.
    "deepseek-v4-flash": {"uncached_input": 0.30, "cached_input": 0.006, "output": 1.20},
    "deepseek-flash": {"uncached_input": 0.30, "cached_input": 0.006, "output": 1.20},
    "deepseek-v4-pro": {"uncached_input": 1.32, "cached_input": 0.044, "output": 3.96},
}

DEFAULT_MODEL = "gpt-5.6-luna"

# The old names remain available for callers that imported these internals.
_NORMALIZED_MAP: dict[str, str] = {k.casefold(): k for k in MODEL_PRICING}
_ALIASES: list[tuple[str, str]] = [
    ("3.8 flash", "Gemini 3.8 Flash (High)"), ("gemini-3.8-flash", "Gemini 3.8 Flash (High)"),
    ("gemini 3.8", "Gemini 3.8 Flash (High)"), ("2.5 pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro"), ("2.5 flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"), ("1.5 pro", "Gemini 1.5 Pro"),
    ("gemini-1.5-pro", "Gemini 1.5 Pro"), ("1.5 flash", "Gemini 1.5 Flash"),
    ("gemini-1.5-flash", "Gemini 1.5 Flash"), ("astra", "gpt-6-astra"),
    ("6-sol", "gpt-6-sol"), ("gpt6-sol", "gpt-6-sol"), ("gpt-6 sol", "gpt-6-sol"),
    ("gpt 6 sol", "gpt-6-sol"), ("6 sol", "gpt-6-sol"),
    ("6-luna", "gpt-6-luna"), ("gpt6-luna", "gpt-6-luna"), ("gpt-6 luna", "gpt-6-luna"),
    ("gpt 6 luna", "gpt-6-luna"), ("6 luna", "gpt-6-luna"),
    ("luna", "gpt-5.6-luna"), ("codex-auto-review", "gpt-5.6-luna"),
    ("gpt-reserve", "gpt-5.6-luna"),
    ("sol", "gpt-5.6-sol"), ("terra", "gpt-5.6-terra"),
    ("o3-mini", "o3-mini"), ("o3", "o3-mini"),
    ("o1-mini", "o1-mini"), ("o1-preview", "o1"), ("o1", "o1"),
    ("gpt-4o-mini", "gpt-4o-mini"), ("4o-mini", "gpt-4o-mini"),
    ("gpt-4o", "gpt-4o"), ("4o", "gpt-4o"),
    ("claude-opus-5-5", "Claude Opus 5.5"), ("claude-opus-5.5", "Claude Opus 5.5"),
    ("claude-opus-5-5-latest", "Claude Opus 5.5"), ("claude-opus-5.5-latest", "Claude Opus 5.5"),
    ("opus-5-5", "Claude Opus 5.5"), ("opus-5.5", "Claude Opus 5.5"),
    ("opus 5.5", "Claude Opus 5.5"), ("claude opus 5.5", "Claude Opus 5.5"),
    ("claude-5-5-opus", "Claude Opus 5.5"), ("claude-5.5-opus", "Claude Opus 5.5"),
    ("claude-opus-5", "Claude Opus 5"), ("opus-5", "Claude Opus 5"),
    ("opus 5", "Claude Opus 5"), ("claude opus 5", "Claude Opus 5"),
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-opus-4-7", "Claude Opus 4.7"), ("claude-opus-4-6", "Claude Opus 4.6"),
    ("claude-opus-4-5", "Claude Opus 4.5"), ("claude-opus-4-1", "Claude Opus 4.1"),
    ("claude-opus-4", "Claude Opus 4"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"), ("claude-sonnet-5.5", "Claude Sonnet 5.5"),
    ("claude-sonnet-5-5-latest", "Claude Sonnet 5.5"), ("claude-sonnet-5.5-latest", "Claude Sonnet 5.5"),
    ("sonnet-5-5", "Claude Sonnet 5.5"), ("sonnet-5.5", "Claude Sonnet 5.5"),
    ("sonnet 5.5", "Claude Sonnet 5.5"), ("claude sonnet 5.5", "Claude Sonnet 5.5"),
    ("claude-5-5-sonnet", "Claude Sonnet 5.5"), ("claude-5.5-sonnet", "Claude Sonnet 5.5"),
    ("claude-sonnet-5", "Claude Sonnet 5"), ("sonnet-5", "Claude Sonnet 5"),
    ("sonnet 5", "Claude Sonnet 5"), ("claude sonnet 5", "Claude Sonnet 5"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"), ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
    ("claude-sonnet-4", "Claude Sonnet 4"), ("claude-haiku-4-5", "Claude Haiku 4.5"),
    ("claude-haiku-3-5", "Claude Haiku 3.5"),
    ("deepseek-chat", "deepseek-v4-flash"), ("deepseek-reasoner", "deepseek-v4-pro"),
    ("deepseek-flash", "deepseek-v4-flash"),
]

# DeepSeek off-peak rates (50% discount on peak rates):
# Off-peak: uncached_input: $0.15, cached_input: $0.003, output: $0.60 per 1M tokens
DEEPSEEK_OFF_PEAK_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"uncached_input": 0.15, "cached_input": 0.003, "output": 0.60},
    "deepseek-flash": {"uncached_input": 0.15, "cached_input": 0.003, "output": 0.60},
    "deepseek-v4-pro": {"uncached_input": 0.66, "cached_input": 0.022, "output": 1.98},
}

Provider = str
ResolutionStatus = Literal["known", "unpriced", "unknown", "ambiguous"]


@dataclass(frozen=True)
class PricingRates:
    """Rates in USD per million tokens for one model."""

    uncached_input: float
    cached_input: float
    output: float
    cache_write: float | None = None
    cache_creation: float | None = None
    cache_write_5m: float | None = None
    cache_write_1h: float | None = None

    def as_dict(self, *, include_optional: bool = True) -> dict[str, float]:
        """Return serializable rates, omitting unset optional fields."""
        result = {"uncached_input": self.uncached_input, "cached_input": self.cached_input, "output": self.output}
        if include_optional:
            if self.cache_write is not None:
                result["cache_write"] = self.cache_write
            if self.cache_creation is not None:
                result["cache_creation"] = self.cache_creation
            if self.cache_write_5m is not None:
                result["cache_write_5m"] = self.cache_write_5m
            if self.cache_write_1h is not None:
                result["cache_write_1h"] = self.cache_write_1h
        return result

    @classmethod
    def from_mapping(cls, rates: Mapping[str, Any]) -> "PricingRates":
        return cls(
            uncached_input=float(rates["uncached_input"]), cached_input=float(rates["cached_input"]),
            output=float(rates["output"]),
            cache_write=_optional_float(rates.get("cache_write", rates.get("cache_write_input"))),
            cache_creation=_optional_float(
                rates.get("cache_creation", rates.get("cache_creation_input"))
            ),
            cache_write_5m=_optional_float(
                rates.get("cache_write_5m", rates.get("cache_creation_5m"))
            ),
            cache_write_1h=_optional_float(
                rates.get("cache_write_1h", rates.get("cache_creation_1h"))
            ),
        )


@dataclass(frozen=True)
class PricingEntry:
    """A provider-scoped canonical model and its optional rates."""

    provider: Provider
    model: str
    rates: PricingRates | None
    aliases: tuple[str, ...] = ()
    off_peak_rates: PricingRates | None = None
    exact_only: bool = False


@dataclass(frozen=True)
class PricingResolution:
    """Result of strict model resolution.

    ``unknown`` means no catalog entry matched. ``unpriced`` means the model
    is known to the provider but has no rates yet. Both expose ``rates=None``.
    """

    input_model: str | None
    provider: Provider | None
    canonical_model: str | None
    rates: PricingRates | None
    status: ResolutionStatus
    matched_by: str | None = None
    candidates: tuple[tuple[Provider, str], ...] = ()
    pricing_tier: str | None = None

    @property
    def priced(self) -> bool:
        return self.status == "known" and self.rates is not None

    @property
    def is_known(self) -> bool:
        return self.status in ("known", "unpriced")

    def as_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status, "priced": self.priced, "provider": self.provider,
            "canonical_model": self.canonical_model,
            "rates": self.rates.as_dict() if self.rates else None,
            "matched_by": self.matched_by, "candidates": list(self.candidates),
        }
        if self.pricing_tier is not None:
            data["pricing_tier"] = self.pricing_tier
        return data


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _normalize(value: str | None) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


_PROVIDER_ALIASES: dict[str, str] = {
    "agy": "antigravity", "antigravity": "antigravity", "gemini": "antigravity", "google": "antigravity",
    "claude": "claude", "claude-code": "claude", "anthropic": "claude",
    "deepseek": "deepseek", "deep-seek": "deepseek",
    "codex": "codex", "openai": "codex", "chatgpt": "codex",
}


def normalize_provider(provider: str | None) -> str | None:
    """Normalize common tool/provider names to catalog provider keys."""
    normalized = _normalize(provider)
    return _PROVIDER_ALIASES.get(normalized, normalized or None)


def to_utc_datetime(dt: Any) -> datetime | None:
    """Resolve any timestamp, datetime, or date string to an aware UTC datetime.

    Naive datetimes and strings without timezone information are interpreted as
    the user's local system time and converted to UTC via ``astimezone(timezone.utc)``.
    Aware datetimes are converted directly to UTC.
    """
    if dt is None:
        return None
    parsed_dt: datetime | None = None
    if isinstance(dt, datetime):
        parsed_dt = dt
    elif isinstance(dt, (int, float)):
        try:
            val = float(dt)
            if val > 1e14:
                secs = val / 1_000_000.0
            elif val > 1e11:
                secs = val / 1_000.0
            else:
                secs = val
            parsed_dt = datetime.fromtimestamp(secs, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    elif isinstance(dt, str):
        raw = dt.strip()
        try:
            val = float(raw)
            if val > 1e14:
                secs = val / 1_000_000.0
            elif val > 1e11:
                secs = val / 1_000.0
            else:
                secs = val
            parsed_dt = datetime.fromtimestamp(secs, timezone.utc)
        except (ValueError, OverflowError, OSError):
            if raw.endswith(("Z", "z")):
                raw = raw[:-1] + "+00:00"
            try:
                parsed_dt = datetime.fromisoformat(raw)
            except (TypeError, ValueError, OverflowError):
                return None
    else:
        return None

    if not isinstance(parsed_dt, datetime):
        return None

    try:
        return as_utc(parsed_dt)
    except (TypeError, ValueError, OverflowError):
        return None


def is_deepseek_peak_utc(dt: Any) -> bool:
    """Determine if a datetime or timestamp falls within DeepSeek peak hours.

    Resolves user local time -> UTC before evaluating against peak windows.
    Peak hours:
        01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday.
        (All other hours and weekends are off-peak).
    If dt is None or cannot be parsed, default to True (peak) to ensure
    usage and costs are never understated.
    """
    utc_dt = to_utc_datetime(dt)
    if utc_dt is None:
        return True

    # Monday is 0, Friday is 4, Saturday is 5, Sunday is 6
    if utc_dt.weekday() >= 5:
        return False

    t = utc_dt.time()
    # 01:00 - 04:00 and 06:00 - 10:00 UTC
    if time(1, 0) <= t < time(4, 0):
        return True
    if time(6, 0) <= t < time(10, 0):
        return True
    return False


class PricingCatalog:
    """Registry for provider-scoped model rates and aliases."""

    def __init__(self, entries: Iterable[PricingEntry] | None = None) -> None:
        self._lock = RLock()
        self._version = 0
        self._entries = {}
        if entries:
            for entry in entries:
                self.add_entry(entry)

    @property
    def version(self) -> int:
        """Monotonic revision, including atomic catalog replacements on refresh."""
        return self._version

    @property
    def _entries(self) -> dict[tuple[str, str], PricingEntry]:
        return self._current_entries

    @_entries.setter
    def _entries(self, entries: dict[tuple[str, str], PricingEntry]) -> None:
        # Refresh and test restoration also replace _entries directly. Rebuild
        # every derived index together so no memo can outlive its catalog.
        with self._lock:
            self._current_entries = dict(entries)
            self._normalized_entries = tuple(
                (entry, _normalize(entry.model), tuple(_normalize(a) for a in entry.aliases))
                for entry in entries.values()
            )
            self._known_providers = {entry.provider for entry in entries.values()}
            self._entries_by_name = {(e.provider, e.model): e for e in entries.values()}
            self._matches: OrderedDict[tuple[str, str | None], PricingResolution] = OrderedDict()
            self._resolutions: OrderedDict[tuple[str, str | None, bool | None], PricingResolution] = OrderedDict()
            self._version += 1

    def add_entry(self, entry: PricingEntry) -> PricingEntry:
        provider = normalize_provider(entry.provider) or entry.provider
        normalized = PricingEntry(
            provider,
            entry.model,
            entry.rates,
            entry.aliases,
            entry.off_peak_rates,
            exact_only=entry.exact_only,
        )
        with self._lock:
            new_entries = dict(self._entries)
            key = (provider, _normalize(entry.model))
            if new_entries.get(key) != normalized:
                new_entries[key] = normalized
                self._entries = new_entries
        return normalized

    def register(
        self,
        provider: str,
        model: str,
        rates: PricingRates | Mapping[str, Any] | None = None,
        *,
        aliases: Iterable[str] = (),
        uncached_input: float | None = None,
        cached_input: float | None = None,
        output: float | None = None,
        cache_write: float | None = None,
        cache_creation: float | None = None,
        cache_write_5m: float | None = None,
        cache_write_1h: float | None = None,
        off_peak_rates: PricingRates | Mapping[str, Any] | None = None,
        exact_only: bool = False,
    ) -> PricingEntry:
        """Register a model, including a known model with ``rates=None``.

        Rates may be supplied as a ``PricingRates``/mapping, or as keyword
        fields. The latter is convenient when adding a provider at runtime.
        """
        fields = (uncached_input, cached_input, output)
        if rates is None and any(value is not None for value in fields):
            if any(value is None for value in fields):
                raise ValueError("uncached_input, cached_input, and output must be supplied together")
            rates = PricingRates(
                float(uncached_input),
                float(cached_input),
                float(output),
                cache_write,
                cache_creation,
                cache_write_5m,
                cache_write_1h,
            )
        parsed = rates if isinstance(rates, PricingRates) or rates is None else PricingRates.from_mapping(rates)
        parsed_off_peak = (
            off_peak_rates
            if isinstance(off_peak_rates, PricingRates) or off_peak_rates is None
            else PricingRates.from_mapping(off_peak_rates)
        )
        return self.add_entry(
            PricingEntry(
                provider,
                model,
                parsed,
                tuple(aliases),
                off_peak_rates=parsed_off_peak,
                exact_only=exact_only,
            )
        )

    def entries(self) -> tuple[PricingEntry, ...]:
        current = self._entries
        return tuple(current.values())

    @property
    def models(self) -> dict[tuple[Provider, str], PricingEntry]:
        """Return a snapshot keyed by ``(provider, canonical_model)``."""
        current = self._entries
        return dict(current)

    def get_entry(self, provider: str, model: str) -> PricingEntry | None:
        """Return one exact provider/model entry, if registered."""
        current = self._entries
        return current.get((normalize_provider(provider) or provider, _normalize(model)))

    def remove(self, provider: str, model: str) -> None:
        """Remove one exact provider/model entry, if present."""
        key = (normalize_provider(provider) or provider, _normalize(model))
        with self._lock:
            current = self._entries
            if key in current:
                new_entries = dict(current)
                new_entries.pop(key, None)
                self._entries = new_entries

    def resolve(
        self, model_name: str | None, provider: str | None = None, *, timestamp: Any = None
    ) -> PricingResolution:
        """Memoize model matching; only time-dependent rates consult timestamps."""
        if not isinstance(model_name, str) or not model_name.strip():
            return PricingResolution(model_name, normalize_provider(provider), None, None, "unknown")
        with self._lock:
            key = (model_name, provider)
            base = self._matches.get(key)
            if base is None:
                base = self._resolve_uncached(model_name, provider)
                self._matches[key] = base
                if len(self._matches) > 1024:
                    self._matches.popitem(last=False)
            self._matches.move_to_end(key)
            entry = self._entries_by_name.get((base.provider, base.canonical_model))
            peak = is_deepseek_peak_utc(timestamp) if entry and entry.off_peak_rates is not None else None
            resolution_key = (*key, peak)
            result = self._resolutions.get(resolution_key)
            if result is None:
                result = base
                if peak is False and entry is not None:
                    result = replace(base, rates=entry.off_peak_rates, status="known", pricing_tier="off-peak")
                self._resolutions[resolution_key] = result
                if len(self._resolutions) > 2048:
                    self._resolutions.popitem(last=False)
            self._resolutions.move_to_end(resolution_key)
            return result

    def _resolve_uncached(
        self, model_name: str | None, provider: str | None = None, *, timestamp: Any = None
    ) -> PricingResolution:
        """Resolve without fallback, returning a status for every outcome."""
        if not isinstance(model_name, str) or not model_name.strip():
            return PricingResolution(model_name, normalize_provider(provider), None, None, "unknown")

        raw, scoped_provider, model = model_name.strip(), normalize_provider(provider), model_name.strip()
        known_providers = self._known_providers
        for separator in ("/", ":"):
            if separator in raw:
                prefix, candidate = raw.split(separator, 1)
                normalized_prefix = normalize_provider(prefix)
                if normalized_prefix in known_providers:
                    scoped_provider = scoped_provider or normalized_prefix
                    model = candidate.strip()
                    break

        entries = [row for row in self._normalized_entries if not scoped_provider or row[0].provider == scoped_provider]
        normalized_model = _normalize(model)
        matches: list[tuple[PricingEntry, str]] = [
            (entry, "canonical") for entry, canonical, _aliases in entries if canonical == normalized_model
        ]
        if not matches:
            matches = [
                (entry, "alias") for entry, _canonical, aliases in entries
                if normalized_model in aliases
            ]
        if not matches:
            fuzzy: list[tuple[PricingEntry, str, int]] = []
            for entry, canonical, aliases in entries:
                if entry.exact_only:
                    continue
                for key_normalized in (canonical, *aliases):
                    if key_normalized and key_normalized in normalized_model:
                        fuzzy.append((entry, "fuzzy", len(key_normalized)))
            if fuzzy:
                longest = max(item[2] for item in fuzzy)
                matches = [(entry, "fuzzy") for entry, _, size in fuzzy if size == longest]

        unique: dict[tuple[str, str], tuple[PricingEntry, str]] = {
            (entry.provider, entry.model): (entry, matched_by) for entry, matched_by in matches
        }
        matches = list(unique.values())
        if len(matches) != 1:
            status: ResolutionStatus = "unknown" if not matches else "ambiguous"
            candidates = tuple((entry.provider, entry.model) for entry, _ in matches)
            return PricingResolution(raw, scoped_provider, None, None, status, candidates=candidates)

        entry, matched_by = matches[0]
        rates = entry.rates
        pricing_tier: str | None = None
        if entry.off_peak_rates is not None:
            if is_deepseek_peak_utc(timestamp):
                rates = entry.rates
                pricing_tier = "peak"
            else:
                rates = entry.off_peak_rates
                pricing_tier = "off-peak"
        status: ResolutionStatus = "known" if rates is not None else "unpriced"
        return PricingResolution(
            raw, entry.provider, entry.model, rates, status, matched_by, pricing_tier=pricing_tier
        )

    def get_pricing(
        self, model_name: str | None, provider: str | None = None, *, timestamp: Any = None
    ) -> PricingRates | None:
        """Return rates strictly, or ``None`` for unknown/unpriced models."""
        return self.resolve(model_name, provider, timestamp=timestamp).rates


def _build_catalog() -> PricingCatalog:
    catalog = PricingCatalog()
    for model, rates in dict(MODEL_PRICING).items():
        folded_model = model.casefold()
        if folded_model.startswith("gemini"):
            provider = "antigravity"
        elif folded_model.startswith("claude"):
            provider = "claude"
        elif folded_model.startswith("deepseek"):
            provider = "deepseek"
        else:
            provider = "codex"
        aliases = tuple(alias for alias, target in _ALIASES if target == model)
        if provider == "claude":
            rates = {
                **rates,
                "cache_creation": rates["uncached_input"] * 1.25,
                "cache_write_5m": rates["uncached_input"] * 1.25,
                "cache_write_1h": rates["uncached_input"] * 2.0,
            }
        off_peak = DEEPSEEK_OFF_PEAK_PRICING.get(model) if provider == "deepseek" else None
        catalog.register(provider, model, rates, aliases=aliases, off_peak_rates=off_peak)
    return catalog


PRICING_CATALOG = _build_catalog()
DEFAULT_PRICING_CATALOG = PRICING_CATALOG


# LiteLLM publishes a community-maintained, machine-readable JSON price list.
# Rates are refreshed with conditional GET and cached on disk.
PRICING_TTL_SECONDS = 24 * 60 * 60
PRICING_RETRY_SECONDS = 15 * 60
_PRICING_REFRESH_LOCK = RLock()
_PRICING_STATES: dict[str, dict[str, Any]] = {}
_PRICING_INDEX_BY_KEY: dict[str, dict[str, dict[str, Any]]] = {}
_ACTIVE_PRICING_CACHE_KEY: str | None = None
_USED_MODELS: set[tuple[str | None, str]] = set()
_APPLIED_PAIRS_VERSION: dict[tuple[str | None, str], int] = {}
_PRICING_INDEX_VERSION: int = 0

_INITIAL_CATALOG_ENTRIES: tuple[PricingEntry, ...] = tuple(PRICING_CATALOG.entries())
_INITIAL_MODEL_PRICING: dict[str, dict[str, float]] = {k: dict(v) for k, v in MODEL_PRICING.items()}


def _new_pricing_state() -> dict[str, Any]:
    return {
        "source": "bundled",
        "source_url": LITELLM_PRICING_URL,
        "fetched_at": None,
        "last_checked_at": None,
        "next_retry_at": None,
        "stale": True,
        "error": None,
        "persistence_warning": None,
        "etag": None,
        "last_modified": None,
        "initialized": False,
        "failure_count": 0,
        "models": {},
    }


def pricing_cache_path() -> Path:
    """Return the non-repository cache path used for last-known-good rates."""
    configured = os.environ.get("AI_USAGE_PRICING_CACHE")
    if configured:
        return Path(configured).expanduser()
    cache_root = os.environ.get("XDG_CACHE_HOME")
    if cache_root:
        return Path(cache_root).expanduser() / "ai-usage-dashboard" / "litellm-pricing.json"
    return Path.home() / ".cache" / "ai-usage-dashboard" / "litellm-pricing.json"


def _response_header(headers: Any, name: str) -> Any:
    """Read an HTTP header case-insensitively from real or mocked responses."""
    if headers is None:
        return None
    try:
        value = headers.get(name)
        if value is not None:
            return value
        for key, candidate in headers.items():
            if str(key).casefold() == name.casefold():
                return candidate
    except (AttributeError, TypeError):
        return None
    return None


def _metadata_fetched_at(metadata: Mapping[str, Any]) -> float | None:
    value = metadata.get("fetched_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _write_pricing_cache(
    path: Path,
    index: Mapping[str, dict[str, Any]],
    fetched_at: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "litellm",
        "schema_version": 1,
        "source_url": LITELLM_PRICING_URL,
        "fetched_at": fetched_at,
        "etag": etag,
        "last_modified": last_modified,
        "index": dict(index),
    }
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temp_name = handle.name
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _read_pricing_cache(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]] | None:
    try:
        def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"Duplicate pricing cache key: {key}")
                result[key] = value
            return result

        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_pairs)
        if not isinstance(payload, dict):
            return None
        if payload.get("source") != "litellm" or payload.get("schema_version") != 1:
            return None
        source_url = str(payload.get("source_url") or "")
        parsed_url = urlparse(source_url)
        if (
            source_url != LITELLM_PRICING_URL
            or parsed_url.scheme != "https"
            or parsed_url.hostname != LITELLM_ALLOWED_HOST
        ):
            return None
        fetched_at = payload.get("fetched_at")
        if _metadata_fetched_at({"fetched_at": fetched_at}) is None:
            return None
        for validator in ("etag", "last_modified"):
            if payload.get(validator) is not None and not isinstance(payload.get(validator), str):
                return None
        raw_index = payload.get("index")
        if not isinstance(raw_index, dict) or len(raw_index) < 1:
            return None

        validated_index: dict[str, dict[str, Any]] = {}
        for model_key, entry in raw_index.items():
            if not isinstance(model_key, str) or not model_key.strip() or not isinstance(entry, Mapping):
                return None
            provider = entry.get("litellm_provider")
            if not isinstance(provider, str) or not provider.strip():
                return None
            entry_copy: dict[str, Any] = {"litellm_provider": str(provider)}
            required = ("uncached_input", "cached_input", "output")
            for field in required:
                if field not in entry:
                    return None
                val = entry[field]
                if (
                    val is None
                    or isinstance(val, bool)
                    or not isinstance(val, (int, float))
                    or not math.isfinite(val)
                    or val < 0
                ):
                    return None
                entry_copy[field] = float(val)

            optional_fields = (
                "cache_write",
                "cache_creation",
                "cache_write_5m",
                "cache_write_1h",
            )
            for field in optional_fields:
                if field in entry:
                    val = entry[field]
                    if val is not None:
                        if (
                            isinstance(val, bool)
                            or not isinstance(val, (int, float))
                            or not math.isfinite(val)
                            or val < 0
                        ):
                            return None
                        entry_copy[field] = float(val)
            validated_index[model_key] = entry_copy

        validate_index(validated_index)

        metadata = {
            "source": "litellm-cache",
            "source_url": source_url,
            "fetched_at": fetched_at,
            "etag": payload.get("etag"),
            "last_modified": payload.get("last_modified"),
            "last_checked_at": None,
            "next_retry_at": None,
            "stale": True,
            "error": None,
            "persistence_warning": None,
            "failure_count": 0,
        }
        return validated_index, metadata
    except (OSError, TypeError, ValueError, KeyError, OverflowError):
        return None


def _apply_single_model(
    provider: str | None,
    raw_model: str,
    index: Mapping[str, dict[str, Any]],
    state: dict[str, Any],
    *,
    catalog: PricingCatalog | None = None,
    model_pricing: dict[str, Any] | None = None,
) -> None:
    cat = catalog if catalog is not None else PRICING_CATALOG
    mp = model_pricing if model_pricing is not None else MODEL_PRICING

    hit = lookup(index, raw_model)
    res = cat.resolve(raw_model, provider)

    matched_entry = None
    if res.status == "known" and res.canonical_model:
        entry = cat.get_entry(res.provider or provider or "", res.canonical_model)
        if entry is not None:
            norm_raw = _normalize(raw_model)
            if _normalize(entry.model) == norm_raw:
                matched_entry = entry
            else:
                # Matched by alias (or fuzzy)
                if hit is not None:
                    canonical_hit = lookup(index, entry.model)
                    if canonical_hit is not None and canonical_hit[0] == hit[0]:
                        matched_entry = entry
                    else:
                        matched_entry = None
                else:
                    if res.matched_by in ("canonical", "alias"):
                        matched_entry = entry
                    else:
                        matched_entry = None

    if hit is None:
        if matched_entry is not None:
            hit = lookup(index, matched_entry.model)

    if hit is None:
        return

    key, entry_data = hit

    prov_map = {
        "openai": "codex",
        "anthropic": "claude",
        "gemini": "antigravity",
        "vertex_ai-language-models": "antigravity",
        "deepseek": "deepseek",
    }

    if matched_entry is not None:
        canonical = matched_entry.model
        target_provider = matched_entry.provider
        aliases = matched_entry.aliases
        exact_only = matched_entry.exact_only
    else:
        raw = raw_model.strip()
        scoped_provider = normalize_provider(provider)
        model_part = raw
        current_entries = cat._entries
        known_providers = {e.provider for e in current_entries.values()}
        for separator in ("/", ":"):
            if separator in raw:
                prefix, candidate = raw.split(separator, 1)
                normalized_prefix = normalize_provider(prefix)
                if normalized_prefix in known_providers:
                    scoped_provider = scoped_provider or normalized_prefix
                    model_part = candidate.strip()
                    break

        canonical = model_part
        target_provider = scoped_provider if scoped_provider else prov_map.get(entry_data.get("litellm_provider"), "codex")
        aliases = (raw,) if raw != canonical else ()
        exact_only = True

    rate_kwargs: dict[str, Any] = {
        "uncached_input": entry_data["uncached_input"],
        "cached_input": entry_data["cached_input"],
        "output": entry_data["output"],
    }
    for field in ("cache_write", "cache_creation", "cache_write_5m", "cache_write_1h"):
        if field in entry_data:
            rate_kwargs[field] = entry_data[field]

    if target_provider == "claude":
        uncached = float(rate_kwargs["uncached_input"])
        if rate_kwargs.get("cache_write_5m") is None and rate_kwargs.get("cache_creation") is None:
            rate_kwargs["cache_write"] = round_rate(uncached * 1.25)
            rate_kwargs["cache_creation"] = round_rate(uncached * 1.25)
            rate_kwargs["cache_write_5m"] = round_rate(uncached * 1.25)
        elif rate_kwargs.get("cache_write_5m") is None:
            cc = rate_kwargs.get("cache_creation")
            rate_kwargs["cache_write_5m"] = round_rate(float(cc if cc is not None else (uncached * 1.25)))
        if rate_kwargs.get("cache_write_1h") is None:
            rate_kwargs["cache_write_1h"] = round_rate(uncached * 2.0)

    rates = PricingRates(
        uncached_input=float(rate_kwargs["uncached_input"]),
        cached_input=float(rate_kwargs["cached_input"]),
        output=float(rate_kwargs["output"]),
        cache_write=_optional_float(rate_kwargs.get("cache_write")),
        cache_creation=_optional_float(rate_kwargs.get("cache_creation")),
        cache_write_5m=_optional_float(rate_kwargs.get("cache_write_5m")),
        cache_write_1h=_optional_float(rate_kwargs.get("cache_write_1h")),
    )

    if target_provider == "deepseek":
        off_peak = PricingRates(
            uncached_input=round_rate(rates.uncached_input * 0.5),
            cached_input=round_rate(rates.cached_input * 0.5),
            output=round_rate(rates.output * 0.5),
            cache_write=round_rate(rates.cache_write * 0.5) if rates.cache_write is not None else None,
            cache_creation=round_rate(rates.cache_creation * 0.5) if rates.cache_creation is not None else None,
            cache_write_5m=round_rate(rates.cache_write_5m * 0.5) if rates.cache_write_5m is not None else None,
            cache_write_1h=round_rate(rates.cache_write_1h * 0.5) if rates.cache_write_1h is not None else None,
        )
    else:
        off_peak = None

    cat.register(
        target_provider,
        canonical,
        rates,
        aliases=aliases,
        off_peak_rates=off_peak,
        exact_only=exact_only,
    )

    mp[canonical] = rates.as_dict(include_optional=False)

    if "models" not in state or not isinstance(state["models"], dict):
        state["models"] = {}
    state["models"][raw_model] = {"litellm_key": key, "canonical_model": canonical}


def _activate_pricing(state_key: str, index: Mapping[str, dict[str, Any]]) -> None:
    global _ACTIVE_PRICING_CACHE_KEY, _PRICING_INDEX_VERSION
    _PRICING_INDEX_VERSION += 1
    _ACTIVE_PRICING_CACHE_KEY = state_key
    _PRICING_INDEX_BY_KEY[state_key] = dict(index)
    _APPLIED_PAIRS_VERSION.clear()

    staged_catalog = PricingCatalog(_INITIAL_CATALOG_ENTRIES)
    staged_model_pricing = {k: dict(v) for k, v in _INITIAL_MODEL_PRICING.items()}
    staged_models: dict[str, Any] = {}
    temp_state: dict[str, Any] = {"models": staged_models}

    for provider, raw_model in list(_USED_MODELS):
        _apply_single_model(
            provider,
            raw_model,
            index,
            temp_state,
            catalog=staged_catalog,
            model_pricing=staged_model_pricing,
        )
        _APPLIED_PAIRS_VERSION[(provider, raw_model)] = _PRICING_INDEX_VERSION

    PRICING_CATALOG._entries = staged_catalog._entries

    for k, v in staged_model_pricing.items():
        MODEL_PRICING[k] = v
    for k in list(MODEL_PRICING):
        if k not in staged_model_pricing:
            del MODEL_PRICING[k]

    state = _PRICING_STATES.setdefault(state_key, _new_pricing_state())
    state["models"] = staged_models


def _ensure_active_pricing(state_key: str) -> None:
    global _ACTIVE_PRICING_CACHE_KEY
    if _ACTIVE_PRICING_CACHE_KEY == state_key:
        return
    idx = _PRICING_INDEX_BY_KEY.get(state_key) or {}
    _activate_pricing(state_key, idx)


def apply_used_model_rates(models: Iterable[tuple[str | None, str]]) -> None:
    """Apply LiteLLM rates to models seen in local usage."""
    with _PRICING_REFRESH_LOCK:
        path = pricing_cache_path()
        state_key = _ACTIVE_PRICING_CACHE_KEY or str(path.resolve())
        state = _PRICING_STATES.setdefault(state_key, _new_pricing_state())
        index = _PRICING_INDEX_BY_KEY.get(state_key)

        for provider, raw_model in models:
            if not raw_model:
                continue
            model_str = str(raw_model).strip()
            if not model_str or model_str.lower() in ("mixed", "unknown"):
                continue
            pair = (provider, model_str)
            _USED_MODELS.add(pair)
            if index is not None:
                if _APPLIED_PAIRS_VERSION.get(pair) == _PRICING_INDEX_VERSION:
                    continue
                _apply_single_model(provider, model_str, index, state)
                _APPLIED_PAIRS_VERSION[pair] = _PRICING_INDEX_VERSION


def refresh_pricing(
    *,
    force: bool = False,
    cache_path: str | Path | None = None,
    fetcher: Any | None = None,
    ttl_seconds: int = PRICING_TTL_SECONDS,
) -> dict[str, Any]:
    """Refresh LiteLLM public pricing, coalescing concurrent requests."""
    path = Path(cache_path).expanduser() if cache_path is not None else pricing_cache_path()
    state_key = str(path.resolve())
    with _PRICING_REFRESH_LOCK:
        state = _PRICING_STATES.setdefault(state_key, _new_pricing_state())
        now = datetime.now(timezone.utc).timestamp()
        if not state["initialized"]:
            cached = _read_pricing_cache(path)
            state["initialized"] = True
            if cached is not None:
                cached_index, cached_metadata = cached
                _activate_pricing(state_key, cached_index)
                cached_metadata.pop("models", None)
                state.update(cached_metadata)
                cached_at = _metadata_fetched_at(state)
                if not force and cached_at is not None and now - cached_at < max(0, ttl_seconds):
                    state["stale"] = False
                    return dict(state)

        _ensure_active_pricing(state_key)

        fetched_at = _metadata_fetched_at(state)
        if not force and not state.get("stale") and fetched_at is not None and now - fetched_at < max(0, ttl_seconds):
            return dict(state)
        retry_at = _metadata_fetched_at({"fetched_at": state.get("next_retry_at")})
        if not force and retry_at is not None and now < retry_at:
            return dict(state)

        error: str | None = None
        state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        try:
            if fetcher is None:
                parsed_url = urlparse(LITELLM_PRICING_URL)
                if parsed_url.scheme != "https" or parsed_url.hostname != LITELLM_ALLOWED_HOST:
                    raise ValueError(f"LiteLLM pricing URL must use {LITELLM_ALLOWED_HOST} over HTTPS")
                headers = {"User-Agent": "ai-usage-dashboard/1.0", "Accept": "application/json, text/plain"}
                if state.get("etag"):
                    headers["If-None-Match"] = str(state["etag"])
                if state.get("last_modified"):
                    headers["If-Modified-Since"] = str(state["last_modified"])
                request = Request(LITELLM_PRICING_URL, headers=headers)
                try:
                    response = urlopen(request, timeout=15)  # noqa: S310 - fixed official HTTPS URL
                except HTTPError as exc:
                    if exc.code != 304:
                        raise
                    refreshed = datetime.now(timezone.utc).isoformat()
                    state.update({
                        "source": "litellm",
                        "source_url": LITELLM_PRICING_URL,
                        "fetched_at": refreshed,
                        "stale": False,
                        "error": None,
                        "next_retry_at": None,
                        "failure_count": 0,
                    })
                    current_index = _PRICING_INDEX_BY_KEY.get(state_key) or {}
                    _activate_pricing(state_key, current_index)
                    state["persistence_warning"] = None
                    try:
                        _write_pricing_cache(path, current_index, refreshed, etag=state.get("etag"), last_modified=state.get("last_modified"))
                    except Exception as persist_exc:
                        state["persistence_warning"] = str(persist_exc)
                    return dict(state)

                response_url = response.geturl() if callable(getattr(response, "geturl", None)) else LITELLM_PRICING_URL
                final_url = urlparse(str(response_url))
                if final_url.scheme != "https" or final_url.hostname != LITELLM_ALLOWED_HOST:
                    raise ValueError(f"LiteLLM pricing response redirected away from {LITELLM_ALLOWED_HOST}")
                response_code = getattr(response, "status", None)
                if response_code is None:
                    try:
                        response_code = response.getcode()
                    except (AttributeError, OSError):
                        response_code = None
                if response_code == 304:
                    try:
                        response.close()
                    except (AttributeError, OSError):
                        pass
                    refreshed = datetime.now(timezone.utc).isoformat()
                    state.update({
                        "source": "litellm",
                        "source_url": LITELLM_PRICING_URL,
                        "fetched_at": refreshed,
                        "stale": False,
                        "error": None,
                        "next_retry_at": None,
                        "failure_count": 0,
                    })
                    current_index = _PRICING_INDEX_BY_KEY.get(state_key) or {}
                    _activate_pricing(state_key, current_index)
                    state["persistence_warning"] = None
                    try:
                        _write_pricing_cache(path, current_index, refreshed, etag=state.get("etag"), last_modified=state.get("last_modified"))
                    except Exception as persist_exc:
                        state["persistence_warning"] = str(persist_exc)
                    return dict(state)

                with response:
                    content_type = str(_response_header(getattr(response, "headers", None), "Content-Type") or "").lower()
                    if "application/json" not in content_type and "text/plain" not in content_type:
                        raise ValueError("LiteLLM pricing response was not JSON or text")
                    body = response.read(LITELLM_MAX_RESPONSE_BYTES + 1)
                    if len(body) > LITELLM_MAX_RESPONSE_BYTES:
                        raise ValueError("LiteLLM pricing response exceeded size limit")
                    response_etag = _response_header(getattr(response, "headers", None), "ETag")
                    response_last_modified = _response_header(getattr(response, "headers", None), "Last-Modified")
                index = parse_feed(body)
            else:
                data = fetcher()
                if isinstance(data, Mapping):
                    index = build_index(data)
                    validate_index(index)
                else:
                    index = parse_feed(data)
                response_etag = None
                response_last_modified = None

            fetched = datetime.now(timezone.utc).isoformat()
            _activate_pricing(state_key, index)
            persistence_warning = None
            try:
                _write_pricing_cache(path, index, fetched, etag=response_etag, last_modified=response_last_modified)
            except Exception as persist_exc:
                persistence_warning = str(persist_exc)
            state.update({
                "source": "litellm",
                "source_url": LITELLM_PRICING_URL,
                "fetched_at": fetched,
                "etag": response_etag,
                "last_modified": response_last_modified,
                "stale": False,
                "error": None,
                "persistence_warning": persistence_warning,
                "next_retry_at": None,
                "failure_count": 0,
            })
            return dict(state)
        except Exception as exc:
            error = str(exc)

        state["failure_count"] = int(state.get("failure_count") or 0) + 1
        backoff = min(60 * 60, PRICING_RETRY_SECONDS * (2 ** min(state["failure_count"] - 1, 2)))
        state["next_retry_at"] = datetime.fromtimestamp(now + backoff, timezone.utc).isoformat()
        if state.get("source") in ("litellm-cache", "bundled") or state.get("fetched_at") is None:
            cached = _read_pricing_cache(path)
        else:
            cached = None
        if cached is not None:
            cached_index, cached_metadata = cached
            _activate_pricing(state_key, cached_index)
            cached_metadata.pop("models", None)
            cached_metadata["error"] = error
            cached_metadata["last_checked_at"] = state.get("last_checked_at")
            cached_metadata["next_retry_at"] = state.get("next_retry_at")
            cached_metadata["failure_count"] = state.get("failure_count")
            state.update(cached_metadata)
        else:
            _ensure_active_pricing(state_key)
            state.update({"stale": True, "error": error})
        return dict(state)


def pricing_metadata(cache_path: str | Path | None = None) -> dict[str, Any]:
    """Return a copy of the active pricing provenance metadata."""
    path = Path(cache_path).expanduser() if cache_path is not None else pricing_cache_path()
    with _PRICING_REFRESH_LOCK:
        meta = dict(_PRICING_STATES.get(str(path.resolve()), _new_pricing_state()))
        if "models" in meta:
            meta["models"] = dict(meta["models"])
        return meta


def active_pricing_payload(*, refresh: bool = True, cache_path: str | Path | None = None) -> dict[str, Any]:
    """Return legacy model keys plus reserved ``__meta__`` provenance data."""
    path = Path(cache_path).expanduser() if cache_path is not None else pricing_cache_path()
    state_key = str(path.resolve())
    if refresh:
        refresh_pricing(cache_path=cache_path)
    with _PRICING_REFRESH_LOCK:
        _ensure_active_pricing(state_key)
        payload: dict[str, Any] = {}
        for model, rates in dict(MODEL_PRICING).items():
            resolved = PRICING_CATALOG.resolve(model)
            payload[model] = resolved.rates.as_dict() if resolved.rates is not None else dict(rates)
        luna_rates = payload.get("gpt-5.6-luna")
        if isinstance(luna_rates, dict):
            payload.setdefault("codex-auto-review", dict(luna_rates))
            payload.setdefault("gpt-reserve", dict(luna_rates))
        meta = dict(_PRICING_STATES.get(state_key, _new_pricing_state()))
        if "models" in meta:
            meta["models"] = dict(meta["models"])
        payload["__meta__"] = meta
        return payload


def resolve_pricing_strict(
    model_name: str | None, provider: str | None = None, *, timestamp: Any = None, catalog: PricingCatalog | None = None
) -> PricingResolution:
    """Resolve a model without a default-model fallback."""
    return (catalog or PRICING_CATALOG).resolve(model_name, provider, timestamp=timestamp)


def get_pricing_strict(
    model_name: str | None, provider: str | None = None, *, timestamp: Any = None, catalog: PricingCatalog | None = None
) -> PricingResolution:
    """Strict counterpart to :func:`get_pricing`; inspect ``status`` first."""
    return resolve_pricing_strict(model_name, provider, timestamp=timestamp, catalog=catalog)


def get_pricing(
    model_name: str | None, provider: str | None = None, *, timestamp: Any = None
) -> dict[str, float]:
    """Backward-compatible pricing lookup with the historical fallbacks."""
    resolution = PRICING_CATALOG.resolve(model_name, provider, timestamp=timestamp)
    if resolution.rates is not None:
        return resolution.rates.as_dict(include_optional=False)
    if isinstance(model_name, str) and "gemini" in model_name.casefold() and not provider:
        return dict(MODEL_PRICING["Gemini 3.8 Flash (High)"])
    return dict(MODEL_PRICING[DEFAULT_MODEL])


def _token_count(value: int | None) -> int:
    return max(0, value or 0)


def _calculate_with_rates(
    rates: PricingRates,
    uncached_input: int | None,
    cached_input: int | None,
    output: int | None,
    cache_write: int | None = None,
    cache_creation: int | None = None,
    cache_write_5m: int | None = None,
    cache_write_1h: int | None = None,
) -> dict[str, float]:
    u_in, c_in, out = _token_count(uncached_input), _token_count(cached_input), _token_count(output)
    raw_writes = _token_count(cache_write)
    raw_creations = _token_count(cache_creation)
    writes_5m = _token_count(cache_write_5m)
    writes_1h = _token_count(cache_write_1h)
    ttl_writes = writes_5m + writes_1h

    # Public callers may pass an aggregate write count together with its TTL
    # components. Subtract those components before applying generic rates so the
    # aggregate is not charged twice.
    generic_writes = max(0, raw_writes - ttl_writes)
    generic_creations = max(0, raw_creations - ttl_writes)
    generic_write_rate = rates.cache_write if rates.cache_write is not None else rates.cache_creation
    generic_creation_rate = rates.cache_creation if rates.cache_creation is not None else rates.cache_write
    write_5m_rate = rates.cache_write_5m if rates.cache_write_5m is not None else generic_write_rate
    write_1h_rate = rates.cache_write_1h if rates.cache_write_1h is not None else generic_creation_rate
    write_cost = generic_writes * (generic_write_rate or 0.0) / 1_000_000.0
    creation_cost = generic_creations * (generic_creation_rate or 0.0) / 1_000_000.0
    creation_cost += writes_5m * (write_5m_rate or 0.0) / 1_000_000.0
    creation_cost += writes_1h * (write_1h_rate or 0.0) / 1_000_000.0

    cost_cached = (u_in * rates.uncached_input + c_in * rates.cached_input + out * rates.output) / 1_000_000.0
    cost_cached += write_cost + creation_cost

    # The no-cache counterfactual prices every input token at the regular input
    # rate. Cache-write premiums therefore disappear, and expensive writes can
    # correctly produce negative net savings.
    total_input = u_in + c_in + generic_writes + generic_creations + writes_5m + writes_1h
    cost_uncached = (total_input * rates.uncached_input + out * rates.output) / 1_000_000.0
    return {
        "cost_cached_usd": round(cost_cached, 6),
        "cost_uncached_usd": round(cost_uncached, 6),
        "savings_usd": round(cost_uncached - cost_cached, 6),
    }


def calculate_cost(
    model_name: str | None, uncached_input: int | None, cached_input: int | None, output: int | None,
    cache_write: int | None = None, cache_creation: int | None = None, *,
    cache_write_5m: int | None = None, cache_write_1h: int | None = None,
    provider: str | None = None, timestamp: Any = None, catalog: PricingCatalog | None = None,
) -> dict[str, float]:
    """Calculate cost, preserving the original fallback behavior."""
    active_catalog = catalog or PRICING_CATALOG
    rates = active_catalog.resolve(model_name, provider, timestamp=timestamp).rates
    if rates is None:
        rates = PricingRates.from_mapping(get_pricing(model_name, provider, timestamp=timestamp))
    return _calculate_with_rates(
        rates,
        uncached_input,
        cached_input,
        output,
        cache_write,
        cache_creation,
        cache_write_5m,
        cache_write_1h,
    )


def calculate_cost_strict(
    model_name: str | None, uncached_input: int | None, cached_input: int | None, output: int | None, *,
    provider: str | None = None, cache_write: int | None = None, cache_creation: int | None = None,
    cache_write_5m: int | None = None, cache_write_1h: int | None = None,
    timestamp: Any = None, catalog: PricingCatalog | None = None,
) -> dict[str, Any]:
    """Calculate cost without fallback and include model-resolution status."""
    resolution = resolve_pricing_strict(model_name, provider, timestamp=timestamp, catalog=catalog)
    result: dict[str, Any] = resolution.as_dict()
    if resolution.rates is None:
        result.update({"cost_cached_usd": None, "cost_uncached_usd": None, "savings_usd": None})
    else:
        result.update(_calculate_with_rates(
            resolution.rates,
            uncached_input,
            cached_input,
            output,
            cache_write,
            cache_creation,
            cache_write_5m,
            cache_write_1h,
        ))
    return result


resolve_cost_strict = calculate_cost_strict


__all__ = [
    "DEFAULT_MODEL", "DEFAULT_PRICING_CATALOG", "DEEPSEEK_OFF_PEAK_PRICING", "MODEL_PRICING", "PRICING_CATALOG",
    "PricingCatalog", "PricingEntry", "PricingRates", "PricingResolution",
    "calculate_cost", "calculate_cost_strict", "get_pricing", "get_pricing_strict",
    "is_deepseek_peak_utc", "to_utc_datetime",
    "normalize_provider", "resolve_cost_strict", "resolve_pricing_strict",
    "LITELLM_PRICING_URL", "PRICING_TTL_SECONDS", "pricing_cache_path",
    "refresh_pricing", "apply_used_model_rates", "pricing_metadata",
    "active_pricing_payload",
]
