"""Small process-local cache for parsed files that change infrequently."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock
from typing import Callable, Generic, Iterable, TypeVar

T = TypeVar("T")
FileSignature = tuple[int, int, int, int, int]
Parser = Callable[[Path], tuple[T, bool]]


@dataclass(slots=True)
class _CacheEntry(Generic[T]):
    signature: FileSignature
    value: T


class ParsedFileCache(Generic[T]):
    """Cache stable parse results by resolved path and complete file identity.

    ``parser`` returns ``(result, read_succeeded)`` so callers can keep the
    existing best-effort behavior for partial reads without caching them.
    Parser work and file stats happen outside the shared map lock. Requests
    parsing the same path at once share one in-flight parse.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _CacheEntry[T]] = OrderedDict()
        self._inflight: dict[str, Event] = {}
        self._lock = RLock()

    @staticmethod
    def _resolved_path(path: Path) -> Path:
        return path.expanduser().resolve()

    @staticmethod
    def _signature(path: Path) -> FileSignature | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    def _invalidate(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def retain_paths(self, paths: Iterable[Path]) -> None:
        """Drop entries for files no longer found during source discovery."""
        live_paths = {str(self._resolved_path(Path(path))) for path in paths}
        with self._lock:
            for key in tuple(self._entries):
                if key not in live_paths:
                    self._entries.pop(key, None)

    def parse(self, path: Path, parser: Parser[T]) -> T:
        """Return an isolated cached parse, or parse and cache a stable read."""
        resolved_path = self._resolved_path(Path(path))
        key = str(resolved_path)
        signature = self._signature(resolved_path)
        if signature is None:
            self._invalidate(key)
            value, _ = parser(resolved_path)
            return deepcopy(value)

        while True:
            cached_entry: _CacheEntry[T] | None = None
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.signature == signature:
                    self._entries.move_to_end(key)
                    cached_entry = entry
                elif entry is not None:
                    self._entries.pop(key, None)

                if cached_entry is None:
                    waiting = self._inflight.get(key)
                    if waiting is None:
                        waiting = Event()
                        self._inflight[key] = waiting
                        is_owner = True
                    else:
                        is_owner = False

            if cached_entry is not None:
                value = deepcopy(cached_entry.value)
                current_signature = self._signature(resolved_path)
                if current_signature == signature:
                    return value
                if current_signature is None:
                    self._invalidate(key)
                    value, _ = parser(resolved_path)
                    return deepcopy(value)
                signature = current_signature
                continue

            if not is_owner:
                waiting.wait()
                signature = self._signature(resolved_path)
                if signature is None:
                    self._invalidate(key)
                    value, _ = parser(resolved_path)
                    return deepcopy(value)
                continue

            try:
                value, read_succeeded = parser(resolved_path)
                after_signature = self._signature(resolved_path)
                if read_succeeded and after_signature == signature:
                    with self._lock:
                        self._entries[key] = _CacheEntry(signature, value)
                        self._entries.move_to_end(key)
                        while len(self._entries) > self._max_entries:
                            self._entries.popitem(last=False)
                return deepcopy(value)
            finally:
                with self._lock:
                    if self._inflight.get(key) is waiting:
                        self._inflight.pop(key, None)
                    waiting.set()
