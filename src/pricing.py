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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Literal, Mapping
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


# This is the legacy, provider-less representation consumed by the existing
# API and frontend. Keep its keys and the three standard rate fields stable.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Codex / OpenAI
    "gpt-6-astra": {"uncached_input": 10.0, "cached_input": 1.0, "output": 50.0},
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
    "Gemini 3.8 Flash (High)": {"uncached_input": 0.10, "cached_input": 0.025, "output": 0.40},
    "Gemini 2.5 Flash": {"uncached_input": 0.30, "cached_input": 0.075, "output": 2.50},
    "Gemini 2.5 Pro": {"uncached_input": 1.25, "cached_input": 0.3125, "output": 10.00},
    "Gemini 1.5 Flash": {"uncached_input": 0.075, "cached_input": 0.01875, "output": 0.30},
    "Gemini 1.5 Pro": {"uncached_input": 1.25, "cached_input": 0.3125, "output": 5.00},
    # Claude Code / Anthropic. Rates are USD per million tokens.
    "Claude Opus 5": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.8": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.7": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.6": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.5": {"uncached_input": 5.00, "cached_input": 0.50, "output": 25.00},
    "Claude Opus 4.1": {"uncached_input": 15.00, "cached_input": 1.50, "output": 75.00},
    "Claude Opus 4": {"uncached_input": 15.00, "cached_input": 1.50, "output": 75.00},
    "Claude Sonnet 5": {"uncached_input": 2.00, "cached_input": 0.20, "output": 10.00},
    "Claude Sonnet 4.6": {"uncached_input": 3.00, "cached_input": 0.30, "output": 15.00},
    "Claude Sonnet 4.5": {"uncached_input": 3.00, "cached_input": 0.30, "output": 15.00},
    "Claude Sonnet 4": {"uncached_input": 3.00, "cached_input": 0.30, "output": 15.00},
    "Claude Haiku 4.5": {"uncached_input": 1.00, "cached_input": 0.10, "output": 5.00},
    "Claude Haiku 3.5": {"uncached_input": 0.80, "cached_input": 0.08, "output": 4.00},
    # DeepSeek V4. The official API publishes peak and off-peak rates; the
    # static catalog uses the latest peak rates so usage is not understated.
    "deepseek-v4-flash": {"uncached_input": 0.44, "cached_input": 0.014, "output": 1.32},
    "deepseek-v4-pro": {"uncached_input": 1.32, "cached_input": 0.044, "output": 3.96},
}

DEFAULT_MODEL = "gpt-5.6-luna"
_BUNDLED_OPENAI_MODELS = frozenset(
    model for model in MODEL_PRICING
    if not model.casefold().startswith(("gemini", "claude", "deepseek"))
)
_BUNDLED_OPENAI_RATE_VALUES = {
    model: dict(MODEL_PRICING[model]) for model in _BUNDLED_OPENAI_MODELS
}

# The old names remain available for callers that imported these internals.
_NORMALIZED_MAP: dict[str, str] = {k.casefold(): k for k in MODEL_PRICING}
_ALIASES: list[tuple[str, str]] = [
    ("3.8 flash", "Gemini 3.8 Flash (High)"), ("gemini-3.8-flash", "Gemini 3.8 Flash (High)"),
    ("gemini 3.8", "Gemini 3.8 Flash (High)"), ("2.5 pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro"), ("2.5 flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"), ("1.5 pro", "Gemini 1.5 Pro"),
    ("gemini-1.5-pro", "Gemini 1.5 Pro"), ("1.5 flash", "Gemini 1.5 Flash"),
    ("gemini-1.5-flash", "Gemini 1.5 Flash"), ("astra", "gpt-6-astra"),
    ("luna", "gpt-5.6-luna"), ("sol", "gpt-5.6-sol"), ("terra", "gpt-5.6-terra"),
    ("o3-mini", "o3-mini"), ("o3", "o3-mini"),
    ("o1-mini", "o1-mini"), ("o1-preview", "o1"), ("o1", "o1"),
    ("gpt-4o-mini", "gpt-4o-mini"), ("4o-mini", "gpt-4o-mini"),
    ("gpt-4o", "gpt-4o"), ("4o", "gpt-4o"),
    ("claude-opus-5", "Claude Opus 5"), ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-opus-4-7", "Claude Opus 4.7"), ("claude-opus-4-6", "Claude Opus 4.6"),
    ("claude-opus-4-5", "Claude Opus 4.5"), ("claude-opus-4-1", "Claude Opus 4.1"),
    ("claude-opus-4", "Claude Opus 4"), ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"), ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
    ("claude-sonnet-4", "Claude Sonnet 4"), ("claude-haiku-4-5", "Claude Haiku 4.5"),
    ("claude-haiku-3-5", "Claude Haiku 3.5"),
    ("deepseek-chat", "deepseek-v4-flash"), ("deepseek-reasoner", "deepseek-v4-pro"),
]

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

    def as_dict(self, *, include_optional: bool = True) -> dict[str, float]:
        """Return serializable rates, omitting unset optional fields."""
        result = {"uncached_input": self.uncached_input, "cached_input": self.cached_input, "output": self.output}
        if include_optional:
            if self.cache_write is not None:
                result["cache_write"] = self.cache_write
            if self.cache_creation is not None:
                result["cache_creation"] = self.cache_creation
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
        )


