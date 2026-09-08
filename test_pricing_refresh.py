"""Offline tests for the official OpenAI pricing refresh path."""

import json
from pathlib import Path
from urllib.error import HTTPError

from src.parsers.aggregator import _refresh_estimated_session_cost
from src.parsers.contracts import CostEstimate, TokenUsage, UsageSession
from src.pricing import (
    active_pricing_payload,
    get_pricing_strict,
    parse_openai_standard_pricing,
    refresh_openai_pricing,
)
import src.pricing as pricing_module


STANDARD_FIXTURE = """
# Pricing

### Standard pricing data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6-astra | $10.00 | $1.00 | $12.50 | $50.00 | $20.00 | $2.00 | $25.00 | $75.00 |
| gpt-5.6-sol | $4.00 | $0.40 | $5.00 | $20.00 | $8.00 | $0.80 | $10.00 | $30.00 |
| gpt-5.6-terra | $2.00 | $0.20 | $2.50 | $12.00 | $4.00 | $0.40 | $5.00 | $18.00 |
| gpt-5.6-luna | $0.20 | $0.02 | $0.25 | $1.20 | $0.40 | $0.04 | $0.50 | $1.80 |
| gpt-5.5 (<272K context length) | $5.00 | $0.50 | - | $30.00 | $10.00 | $1.00 | - | $45.00 |

### Batch pricing data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output |
| --- | --- | --- | --- | --- |
| gpt-5.6-sol | $2.00 | $0.20 | $2.50 | $10.00 |
"""


def test_standard_markdown_parser_selects_short_context_and_strips_annotations() -> None:
    rates = parse_openai_standard_pricing(STANDARD_FIXTURE)
    assert rates["gpt-5.6-sol"].as_dict() == {
        "uncached_input": 4.0,
        "cached_input": 0.4,
        "output": 20.0,
        "cache_write": 5.0,
    }
    assert rates["gpt-5.5"].as_dict() == {
        "uncached_input": 5.0,
        "cached_input": 0.5,
        "output": 30.0,
    }


def test_refresh_persists_last_good_snapshot_and_reports_stale_failure(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    fresh = refresh_openai_pricing(
        force=True,
        cache_path=cache_file,
        fetcher=lambda: STANDARD_FIXTURE,
    )
    assert fresh["source"] == "openai"
    assert fresh["stale"] is False
    assert cache_file.exists()
    assert get_pricing_strict("gpt-5.6-sol").rates.uncached_input == 4.0

    stale = refresh_openai_pricing(
        force=True,
        cache_path=cache_file,
        fetcher=lambda: "not a pricing document",
    )
    assert stale["source"] == "openai"
    assert stale["stale"] is True
    assert stale["error"]
    assert get_pricing_strict("gpt-5.6-sol").rates.uncached_input == 4.0


def test_active_payload_keeps_legacy_model_keys_and_adds_reserved_metadata(tmp_path: Path) -> None:
    refresh_openai_pricing(force=True, cache_path=tmp_path / "pricing.json", fetcher=lambda: STANDARD_FIXTURE)
    payload = active_pricing_payload(refresh=False, cache_path=tmp_path / "pricing.json")
    assert payload["gpt-5.6-sol"]["uncached_input"] == 4.0
    assert payload["__meta__"]["tier"] == "standard"


def test_custom_cache_paths_reactivate_matching_rates_and_metadata(tmp_path: Path) -> None:
    cache_a = tmp_path / "a.json"
    cache_b = tmp_path / "b.json"
    fixture_b = STANDARD_FIXTURE.replace("| gpt-5.6-sol | $4.00 |", "| gpt-5.6-sol | $9.00 |")

    refresh_openai_pricing(force=True, cache_path=cache_a, fetcher=lambda: STANDARD_FIXTURE)
    refresh_openai_pricing(force=True, cache_path=cache_b, fetcher=lambda: fixture_b)
    payload_a = active_pricing_payload(refresh=False, cache_path=cache_a)
    assert payload_a["gpt-5.6-sol"]["uncached_input"] == 4.0
    assert payload_a["__meta__"]["source"] == "openai"

    payload_b = active_pricing_payload(refresh=False, cache_path=cache_b)
    assert payload_b["gpt-5.6-sol"]["uncached_input"] == 9.0


def test_estimated_costs_are_repriced_but_reported_cost_is_preserved(tmp_path: Path) -> None:
    refresh_openai_pricing(force=True, cache_path=tmp_path / "pricing.json", fetcher=lambda: STANDARD_FIXTURE)
    session = UsageSession(
        id="estimated",
        tool="codex",
        provider="codex",
        model="gpt-5.6-sol",
        usage=TokenUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0),
        cost=CostEstimate(cached_usd=0.5, uncached_usd=0.5, source="estimated"),
    )
    _refresh_estimated_session_cost(session)
    assert session.cost is not None
    assert session.cost.cached_usd == 4.0

    reported = UsageSession(
        id="reported",
        tool="codex",
        provider="codex",
        model="gpt-5.6-sol",
        usage=TokenUsage(input_tokens=1_000_000),
        cost=CostEstimate(cached_usd=0.5, uncached_usd=0.5, reported_usd=0.42),
    )
    _refresh_estimated_session_cost(reported)
    assert reported.cost is not None
    assert float(reported.cost.reported_usd) == 0.42


