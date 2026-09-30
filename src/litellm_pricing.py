"""LiteLLM public pricing feed parser and exact model index.

This module is pure and must NOT import ``src.pricing`` to prevent circular imports.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping

LITELLM_PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
LITELLM_ALLOWED_HOST = "raw.githubusercontent.com"
LITELLM_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def round_rate(value: float, decimals: int = 10) -> float:
    """Round a rate per million tokens to eliminate floating-point arithmetic artefacts."""
    return round(float(value), decimals)


def build_index(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Filter raw LiteLLM JSON into a first-party per-million rate mapping.

    Keeps only entries where:
    - ``litellm_provider`` in {openai, anthropic, gemini, vertex_ai-language-models, deepseek}
    - ``mode`` in {chat, responses}
    - rates are numeric, finite, non-negative, and not both 0.
    """
    allowed_providers = {
        "openai",
        "anthropic",
        "gemini",
        "vertex_ai-language-models",
        "deepseek",
    }
    allowed_modes = {"chat", "responses"}
    allowed_prefixes = ("openai/", "anthropic/", "gemini/", "deepseek/")

    index: dict[str, dict[str, Any]] = {}
    is_bare_map: dict[str, bool] = {}

    for raw_k, entry in raw.items():
        if raw_k == "sample_spec" or not isinstance(entry, Mapping):
            continue

        provider = entry.get("litellm_provider")
        if provider not in allowed_providers:
            continue

        mode = entry.get("mode")
        if mode not in allowed_modes:
            continue

        raw_key_str = str(raw_k).strip()
        if not raw_key_str:
            continue

        if "/" in raw_key_str:
            matched_prefix = None
            for pfx in allowed_prefixes:
                if raw_key_str.casefold().startswith(pfx):
                    matched_prefix = pfx
                    break
            if matched_prefix is None:
                # Any other key containing "/" is skipped
                continue
            stripped_key = raw_key_str[len(matched_prefix):]
            if "/" in stripped_key:
                continue
            key = stripped_key.casefold()
            is_bare = False
        else:
            key = raw_key_str.casefold()
            is_bare = True

        if not key:
            continue

        # A bare key wins over a prefix-stripped duplicate
        if key in index and is_bare_map.get(key) and not is_bare:
            continue

        input_cost = entry.get("input_cost_per_token")
        output_cost = entry.get("output_cost_per_token")

        if input_cost is None or output_cost is None:
            continue
        if isinstance(input_cost, bool) or isinstance(output_cost, bool):
            continue
        if not isinstance(input_cost, (int, float)) or not isinstance(output_cost, (int, float)):
            continue
        if not math.isfinite(input_cost) or not math.isfinite(output_cost):
            continue
        if input_cost < 0 or output_cost < 0:
            continue
        if input_cost == 0 and output_cost == 0:
            continue

        try:
            uncached_raw = float(input_cost) * 1e6
            output_raw = float(output_cost) * 1e6
            if not math.isfinite(uncached_raw) or not math.isfinite(output_raw):
                continue
            uncached_input = round_rate(uncached_raw)
            output = round_rate(output_raw)
            if not math.isfinite(uncached_input) or not math.isfinite(output):
                continue
        except OverflowError:
            continue

        cache_read = entry.get("cache_read_input_token_cost")
        if (
            cache_read is not None
            and not isinstance(cache_read, bool)
            and isinstance(cache_read, (int, float))
            and math.isfinite(cache_read)
            and cache_read >= 0
        ):
            try:
                cached_raw = float(cache_read) * 1e6
                if not math.isfinite(cached_raw):
                    continue
                cached_input = round_rate(cached_raw)
                if not math.isfinite(cached_input):
                    continue
            except OverflowError:
                continue
        else:
            cached_input = uncached_input

        entry_dict: dict[str, Any] = {
            "uncached_input": uncached_input,
            "cached_input": cached_input,
            "output": output,
            "litellm_provider": str(provider),
        }

        cache_creation = entry.get("cache_creation_input_token_cost")
        if (
            cache_creation is not None
            and not isinstance(cache_creation, bool)
            and isinstance(cache_creation, (int, float))
            and math.isfinite(cache_creation)
            and cache_creation >= 0
        ):
            try:
                creation_raw = float(cache_creation) * 1e6
                if math.isfinite(creation_raw):
                    creation_rate = round_rate(creation_raw)
                    if math.isfinite(creation_rate):
                        entry_dict["cache_write"] = creation_rate
                        entry_dict["cache_creation"] = creation_rate
                        entry_dict["cache_write_5m"] = creation_rate
            except OverflowError:
                pass

            above_1hr = entry.get("cache_creation_input_token_cost_above_1hr")
            if (
                above_1hr is not None
                and not isinstance(above_1hr, bool)
                and isinstance(above_1hr, (int, float))
                and math.isfinite(above_1hr)
                and above_1hr >= 0
            ):
                try:
                    above_1hr_raw = float(above_1hr) * 1e6
                    if math.isfinite(above_1hr_raw):
                        above_1hr_rate = round_rate(above_1hr_raw)
                        if math.isfinite(above_1hr_rate):
                            entry_dict["cache_write_1h"] = above_1hr_rate
                except OverflowError:
                    pass

        index[key] = entry_dict
        is_bare_map[key] = is_bare

    return index