@dataclass(frozen=True)
class PricingEntry:
    """A provider-scoped canonical model and its optional rates."""

    provider: Provider
    model: str
    rates: PricingRates | None
    aliases: tuple[str, ...] = ()


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

    @property
    def priced(self) -> bool:
        return self.status == "known" and self.rates is not None

    @property
    def is_known(self) -> bool:
        return self.status in ("known", "unpriced")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "priced": self.priced, "provider": self.provider,
            "canonical_model": self.canonical_model,
            "rates": self.rates.as_dict() if self.rates else None,
            "matched_by": self.matched_by, "candidates": list(self.candidates),
        }


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


class PricingCatalog:
    """Registry for provider-scoped model rates and aliases."""

    def __init__(self, entries: Iterable[PricingEntry] | None = None) -> None:
        self._entries: dict[tuple[str, str], PricingEntry] = {}
        if entries:
            for entry in entries:
                self.add_entry(entry)

    def add_entry(self, entry: PricingEntry) -> PricingEntry:
        provider = normalize_provider(entry.provider) or entry.provider
        normalized = PricingEntry(provider, entry.model, entry.rates, entry.aliases)
        self._entries[(provider, _normalize(entry.model))] = normalized
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
                float(uncached_input), float(cached_input), float(output), cache_write, cache_creation
            )
        parsed = rates if isinstance(rates, PricingRates) or rates is None else PricingRates.from_mapping(rates)
        return self.add_entry(PricingEntry(provider, model, parsed, tuple(aliases)))

    def entries(self) -> tuple[PricingEntry, ...]:
        return tuple(self._entries.values())

    @property
    def models(self) -> dict[tuple[Provider, str], PricingEntry]:
        """Return a snapshot keyed by ``(provider, canonical_model)``."""
        return dict(self._entries)

    def get_entry(self, provider: str, model: str) -> PricingEntry | None:
        """Return one exact provider/model entry, if registered."""
        return self._entries.get((normalize_provider(provider) or provider, _normalize(model)))

    def remove(self, provider: str, model: str) -> None:
        """Remove one exact provider/model entry, if present."""
        self._entries.pop((normalize_provider(provider) or provider, _normalize(model)), None)

    def resolve(self, model_name: str | None, provider: str | None = None) -> PricingResolution:
        """Resolve without fallback, returning a status for every outcome."""
        if not isinstance(model_name, str) or not model_name.strip():
            return PricingResolution(model_name, normalize_provider(provider), None, None, "unknown")

        raw, scoped_provider, model = model_name.strip(), normalize_provider(provider), model_name.strip()
        known_providers = {entry.provider for entry in self._entries.values()}
        for separator in ("/", ":"):
            if separator in raw:
                prefix, candidate = raw.split(separator, 1)
                normalized_prefix = normalize_provider(prefix)
                if normalized_prefix in known_providers:
                    scoped_provider = scoped_provider or normalized_prefix
                    model = candidate.strip()
                    break

        entries = [entry for entry in self._entries.values() if not scoped_provider or entry.provider == scoped_provider]
        normalized_model = _normalize(model)
        matches: list[tuple[PricingEntry, str]] = [
            (entry, "canonical") for entry in entries if _normalize(entry.model) == normalized_model
        ]
        if not matches:
            matches = [
                (entry, "alias") for entry in entries
                if any(_normalize(alias) == normalized_model for alias in entry.aliases)
            ]
        if not matches:
            fuzzy: list[tuple[PricingEntry, str, int]] = []
            for entry in entries:
                keys = [(entry.model, "canonical"), *[(alias, "alias") for alias in entry.aliases]]
                for key, matched_by in keys:
                    key_normalized = _normalize(key)
                    if key_normalized and key_normalized in normalized_model:
                        fuzzy.append((entry, matched_by, len(key_normalized)))
            if fuzzy:
                longest = max(item[2] for item in fuzzy)
                matches = [(entry, matched_by) for entry, matched_by, size in fuzzy if size == longest]

        unique: dict[tuple[str, str], tuple[PricingEntry, str]] = {
            (entry.provider, _normalize(entry.model)): (entry, matched_by) for entry, matched_by in matches
        }
        matches = list(unique.values())
        if len(matches) != 1:
            status: ResolutionStatus = "unknown" if not matches else "ambiguous"
            candidates = tuple((entry.provider, entry.model) for entry, _ in matches)
            return PricingResolution(raw, scoped_provider, None, None, status, candidates=candidates)

        entry, matched_by = matches[0]
        status: ResolutionStatus = "known" if entry.rates is not None else "unpriced"
        return PricingResolution(raw, entry.provider, entry.model, entry.rates, status, matched_by)

    def get_pricing(self, model_name: str | None, provider: str | None = None) -> PricingRates | None:
        """Return rates strictly, or ``None`` for unknown/unpriced models."""
        return self.resolve(model_name, provider).rates


