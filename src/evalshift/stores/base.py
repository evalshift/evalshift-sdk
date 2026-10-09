"""The ``ObjectStore`` protocol: a remote place that stores bytes under a string key.

The write side of the SDK needs exactly one operation, ``put``. Listing and reading objects
back is the CLI's job (``evalshift_cli.captures.remote``), which carries its own, wider
protocol; the two are kept in step by the shared key layout and URI grammar, not by shared
code. ``runtime_checkable`` so tests can ``isinstance`` a store.

Stdlib only (D-deps).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...


__all__ = ["ObjectStore"]
