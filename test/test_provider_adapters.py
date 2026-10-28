"""Focused tests for Codex and Antigravity provider adapters."""

from src.parsers.agy import AntigravitySource
from src.parsers.codex import CodexSource


def _legacy_session(tool: str) -> dict:
    return {
        "id": "session-1",
        "tool": tool,
        "model": "test-model",
        "title": "A test session",
        "created_at": "2026-09-08T00:00:00+00:00",
        "start_time": "2026-09-08T00:00:00+00:00",
        "end_time": "2026-09-08T00:01:00+00:00",
        "call_count": 1,
        "uncached_input": 75,
        "cached_input": 25,
        "total_input": 100,
        "output": 10,
        "reasoning_output": 2,
        "total_tokens": 110,
        "cost_cached_usd": 0.01,
        "cost_uncached_usd": 0.02,
        "savings_usd": 0.005,
        "usage_events": [
            {
                "timestamp": "2026-09-08T00:01:00+00:00",
                "input_tokens": 100,
                "cached_input_tokens": 25,
                "output_tokens": 10,
                "reasoning_output_tokens": 2,
                "total_tokens": 110,
            }
        ],
    }


def test_sources_expose_stable_identity_and_default_paths() -> None:
    codex = CodexSource()
    agy = AntigravitySource()
    assert codex.key == "codex"
    assert "openai-codex" in codex.aliases
    assert str(codex.default_source_path).endswith("/.codex")
    assert agy.key == "antigravity"
    assert "agy" in agy.aliases
    assert str(agy.default_source_path).endswith("/.gemini/antigravity-cli")


def test_sources_convert_legacy_sessions_to_contracts() -> None:
    codex = CodexSource()
    agy = AntigravitySource()
    codex_sessions = codex_module_sessions(codex, _legacy_session("codex"))
    agy_sessions = agy_module_sessions(agy, _legacy_session("antigravity"))

    for session, provider in ((codex_sessions[0], "codex"), (agy_sessions[0], "antigravity")):
        assert session.provider == provider
        assert session.tool == provider
        assert session.usage.input_tokens == 100
        assert session.usage.cached_input_tokens == 25
        assert session.events[0].model == "test-model"
        assert session.cost is not None


def codex_module_sessions(source: CodexSource, raw: dict) -> list:
    # Exercise the adapter conversion boundary without reading a user's home
    # directory. This helper mirrors the source parser's internal output.
    from src.parsers.codex import _legacy_sessions_to_contract

    return _legacy_sessions_to_contract({"sessions": [raw]})


def agy_module_sessions(source: AntigravitySource, raw: dict) -> list:
    from src.parsers.agy import _legacy_sessions_to_contract

    return _legacy_sessions_to_contract({"sessions": [raw]})
