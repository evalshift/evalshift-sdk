"""Object stores a capture sink can ship to: the ``ObjectStore`` protocol, the URI grammar and
the in-memory store used by tests. Provider adapters (``evalshift.stores.s3`` / ``.gcs`` /
``.azure``) are not re-exported here so importing this package never touches a cloud SDK."""

from __future__ import annotations

from evalshift.stores.base import ObjectStore
from evalshift.stores.memory import MemoryStore
from evalshift.stores.uri import STORE_URI_FORMS, StoreURI, parse_store_uri

__all__ = ["STORE_URI_FORMS", "MemoryStore", "ObjectStore", "StoreURI", "parse_store_uri"]
