"""EvalShift capture SDK.

Import name ``evalshift`` (distribution ``evalshift-sdk``). Records agent behavior in-process
and writes CLI-valid traces to disk. Capture is off unless ``EVALSHIFT_CAPTURE=1``.

Public surface: the ``capture`` decorator, the ``record_model_call`` helper, the programmatic
``configure`` entry point, the ``default_redactor`` reachable via the required ``redact=True``,
the built-in ``FileSink`` / ``MemorySink`` / ``ObjectStoreSink`` (the last also selected by
``EVALSHIFT_SINK``), and ``flush_captures`` to wait for queued object-store uploads.

Read side (tooling): ``load_capture`` / ``load_envelope`` read and upgrade a written capture to
the current schema version; ``register_migration`` plugs in a step for a future version;
``MigrationError`` is the base of the typed read errors.
"""

from __future__ import annotations

from evalshift.capture.api import capture, record_model_call
from evalshift.config import configure, flush_captures
from evalshift.redaction import Redactor, RedactSetting, default_redactor
from evalshift.sinks.file import FileSink
from evalshift.sinks.memory import MemorySink
from evalshift.sinks.object_store import ObjectStoreSink
from evalshift.trace.migrate import (
    MigrationError,
    load_capture,
    load_envelope,
    register_migration,
)
from evalshift.trace.schema import SCHEMA_VERSION

__version__ = "0.5.0"

__all__ = [
    "SCHEMA_VERSION",
    "FileSink",
    "MemorySink",
    "MigrationError",
    "ObjectStoreSink",
    "RedactSetting",
    "Redactor",
    "__version__",
    "capture",
    "configure",
    "default_redactor",
    "flush_captures",
    "load_capture",
    "load_envelope",
    "record_model_call",
    "register_migration",
]
