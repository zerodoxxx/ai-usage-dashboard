"""Thread-safe registry for provider usage sources."""

from __future__ import annotations

from threading import RLock
from typing import Iterable

from .contracts import UsageSource


def normalize_source_key(value: str) -> str:
    """Normalize a provider key while preserving readable separators."""
    normalized = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized


class SourceRegistry:
    """Registry with atomic registration and alias lookup.

    A source's canonical key and all aliases share one namespace.  Registering
    either a duplicate canonical key or an alias already owned by another
    source raises ``ValueError`` rather than silently changing dispatch.
    """

    def __init__(self, sources: Iterable[UsageSource] | None = None) -> None:
        self._lock = RLock()
        self._sources: dict[str, UsageSource] = {}
        self._names: dict[str, str] = {}
        if sources:
            for source in sources:
                self.register(source)

    def register(self, source: UsageSource, *, key: str | None = None, aliases: Iterable[str] = ()) -> UsageSource:
        source_key = key or getattr(source, "key", None)
        canonical = normalize_source_key(source_key)
        if not canonical:
            raise ValueError("A usage source must have a non-empty canonical key")
        declared_aliases = getattr(source, "aliases", ()) or ()
        if isinstance(declared_aliases, str):
            declared_aliases = (declared_aliases,)
        if isinstance(aliases, str):
            aliases = (aliases,)
        source_aliases = list(declared_aliases) + list(aliases)
        names = [canonical] + [normalize_source_key(alias) for alias in source_aliases]
        if any(not name for name in names):
            raise ValueError("Usage source aliases must be non-empty")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate key or alias in source registration: {names!r}")
        with self._lock:
            conflicts = {name: self._names[name] for name in names if name in self._names}
            if conflicts:
                raise ValueError(f"Usage source key or alias already registered: {conflicts!r}")
            self._sources[canonical] = source
            for name in names:
                self._names[name] = canonical
        return source

    def unregister(self, name: str) -> UsageSource:
        normalized = normalize_source_key(name)
        with self._lock:
            canonical = self._names.get(normalized)
            if canonical is None:
                raise KeyError(name)
            source = self._sources.pop(canonical)
            self._names = {key: owner for key, owner in self._names.items() if owner != canonical}
            return source

    def lookup(self, name: str, default: UsageSource | None = None) -> UsageSource | None:
        with self._lock:
            canonical = self._names.get(normalize_source_key(name))
            return self._sources.get(canonical, default) if canonical else default

    def get(self, name: str) -> UsageSource:
        source = self.lookup(name)
        if source is None:
            raise KeyError(name)
        return source

    def list_sources(self) -> tuple[UsageSource, ...]:
        with self._lock:
            return tuple(self._sources[key] for key in sorted(self._sources))

    def keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._sources))

    def aliases(self, name: str) -> tuple[str, ...]:
        canonical = normalize_source_key(name)
        with self._lock:
            owner = self._names.get(canonical)
            if owner is None:
                raise KeyError(name)
            return tuple(sorted(key for key, value in self._names.items() if value == owner and key != owner))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.lookup(name) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._sources)


SOURCE_REGISTRY = SourceRegistry()


def register_source(source: UsageSource, *, key: str | None = None, aliases: Iterable[str] = ()) -> UsageSource:
    return SOURCE_REGISTRY.register(source, key=key, aliases=aliases)


def get_source(name: str) -> UsageSource:
    return SOURCE_REGISTRY.get(name)


def list_sources() -> tuple[UsageSource, ...]:
    return SOURCE_REGISTRY.list_sources()