def _build_catalog() -> PricingCatalog:
    catalog = PricingCatalog()
    for model, rates in MODEL_PRICING.items():
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
            rates = {**rates, "cache_creation": rates["uncached_input"] * 1.25}
        catalog.register(provider, model, rates, aliases=aliases)
    return catalog


PRICING_CATALOG = _build_catalog()
DEFAULT_PRICING_CATALOG = PRICING_CATALOG


# OpenAI publishes pricing as a Markdown table rather than as a public pricing
# API.  Keep the URL and cache policy in one place so the dashboard can refresh
# without making network access part of cost calculation itself.
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/pricing.md"
OPENAI_PRICING_TTL_SECONDS = 24 * 60 * 60
OPENAI_PRICING_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
OPENAI_PRICING_RETRY_SECONDS = 15 * 60
_PRICING_REFRESH_LOCK = RLock()
_PRICING_STATES: dict[str, dict[str, Any]] = {}
_PRICING_RATES_BY_KEY: dict[str, dict[str, PricingRates]] = {}
_ACTIVE_PRICING_CACHE_KEY: str | None = None


def _new_pricing_state() -> dict[str, Any]:
    return {
        "source": "bundled",
        "source_url": OPENAI_PRICING_URL,
        "fetched_at": None,
        "last_checked_at": None,
        "next_retry_at": None,
        "stale": True,
        "error": None,
        "persistence_warning": None,
        "etag": None,
        "last_modified": None,
        "tier": "standard",
        "initialized": False,
        "failure_count": 0,
    }


def _bundled_openai_rates() -> dict[str, PricingRates]:
    return {
        model: PricingRates.from_mapping(rates)
        for model, rates in _BUNDLED_OPENAI_RATE_VALUES.items()
    }


def pricing_cache_path() -> Path:
    """Return the non-repository cache path used for last-known-good rates."""
    configured = os.environ.get("AI_USAGE_PRICING_CACHE")
    if configured:
        return Path(configured).expanduser()
    cache_root = os.environ.get("XDG_CACHE_HOME")
    if cache_root:
        return Path(cache_root).expanduser() / "ai-usage-dashboard" / "openai-pricing.json"
    return Path.home() / ".cache" / "ai-usage-dashboard" / "openai-pricing.json"


