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

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping


# This is the legacy, provider-less representation consumed by the existing
# API and frontend. Keep its keys and the three standard rate fields stable.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Codex / OpenAI
    "gpt-6-astra": {"uncached_input": 10.0, "cached_input": 1.0, "output": 50.0},
    "gpt-5.6-luna": {"uncached_input": 0.20, "cached_input": 0.02, "output": 1.20},
    "gpt-5.6-sol": {"uncached_input": 0.50, "cached_input": 0.05, "output": 2.50},
    "gpt-5.6-terra": {"uncached_input": 0.80, "cached_input": 0.08, "output": 4.00},
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
    ("luna", "gpt-5.6-luna"), ("sol", "gpt-5.6-sol"), ("terra", "gpt-5.6-terra"),
    ("gpt-5.5", "gpt-5.6-luna"), ("o3-mini", "o3-mini"), ("o3", "o3-mini"),
    ("o1-mini", "o1-mini"), ("o1-preview", "o1"), ("o1", "o1"),
    ("gpt-4o-mini", "gpt-4o-mini"), ("4o-mini", "gpt-4o-mini"),
    ("gpt-4o", "gpt-4o"), ("4o", "gpt-4o"), ("codex-auto-review", "gpt-5.6-luna"),
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
        provider = "antigravity" if model.casefold().startswith("gemini") else "codex"
        aliases = tuple(alias for alias, target in _ALIASES if target == model)
        catalog.register(provider, model, rates, aliases=aliases)
    return catalog


PRICING_CATALOG = _build_catalog()
DEFAULT_PRICING_CATALOG = PRICING_CATALOG


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
]
