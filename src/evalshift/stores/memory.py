"""In-memory ``ObjectStore`` for tests and for hosts that ship captures themselves.

Lock-guarded so threaded tools writing concurrently cannot lose a put. ``objects`` returns a
snapshot copy, never the live dict.

Stdlib only (D-deps).
"""

from __future__ import annotations

import threading


class MemoryStore:
    """Keep objects in a process-local dict keyed by object key."""

    uri = "memory://"

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self._objects[key] = data

    @property
    def objects(self) -> dict[str, bytes]:
        """A snapshot of every object put so far (key -> bytes)."""
        with self._lock:
            return dict(self._objects)


__all__ = ["MemoryStore"]