def test_codex_auto_review_is_not_an_alias_for_luna() -> None:
    assert get_pricing_strict("codex-auto-review", provider="codex").status == "unknown"


def test_conditional_get_uses_persisted_validators_and_handles_304(tmp_path: Path, monkeypatch) -> None:
    cache_file = tmp_path / "pricing.json"

    class Response:
        headers = {"Content-Type": "text/markdown; charset=utf-8", "ETag": '"abc"', "Last-Modified": "today"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=None):
            return STANDARD_FIXTURE.encode()

    seen = []

    def urlopen(request, timeout=0):
        seen.append(dict(request.headers))
        if len(seen) == 1:
            return Response()
        raise HTTPError(request.full_url, 304, "Not Modified", {}, None)

    monkeypatch.setattr(pricing_module, "urlopen", urlopen)
    first = refresh_openai_pricing(force=True, cache_path=cache_file)
    second = refresh_openai_pricing(force=True, cache_path=cache_file)
    assert first["etag"] == '"abc"'
    assert second["source"] == "openai"
    assert second["stale"] is False
    assert seen[1]["If-none-match"] == '"abc"'
    assert seen[1]["If-modified-since"] == "today"
    assert json.loads(cache_file.read_text())["etag"] == '"abc"'


def test_failed_refresh_backoff_prevents_repeated_requests(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    calls = []

    def failing_fetch():
        calls.append(1)
        raise OSError("offline")

    first = refresh_openai_pricing(force=True, cache_path=cache_file, fetcher=failing_fetch)
    second = refresh_openai_pricing(cache_path=cache_file, fetcher=failing_fetch)
    assert len(calls) == 1
    assert first["next_retry_at"]
    assert second["next_retry_at"] == first["next_retry_at"]


def test_live_rates_remain_active_when_cache_persistence_fails(tmp_path: Path, monkeypatch) -> None:
    cache_file = tmp_path / "pricing.json"

    def fail_persist(*_args, **_kwargs):
        raise OSError("read-only cache")

    monkeypatch.setattr(pricing_module, "_write_pricing_cache", fail_persist)
    result = refresh_openai_pricing(force=True, cache_path=cache_file, fetcher=lambda: STANDARD_FIXTURE)
    assert result["source"] == "openai"
    assert result["stale"] is False
    assert "read-only cache" in result["persistence_warning"]
    assert get_pricing_strict("gpt-5.6-sol").rates.uncached_input == 4.0


def test_network_response_requires_official_host_markdown_and_size(tmp_path: Path, monkeypatch) -> None:
    class Response:
        headers = {"Content-Type": "application/json"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://developers.openai.com/api/docs/pricing.md"

        def read(self, _limit=None):
            return b"{}"

    monkeypatch.setattr(pricing_module, "urlopen", lambda *_args, **_kwargs: Response())
    result = refresh_openai_pricing(force=True, cache_path=tmp_path / "bad-content.json")
    assert result["source"] == "bundled"
    assert "Markdown/text" in result["error"]

    class Redirected(Response):
        headers = {"Content-Type": "text/markdown"}

        def geturl(self):
            return "https://evil.example/pricing.md"

    monkeypatch.setattr(pricing_module, "urlopen", lambda *_args, **_kwargs: Redirected())
    result = refresh_openai_pricing(force=True, cache_path=tmp_path / "bad-host.json")
    assert "redirected" in result["error"]

    class Oversized(Response):
        headers = {"Content-Type": "text/markdown"}

        def read(self, limit=None):
            return b"x" * (pricing_module.OPENAI_PRICING_MAX_RESPONSE_BYTES + 1)

    monkeypatch.setattr(pricing_module, "urlopen", lambda *_args, **_kwargs: Oversized())
    result = refresh_openai_pricing(force=True, cache_path=tmp_path / "too-large.json")
    assert "size limit" in result["error"]


def test_invalid_cache_schema_is_rejected(tmp_path: Path) -> None:
    cache_file = tmp_path / "pricing.json"
    cache_file.write_text(json.dumps({
        "source": "evil",
        "source_url": "https://evil.example/pricing.md",
        "tier": "standard",
        "rates": {"gpt-5.6-luna": {"uncached_input": 0.2, "cached_input": 0.02, "output": 1.2}},
    }))
    result = refresh_openai_pricing(force=True, cache_path=cache_file, fetcher=lambda: "bad")
    assert result["source"] == "bundled"
    assert result["stale"] is True


def test_parser_rejects_duplicate_rows_and_structurally_incomplete_tables() -> None:
    duplicate = STANDARD_FIXTURE.replace(
        "| gpt-5.6-sol | $4.00 | $0.40 | $5.00 | $20.00 | $8.00 | $0.80 | $10.00 | $30.00 |",
        "| gpt-5.6-sol | $4.00 | $0.40 | $5.00 | $20.00 | $8.00 | $0.80 | $10.00 | $30.00 |\n| gpt-5.6-sol | $4.00 | $0.40 | $5.00 | $20.00 | $8.00 | $0.80 | $10.00 | $30.00 |",
    )
    try:
        parse_openai_standard_pricing(duplicate)
    except ValueError as exc:
        assert "duplicate" in str(exc).lower()
    else:
        raise AssertionError("duplicate model row was accepted")

    incomplete = STANDARD_FIXTURE.replace("| gpt-5.6-terra | $2.00 | $0.20 | $2.50 | $12.00 | $4.00 | $0.40 | $5.00 | $18.00 |\n", "")
    try:
        parse_openai_standard_pricing(incomplete)
    except ValueError as exc:
        assert "too few" in str(exc).lower()
    else:
        raise AssertionError("structurally incomplete table was accepted")


def test_parser_does_not_require_specific_model_ids() -> None:
    future_models = STANDARD_FIXTURE
    for old, new in {
        "gpt-6-astra": "gpt-7-alpha",
        "gpt-5.6-sol": "gpt-7-beta",
        "gpt-5.6-terra": "o5-mini",
        "gpt-5.6-luna": "o5-pro",
        "gpt-5.5 (<272K context length)": "research-1",
    }.items():
        future_models = future_models.replace(old, new)
    rates = parse_openai_standard_pricing(future_models)
    assert set(rates) == {"gpt-7-alpha", "gpt-7-beta", "o5-mini", "o5-pro", "research-1"}
