"""Helpers shared by tests of the legacy file parsers and aggregator."""

from src.parsers.agy import AntigravitySource
from src.parsers.claude import ClaudeCodeSource
from src.parsers.codex import CodexSource
from src.parsers.source_registry import SourceRegistry


def legacy_file_source_registry() -> SourceRegistry:
    """Build the file-backed registry used by parser-focused aggregation tests."""
    return SourceRegistry((CodexSource(), AntigravitySource(), ClaudeCodeSource()))