def validate_index(index: Mapping[str, Any]) -> None:
    """Validate index completeness and provider diversity."""
    if len(index) < 20:
        raise ValueError(f"LiteLLM pricing index contains too few entries: {len(index)} < 20")

    has_openai = False
    has_anthropic = False
    has_gemini = False
    has_deepseek = False

    for entry in index.values():
        if isinstance(entry, Mapping):
            p = entry.get("litellm_provider")
            if p == "openai":
                has_openai = True
            elif p == "anthropic":
                has_anthropic = True
            elif p in ("gemini", "vertex_ai-language-models"):
                has_gemini = True
            elif p == "deepseek":
                has_deepseek = True

    distinct_count = sum([has_openai, has_anthropic, has_gemini, has_deepseek])
    if distinct_count < 3:
        raise ValueError(
            f"LiteLLM pricing index contains too few distinct providers: {distinct_count} < 3 "
            "(expected at least 3 of openai, anthropic, gemini/vertex, deepseek)"
        )


def litellm_key_candidates(model: str) -> list[str]:
    """Generate ordered, de-duplicated exact-lookup candidates for a model name."""
    raw = str(model or "").strip().casefold()
    if not raw:
        return []

    s = raw
    while True:
        prev = s
        s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s).strip()
        s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
        if s == prev:
            break

    for prefix in ("openai/", "anthropic/", "google/", "gemini/", "deepseek/"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break

    base = re.sub(r"[\s_]+", "-", s).strip("-")
    if not base:
        return []

    candidates: list[str] = []

    def _add(cand: str) -> None:
        cand = cand.strip()
        if cand and cand not in candidates:
            candidates.append(cand)

    primary = base
    dot_replaced = primary.replace(".", "-")

    _add(primary)
    _add(dot_replaced)

    for item in list(candidates):
        if item.endswith("-latest"):
            without_latest = item[:-len("-latest")].rstrip("-")
            _add(without_latest)

    return candidates


def lookup(index: Mapping[str, dict[str, Any]], model: str) -> tuple[str, dict[str, Any]] | None:
    """Find the first exact-candidate match in the LiteLLM index."""
    for candidate in litellm_key_candidates(model):
        if candidate in index:
            return candidate, index[candidate]
    return None


def parse_feed(text_or_bytes: str | bytes) -> dict[str, dict[str, Any]]:
    """Parse raw LiteLLM feed, reject duplicate keys, build and validate index."""
    if isinstance(text_or_bytes, bytes):
        text = text_or_bytes.decode("utf-8")
    else:
        text = str(text_or_bytes)

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate key in LiteLLM feed: {key}")
            result[key] = value
        return result

    raw = json.loads(text, object_pairs_hook=reject_duplicate_pairs)
    if not isinstance(raw, dict):
        raise ValueError("LiteLLM pricing feed must be a JSON object")

    index = build_index(raw)
    validate_index(index)
    return index
