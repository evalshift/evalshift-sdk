"""Capture output sinks: the ``Sink`` protocol plus the built-in ``FileSink``, ``MemorySink`` and
``ObjectStoreSink``."""

from __future__ import annotations

from evalshift.sinks.base import Sink
from evalshift.sinks.file import FileSink
from evalshift.sinks.memory import MemorySink
from evalshift.sinks.object_store import ObjectStoreSink

__all__ = ["FileSink", "MemorySink", "ObjectStoreSink", "Sink"]