def _parse_price(value: str) -> float | None:
    raw = str(value or "").strip().replace("$", "").replace(",", "")
    if not raw or raw == "-":
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


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


def _model_family(model: str) -> str:
    """Return a stable family key without depending on specific model IDs."""
    normalized = _normalize(model)
    match = re.match(r"^([a-z]+(?:[-.]\d+)?)(?:[-_.]|$)", normalized)
    return match.group(1) if match else normalized.split("-", 1)[0]


def _validate_standard_completeness(rates: Mapping[str, PricingRates]) -> None:
    """Reject responses that look like an incomplete/incorrect pricing table."""
    if len(rates) < 5:
        raise ValueError("OpenAI Standard pricing table contains too few model rows")
    families = {_model_family(model) for model in rates if _normalize(model)}
    if len(families) < 2:
        raise ValueError("OpenAI Standard pricing table contains too few model families")


def parse_openai_standard_pricing(markdown: str) -> dict[str, PricingRates]:
    """Parse the official Standard pricing table from OpenAI's Markdown page.

    The table has deliberately explicit headings.  This avoids relying on the
    generated HTML/React payload and ignores Batch, Flex, and Fast tables.
    Long-context columns are retained by the source but not selected because
    local transcripts do not identify the service tier or context band.
    """
    lines = str(markdown or "").splitlines()
    try:
        table_start = next(index for index, line in enumerate(lines) if line.strip().lower() == "### standard pricing data")
    except StopIteration as exc:
        raise ValueError("OpenAI Standard pricing table was not found") from exc

    header_index = next(
        (index for index in range(table_start + 1, len(lines)) if lines[index].lstrip().startswith("| Model |")),
        None,
    )
    if header_index is None:
        raise ValueError("OpenAI Standard pricing table header was not found")
    headers = tuple(field.strip().lower() for field in lines[header_index].strip().strip("|").split("|"))
    expected_headers = (
        "model", "short context input", "short context cached input", "short context cache writes",
        "short context output", "long context input", "long context cached input",
        "long context cache writes", "long context output",
    )
    if headers != expected_headers:
        raise ValueError("OpenAI Standard pricing table schema changed")
    separator_index = header_index + 1
    if separator_index >= len(lines):
        raise ValueError("OpenAI Standard pricing table separator was not found")
    separator_fields = [field.strip() for field in lines[separator_index].strip().strip("|").split("|")]
    if len(separator_fields) != len(expected_headers) or any(
        not field or not re.fullmatch(r":?-{3,}:?", field) for field in separator_fields
    ):
        raise ValueError("OpenAI Standard pricing table separator is malformed")

    rates: dict[str, PricingRates] = {}
    normalized_model_ids: set[str] = set()
    for line in lines[header_index + 2:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rates:
                break
            continue
        fields = [field.strip() for field in stripped.strip("|").split("|")]
        if len(fields) != len(expected_headers):
            raise ValueError("OpenAI Standard pricing table contains a malformed row")
        model = re.sub(r"\s*\(<\d+K context length\)\s*$", "", fields[0], flags=re.IGNORECASE).strip()
        if not model:
            raise ValueError("OpenAI Standard pricing table contains an empty model ID")
        normalized_model = _normalize(model)
        if model in rates or normalized_model in normalized_model_ids:
            raise ValueError(f"OpenAI Standard pricing table contains duplicate model ID: {model}")
        if any(field.strip() == "" for field in fields[1:]):
            raise ValueError(f"OpenAI Standard pricing table contains partial rates for {model}")
        uncached = _parse_price(fields[1])
        cached = _parse_price(fields[2])
        cache_write = _parse_price(fields[3])
        output = _parse_price(fields[4])
        # Some Pro models intentionally have no cached-input rate.  Treating a
        # cache read as regular input is conservative and avoids zero pricing.
        if uncached is None or output is None:
            raise ValueError(f"OpenAI Standard pricing table contains malformed rates for {model}")
        for field in fields[1:]:
            if field.strip() not in ("", "-") and _parse_price(field) is None:
                raise ValueError(f"OpenAI Standard pricing table contains malformed rates for {model}")
        rates[model] = PricingRates(
            uncached_input=uncached,
            cached_input=cached if cached is not None else uncached,
            output=output,
            cache_write=cache_write,
        )
        normalized_model_ids.add(normalized_model)
    if not rates:
        raise ValueError("OpenAI Standard pricing table contained no usable rates")
    _validate_standard_completeness(rates)
    return rates


def _metadata_fetched_at(metadata: Mapping[str, Any]) -> float | None:
    value = metadata.get("fetched_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _apply_openai_rates(rates: Mapping[str, PricingRates], *, replace: bool = False) -> None:
    """Apply validated OpenAI rates, optionally removing absent stale models."""
    if replace:
        for model in list(MODEL_PRICING):
            if model in _BUNDLED_OPENAI_MODELS or PRICING_CATALOG.get_entry("codex", model) is not None:
                if model not in rates and PRICING_CATALOG.get_entry("codex", model) is not None:
                    PRICING_CATALOG.remove("codex", model)
                    MODEL_PRICING.pop(model, None)
    for model, model_rates in rates.items():
        aliases = tuple(alias for alias, target in _ALIASES if target == model)
        PRICING_CATALOG.register("codex", model, model_rates, aliases=aliases)
        MODEL_PRICING[model] = model_rates.as_dict(include_optional=False)


def _activate_pricing(key: str, rates: Mapping[str, PricingRates]) -> None:
    """Make one cache snapshot the process-global active catalog snapshot."""
    global _ACTIVE_PRICING_CACHE_KEY
    snapshot = dict(rates)
    _PRICING_RATES_BY_KEY[key] = snapshot
    _apply_openai_rates(snapshot, replace=True)
    _ACTIVE_PRICING_CACHE_KEY = key


def _ensure_active_pricing(key: str) -> None:
    """Ensure active rates and metadata refer to the same cache key."""
    if _ACTIVE_PRICING_CACHE_KEY == key:
        return
    _activate_pricing(key, _PRICING_RATES_BY_KEY.get(key) or _bundled_openai_rates())


def _write_pricing_cache(
    path: Path,
    rates: Mapping[str, PricingRates],
    fetched_at: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "openai",
        "source_url": OPENAI_PRICING_URL,
        "fetched_at": fetched_at,
        "etag": etag,
        "last_modified": last_modified,
        "tier": "standard",
        "rates": {model: value.as_dict() for model, value in rates.items()},
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


def _read_pricing_cache(path: Path) -> tuple[dict[str, PricingRates], dict[str, Any]] | None:
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
        if payload.get("source") != "openai" or payload.get("tier") != "standard":
            return None
        source_url = str(payload.get("source_url") or "")
        parsed_url = urlparse(source_url)
        if source_url != OPENAI_PRICING_URL or parsed_url.scheme != "https" or parsed_url.hostname != "developers.openai.com":
            return None
        fetched_at = payload.get("fetched_at")
        if _metadata_fetched_at({"fetched_at": fetched_at}) is None:
            return None
        for validator in ("etag", "last_modified"):
            if payload.get(validator) is not None and not isinstance(payload.get(validator), str):
                return None
        raw_rates = payload.get("rates")
        if not isinstance(raw_rates, dict) or len(raw_rates) < 1:
            return None
        rates: dict[str, PricingRates] = {}
        normalized_model_ids: set[str] = set()
        for model, value in raw_rates.items():
            if not isinstance(model, str) or not model.strip() or _normalize(model) in normalized_model_ids or not isinstance(value, Mapping):
                return None
            required = ("uncached_input", "cached_input", "output")
            if any(key not in value for key in required):
                return None
            numeric_fields = (*required, "cache_write", "cache_write_input", "cache_creation", "cache_creation_input")
            if any(
                key in value and value[key] is not None
                and (isinstance(value[key], bool) or not isinstance(value[key], (int, float)))
                for key in numeric_fields
            ):
                return None
            parsed_rates = PricingRates.from_mapping(value)
            if any(
                not math.isfinite(rate) or rate < 0
                for rate in (parsed_rates.uncached_input, parsed_rates.cached_input, parsed_rates.output)
            ):
                return None
            if parsed_rates.cache_write is not None and (
                not math.isfinite(parsed_rates.cache_write) or parsed_rates.cache_write < 0
            ):
                return None
            if parsed_rates.cache_creation is not None and (
                not math.isfinite(parsed_rates.cache_creation) or parsed_rates.cache_creation < 0
            ):
                return None
            rates[model] = parsed_rates
            normalized_model_ids.add(_normalize(model))
        try:
            _validate_standard_completeness(rates)
        except ValueError:
            return None
        metadata = {
            "source": "openai-cache",
            "source_url": source_url,
            "fetched_at": fetched_at,
            "etag": payload.get("etag"),
            "last_modified": payload.get("last_modified"),
            "last_checked_at": None,
            "next_retry_at": None,
            "stale": True,
            "error": None,
            "persistence_warning": None,
            "tier": str(payload.get("tier") or "standard"),
        }
        return rates, metadata
    except (OSError, TypeError, ValueError, KeyError, OverflowError):
        return None


def refresh_openai_pricing(
    *,
    force: bool = False,
    cache_path: str | Path | None = None,
    fetcher: Any | None = None,
    ttl_seconds: int = OPENAI_PRICING_TTL_SECONDS,
) -> dict[str, Any]:
    """Refresh OpenAI Standard rates, coalescing concurrent requests.

    Network and parsing failures never remove working rates.  A valid disk
    snapshot is preferred over the bundled catalog when offline, and metadata
    reports whether the active rates are stale.
    """
    path = Path(cache_path).expanduser() if cache_path is not None else pricing_cache_path()
    state_key = str(path.resolve())
    with _PRICING_REFRESH_LOCK:
        state = _PRICING_STATES.setdefault(state_key, _new_pricing_state())
        now = datetime.now(timezone.utc).timestamp()
        if not state["initialized"]:
            cached = _read_pricing_cache(path)
            state["initialized"] = True
            if cached is not None:
                cached_rates, cached_metadata = cached
                _activate_pricing(state_key, cached_rates)
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
                parsed_url = urlparse(OPENAI_PRICING_URL)
                if parsed_url.scheme != "https" or parsed_url.hostname != "developers.openai.com":
                    raise ValueError("OpenAI pricing URL must use developers.openai.com over HTTPS")
                headers = {"User-Agent": "ai-usage-dashboard/1.0", "Accept": "text/markdown"}
                if state.get("etag"):
                    headers["If-None-Match"] = str(state["etag"])
                if state.get("last_modified"):
                    headers["If-Modified-Since"] = str(state["last_modified"])
                request = Request(OPENAI_PRICING_URL, headers=headers)
                try:
                    response = urlopen(request, timeout=10)  # noqa: S310 - fixed official HTTPS URL
                except HTTPError as exc:
                    if exc.code != 304:
                        raise
                    refreshed = datetime.now(timezone.utc).isoformat()
                    state.update({
                        "source": "openai", "source_url": OPENAI_PRICING_URL, "fetched_at": refreshed,
                        "stale": False, "error": None, "next_retry_at": None, "failure_count": 0,
                        "tier": "standard",
                    })
                    cached_active = {
                        model: entry.rates
                        for (provider, model), entry in PRICING_CATALOG.models.items()
                        if provider == "codex" and entry.rates is not None
                    }
                    _activate_pricing(state_key, cached_active)
                    state["persistence_warning"] = None
                    try:
                        _write_pricing_cache(path, cached_active, refreshed, etag=state.get("etag"), last_modified=state.get("last_modified"))
                    except Exception as persist_exc:
                        state["persistence_warning"] = str(persist_exc)
                    return dict(state)
                response_url = response.geturl() if callable(getattr(response, "geturl", None)) else OPENAI_PRICING_URL
                final_url = urlparse(str(response_url))
                if final_url.scheme != "https" or final_url.hostname != "developers.openai.com":
                    raise ValueError("OpenAI pricing response redirected away from developers.openai.com")
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
                        "source": "openai", "source_url": OPENAI_PRICING_URL, "fetched_at": refreshed,
                        "stale": False, "error": None, "next_retry_at": None, "failure_count": 0,
                        "tier": "standard",
                    })
                    cached_active = {
                        model: entry.rates
                        for (provider, model), entry in PRICING_CATALOG.models.items()
                        if provider == "codex" and entry.rates is not None
                    }
                    _activate_pricing(state_key, cached_active)
                    state["persistence_warning"] = None
                    try:
                        _write_pricing_cache(path, cached_active, refreshed, etag=state.get("etag"), last_modified=state.get("last_modified"))
                    except Exception as persist_exc:
                        state["persistence_warning"] = str(persist_exc)
                    return dict(state)
                with response:
                    content_type = str(_response_header(getattr(response, "headers", None), "Content-Type") or "").lower()
                    if "text/markdown" not in content_type and "text/plain" not in content_type:
                        raise ValueError("OpenAI pricing response was not Markdown/text")
                    body = response.read(OPENAI_PRICING_MAX_RESPONSE_BYTES + 1)
                    if len(body) > OPENAI_PRICING_MAX_RESPONSE_BYTES:
                        raise ValueError("OpenAI pricing response exceeded size limit")
                    markdown = body.decode("utf-8")
                    response_etag = _response_header(getattr(response, "headers", None), "ETag")
                    response_last_modified = _response_header(getattr(response, "headers", None), "Last-Modified")
            else:
                markdown = fetcher()
                response_etag = None
                response_last_modified = None
            rates = parse_openai_standard_pricing(markdown)
            fetched = datetime.now(timezone.utc).isoformat()
            _activate_pricing(state_key, rates)
            persistence_warning = None
            try:
                _write_pricing_cache(path, rates, fetched, etag=response_etag, last_modified=response_last_modified)
            except Exception as persist_exc:
                persistence_warning = str(persist_exc)
            state.update({
                "source": "openai",
                "source_url": OPENAI_PRICING_URL,
                "fetched_at": fetched,
                "etag": response_etag,
                "last_modified": response_last_modified,
                "stale": False,
                "error": None,
                "persistence_warning": persistence_warning,
                "next_retry_at": None,
                "failure_count": 0,
                "tier": "standard",
            })
            return dict(state)
        except Exception as exc:  # Network/parsing errors must not break dashboard usage.
            error = str(exc)

        state["failure_count"] = int(state.get("failure_count") or 0) + 1
        backoff = min(60 * 60, OPENAI_PRICING_RETRY_SECONDS * (2 ** min(state["failure_count"] - 1, 2)))
        state["next_retry_at"] = datetime.fromtimestamp(now + backoff, timezone.utc).isoformat()
        if state.get("source") in ("openai-cache", "bundled") or state.get("fetched_at") is None:
            cached = _read_pricing_cache(path)
        else:
            cached = None
        if cached is not None:
            cached_rates, cached_metadata = cached
            _activate_pricing(state_key, cached_rates)
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
        return dict(_PRICING_STATES.get(str(path.resolve()), _new_pricing_state()))


def active_pricing_payload(*, refresh: bool = True, cache_path: str | Path | None = None) -> dict[str, Any]:
    """Return legacy model keys plus reserved ``__meta__`` provenance data."""
    path = Path(cache_path).expanduser() if cache_path is not None else pricing_cache_path()
    state_key = str(path.resolve())
    if refresh:
        refresh_openai_pricing(cache_path=cache_path)
    with _PRICING_REFRESH_LOCK:
        _ensure_active_pricing(state_key)
        payload: dict[str, Any] = {}
        for model, rates in MODEL_PRICING.items():
            resolved = PRICING_CATALOG.resolve(model)
            payload[model] = resolved.rates.as_dict() if resolved.rates is not None else dict(rates)
        payload["__meta__"] = dict(_PRICING_STATES.get(state_key, _new_pricing_state()))
        return payload


def resolve_pricing_strict(
    model_name: str | None, provider: str | None = None, *, catalog: PricingCatalog | None = None
) -> PricingResolution:
    """Resolve a model without a default-model fallback."""
    return (catalog or PRICING_CATALOG).resolve(model_name, provider)


def get_pricing_strict(
    model_name: str | None, provider: str | None = None, *, catalog: PricingCatalog | None = None
) -> PricingResolution:
    """Strict counterpart to :func:`get_pricing`; inspect ``status`` first."""
    return resolve_pricing_strict(model_name, provider, catalog=catalog)


def get_pricing(model_name: str | None, provider: str | None = None) -> dict[str, float]:
    """Backward-compatible pricing lookup with the historical fallbacks."""
    resolution = PRICING_CATALOG.resolve(model_name, provider)
    if resolution.rates is not None:
        return resolution.rates.as_dict(include_optional=False)
    if isinstance(model_name, str) and "gemini" in model_name.casefold() and not provider:
        return dict(MODEL_PRICING["Gemini 3.8 Flash (High)"])
    return dict(MODEL_PRICING[DEFAULT_MODEL])


def _token_count(value: int | None) -> int:
    return max(0, value or 0)


def _calculate_with_rates(
    rates: PricingRates, uncached_input: int | None, cached_input: int | None, output: int | None,
    cache_write: int | None = None, cache_creation: int | None = None,
) -> dict[str, float]:
    u_in, c_in, out = _token_count(uncached_input), _token_count(cached_input), _token_count(output)
    writes, creations = _token_count(cache_write), _token_count(cache_creation)
    cost_cached = (u_in * rates.uncached_input + c_in * rates.cached_input + out * rates.output) / 1_000_000.0
    # Providers use both "cache write" and "cache creation" for the same
    # billing concept. Fall back across the aliases when only one is defined.
    write_rate = rates.cache_write if rates.cache_write is not None else rates.cache_creation
    creation_rate = rates.cache_creation if rates.cache_creation is not None else rates.cache_write
    write_cost = writes * (write_rate or 0.0) / 1_000_000.0
    creation_cost = creations * (creation_rate or 0.0) / 1_000_000.0
    cost_cached += write_cost + creation_cost
    cost_uncached = ((u_in + c_in) * rates.uncached_input + out * rates.output) / 1_000_000.0
    cost_uncached += write_cost + creation_cost
    return {
        "cost_cached_usd": round(cost_cached, 6), "cost_uncached_usd": round(cost_uncached, 6),
        "savings_usd": round(max(0.0, cost_uncached - cost_cached), 6),
    }


def calculate_cost(
    model_name: str | None, uncached_input: int | None, cached_input: int | None, output: int | None,
    cache_write: int | None = None, cache_creation: int | None = None, *,
    provider: str | None = None, catalog: PricingCatalog | None = None,
) -> dict[str, float]:
    """Calculate cost, preserving the original fallback behavior."""
    active_catalog = catalog or PRICING_CATALOG
    rates = active_catalog.resolve(model_name, provider).rates
    if rates is None:
        rates = PricingRates.from_mapping(get_pricing(model_name, provider))
    return _calculate_with_rates(rates, uncached_input, cached_input, output, cache_write, cache_creation)


def calculate_cost_strict(
    model_name: str | None, uncached_input: int | None, cached_input: int | None, output: int | None, *,
    provider: str | None = None, cache_write: int | None = None, cache_creation: int | None = None,
    catalog: PricingCatalog | None = None,
) -> dict[str, Any]:
    """Calculate cost without fallback and include model-resolution status."""
    resolution = resolve_pricing_strict(model_name, provider, catalog=catalog)
    result: dict[str, Any] = resolution.as_dict()
    if resolution.rates is None:
        result.update({"cost_cached_usd": None, "cost_uncached_usd": None, "savings_usd": None})
    else:
        result.update(_calculate_with_rates(
            resolution.rates, uncached_input, cached_input, output, cache_write, cache_creation
        ))
    return result


resolve_cost_strict = calculate_cost_strict


__all__ = [
    "DEFAULT_MODEL", "DEFAULT_PRICING_CATALOG", "MODEL_PRICING", "PRICING_CATALOG",
    "PricingCatalog", "PricingEntry", "PricingRates", "PricingResolution",
    "calculate_cost", "calculate_cost_strict", "get_pricing", "get_pricing_strict",
    "normalize_provider", "resolve_cost_strict", "resolve_pricing_strict",
    "OPENAI_PRICING_URL", "OPENAI_PRICING_TTL_SECONDS", "pricing_cache_path",
    "parse_openai_standard_pricing", "refresh_openai_pricing", "pricing_metadata",
    "active_pricing_payload",
]
