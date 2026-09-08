"""Model pricing definitions and cost calculation helpers."""

from __future__ import annotations

from typing import Any

# Pricing in USD per 1,000,000 tokens ($/1M tokens)
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Codex / OpenAI
    "gpt-6-astra": {
        "uncached_input": 10.0,
        "cached_input": 1.0,
        "output": 50.0,
    },
    "gpt-5.6-luna": {
        "uncached_input": 0.20,
        "cached_input": 0.02,
        "output": 1.20,
    },
    "gpt-5.6-sol": {
        "uncached_input": 0.50,
        "cached_input": 0.05,
        "output": 2.50,
    },
    "gpt-5.6-terra": {
        "uncached_input": 0.80,
        "cached_input": 0.08,
        "output": 4.00,
    },
    "gpt-4o": {
        "uncached_input": 2.50,
        "cached_input": 1.25,
        "output": 10.00,
    },
    "gpt-4o-mini": {
        "uncached_input": 0.15,
        "cached_input": 0.075,
        "output": 0.60,
    },
    "o1": {
        "uncached_input": 15.00,
        "cached_input": 7.50,
        "output": 60.00,
    },
    "o3-mini": {
        "uncached_input": 1.10,
        "cached_input": 0.55,
        "output": 4.40,
    },
    # Google Gemini / Antigravity (AGY)
    "Gemini 3.8 Flash (High)": {
        "uncached_input": 0.10,
        "cached_input": 0.025,
        "output": 0.40,
    },
    "Gemini 2.5 Flash": {
        "uncached_input": 0.30,
        "cached_input": 0.075,
        "output": 2.50,
    },
    "Gemini 2.5 Pro": {
        "uncached_input": 1.25,
        "cached_input": 0.3125,
        "output": 10.00,
    },
    "Gemini 1.5 Flash": {
        "uncached_input": 0.075,
        "cached_input": 0.01875,
        "output": 0.30,
    },
    "Gemini 1.5 Pro": {
        "uncached_input": 1.25,
        "cached_input": 0.3125,
        "output": 5.00,
    },
}

# Default model used as fallback when model cannot be resolved
DEFAULT_MODEL = "gpt-5.6-luna"

# Lookup table mapping normalized lowercase keys to standard MODEL_PRICING keys
_NORMALIZED_MAP: dict[str, str] = {
    k.lower(): k for k in MODEL_PRICING
}

# Alias rules for smart matching (checked in order)
_ALIASES: list[tuple[str, str]] = [
    # Gemini variations
    ("3.8 flash", "Gemini 3.8 Flash (High)"),
    ("gemini-3.8-flash", "Gemini 3.8 Flash (High)"),
    ("gemini 3.8", "Gemini 3.8 Flash (High)"),
    ("2.5 pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("2.5 flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("1.5 pro", "Gemini 1.5 Pro"),
    ("gemini-1.5-pro", "Gemini 1.5 Pro"),
    ("1.5 flash", "Gemini 1.5 Flash"),
    ("gemini-1.5-flash", "Gemini 1.5 Flash"),
    # OpenAI / Codex variations
    ("astra", "gpt-6-astra"),
    ("luna", "gpt-5.6-luna"),
    ("sol", "gpt-5.6-sol"),
    ("terra", "gpt-5.6-terra"),
    ("gpt-5.5", "gpt-5.6-luna"),
    ("o3-mini", "o3-mini"),
    ("o3", "o3-mini"),
    ("o1-mini", "o1"),
    ("o1-preview", "o1"),
    ("o1", "o1"),
    ("gpt-4o-mini", "gpt-4o-mini"),
    ("4o-mini", "gpt-4o-mini"),
    ("gpt-4o", "gpt-4o"),
    ("4o", "gpt-4o"),
    ("codex-auto-review", "gpt-5.6-luna"),
]


def get_pricing(model_name: str | None) -> dict[str, float]:
    """Resolve pricing rates for a given model name with smart normalization.

    Args:
        model_name: The raw name of the model, or None.

    Returns:
        A dictionary with keys 'uncached_input', 'cached_input', and 'output'
        representing USD cost per 1M tokens.
    """
    if not model_name or not isinstance(model_name, str):
        return dict(MODEL_PRICING[DEFAULT_MODEL])

    raw_norm = model_name.strip().lower()

    # 1. Exact match against canonical keys (case-insensitive)
    if raw_norm in _NORMALIZED_MAP:
        return dict(MODEL_PRICING[_NORMALIZED_MAP[raw_norm]])

    # 2. Check predefined alias patterns
    for pattern, target in _ALIASES:
        if pattern in raw_norm:
            return dict(MODEL_PRICING[target])

    # 3. Partial substring matching against canonical keys
    for key_lower, canonical_key in _NORMALIZED_MAP.items():
        if key_lower in raw_norm or raw_norm in key_lower:
            return dict(MODEL_PRICING[canonical_key])

    # 4. Fallback if "gemini" is present anywhere
    if "gemini" in raw_norm:
        return dict(MODEL_PRICING["Gemini 3.8 Flash (High)"])

    # 5. Default fallback
    return dict(MODEL_PRICING[DEFAULT_MODEL])


def calculate_cost(
    model_name: str | None,
    uncached_input: int,
    cached_input: int,
    output: int,
) -> dict[str, float]:
    """Calculate the estimated USD cost of token usage with and without caching.

    Args:
        model_name: Model identifier (used to look up per-1M-token pricing).
        uncached_input: Number of uncached input tokens.
        cached_input: Number of prompt-cached input tokens.
        output: Number of generated output tokens.

    Returns:
        Dictionary with:
            - cost_cached_usd: Actual estimated cost considering cache hits.
            - cost_uncached_usd: Baseline cost if all input tokens were uncached.
            - savings_usd: Estimated dollar savings from prompt caching (>= 0).
    """
    rates = get_pricing(model_name)
    uncached_rate = rates["uncached_input"] / 1_000_000.0
    cached_rate = rates["cached_input"] / 1_000_000.0
    output_rate = rates["output"] / 1_000_000.0

    u_in = max(0, uncached_input)
    c_in = max(0, cached_input)
    out = max(0, output)

    cost_cached = (u_in * uncached_rate) + (c_in * cached_rate) + (out * output_rate)
    cost_uncached = ((u_in + c_in) * uncached_rate) + (out * output_rate)
    savings = max(0.0, cost_uncached - cost_cached)

    return {
        "cost_cached_usd": round(cost_cached, 6),
        "cost_uncached_usd": round(cost_uncached, 6),
        "savings_usd": round(savings, 6),
    }
