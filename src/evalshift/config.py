"""The capture gate and the programmatic configuration surface.

Capture is **off by default**: nothing is recorded or written unless ``EVALSHIFT_CAPTURE`` is set
to a truthy value. The gate is read live on every call (never cached at import) so tests and
long-lived hosts can toggle it mid-process.

:func:`configure` registers process-wide options programmatically: ``sink`` swaps where captures
go, ``sample_rate`` / ``dedup`` / ``max_captures`` / ``capture_ttl`` bound the firehose, and
``require_model_call`` gates content-free captures. It has **merge** semantics: only the keyword
arguments you pass are changed. :func:`reset_config` clears everything back to defaults.

Redaction is deliberately **not** here. ``redact=`` is a required keyword on each capture entry
point instead (D-4c), so masking is decided and reviewable at the call site rather than by
process-wide state that can be set from another module, or after import.

:func:`active_sink` is the swap seam the capture layer routes every write through: it returns the
``configure``-registered sink, falling back to a default :class:`~evalshift.sinks.file.FileSink`.

Stdlib only (D-deps).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from evalshift import safety
from evalshift.hygiene import dedup
from evalshift.hygiene.sample import should_capture
from evalshift.sinks.base import Sink
from evalshift.sinks.file import FileSink
from evalshift.sinks.hygiene import HygieneSink
from evalshift.sinks.object_store import ObjectStoreSink
from evalshift.sinks.toolset import ToolsetSink
from evalshift.stores.uri import STORE_URI_FORMS, MissingExtraError, open_store

#: Env var that gates capture on/off.
CAPTURE_ENV = "EVALSHIFT_CAPTURE"

#: Env vars that set the built-in hygiene defaults (overridden by an explicit ``configure(...)``).
MAX_CAPTURES_ENV = "EVALSHIFT_MAX_CAPTURES"
CAPTURE_TTL_ENV = "EVALSHIFT_CAPTURE_TTL"
DEDUP_ENV = "EVALSHIFT_DEDUP"
SAMPLE_RATE_ENV = "EVALSHIFT_SAMPLE_RATE"

#: Env var naming an object store to ship captures to (``s3://`` / ``gs://`` / ``az://``).
SINK_ENV = "EVALSHIFT_SINK"

#: Values (case-insensitive, stripped) that count as "capture on". Everything else is off.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Values (case-insensitive, stripped) that explicitly mean "no cap / not set" for numeric knobs.
_UNCAPPED = frozenset({"0", "none", "unlimited", "off"})

#: Sentinel for "argument not provided" so ``configure(...)`` can merge without clobbering.
_UNSET: Any = object()

#: Built-in defaults when the corresponding env var is unset. Bounded-but-generous so a host that
#: never calls :func:`configure` still keeps ``captures/`` from growing without limit.
_DEFAULT_DEDUP = True
_DEFAULT_MAX_CAPTURES = 200


def _env_bool(name: str, default: bool) -> bool:
    """Parse a truthy/falsy env var; unset or malformed -> ``default`` (fail-open)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _UNCAPPED:
        return False
    return default


def _env_optional_int(name: str, default: int | None) -> int | None:
    """Parse an int env var; ``0``/``none``/``unlimited`` -> ``None`` (uncapped); bad -> default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in _UNCAPPED or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return default
    return None if value <= 0 else value


def _env_optional_float(name: str, default: float | None) -> float | None:
    """Parse a float env var; ``none``/``unlimited``/empty -> ``None``; bad value -> default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in _UNCAPPED or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return default


def _env_sink() -> Sink | None:
    """Build the sink ``EVALSHIFT_SINK`` names, or ``None`` for the default ``FileSink``.

    Fail-open with a *warning*, not a debug line: a misconfigured remote sink on an ephemeral
    host means every capture is lost, and the one place that can say so is here, once, at
    config construction. Blank counts as unset. The store's client is built lazily, so this
    never touches the network or the credential chain.

    The warning never includes the raw value, directly or through an exception: an
    ``EVALSHIFT_SINK`` holding a SAS token, an Azure connection string (``AccountKey=...``) or
    inline credentials would otherwise leak into logs. Several of the parser's grammar errors
    quote the URI they reject, so a ``ValueError`` gets a fixed message naming only the accepted
    forms. A :class:`~evalshift.stores.uri.MissingExtraError` is logged as-is: its text names
    only the scheme, the missing module and the pip extra to install. Any other exception gets
    a fixed warning too, without its text.
    """
    raw = os.environ.get(SINK_ENV, "").strip()
    if not raw:
        return None
    try:
        return ObjectStoreSink(open_store(raw))
    except MissingExtraError as exc:
        safety.logger.warning(
            "evalshift: %s ignored (%s); captures are written to local disk instead",
            SINK_ENV,
            exc,
        )
    except ValueError:
        safety.logger.warning(
            "evalshift: %s is not a valid store URI (accepted forms: %s); "
            "captures are written to local disk instead",
            SINK_ENV,
            STORE_URI_FORMS,
        )
    except Exception:
        # Not a grammar or missing-extra error, so its text is unknown and may quote the value:
        # log a fixed line only. Without this the outer guard would swallow it at debug and
        # EVALSHIFT_SINK would be ignored silently.
        safety.logger.warning(
            "evalshift: %s could not be opened (unexpected error); "
            "captures are written to local disk instead",
            SINK_ENV,
        )
    return None


@dataclass
class _Config:
    """Process-wide capture configuration, mutated by :func:`configure`.

    ``sink`` defaults from ``EVALSHIFT_SINK`` (an object-store URI selects an
    :class:`~evalshift.sinks.object_store.ObjectStoreSink`; unset, blank or invalid leaves it
    ``None``, i.e. the default ``FileSink``); an explicit ``configure(sink=...)`` still wins.
    Hygiene fields (``dedup`` / ``max_captures`` / ``capture_ttl`` / ``sample_rate``) default from
    env vars so a host that never calls :func:`configure` still gets bounded ``captures/``. A fault
    while reading any env var fails open to the built-in default via :func:`safety.guard`.
    """

    sink: Sink | None = field(default_factory=lambda: safety.guard("sink env", _env_sink))
    sample_rate: float | None = field(
        default_factory=lambda: _env_optional_float(SAMPLE_RATE_ENV, None)
    )
    dedup: bool = field(default_factory=lambda: _env_bool(DEDUP_ENV, _DEFAULT_DEDUP))
    max_captures: int | None = field(
        default_factory=lambda: _env_optional_int(MAX_CAPTURES_ENV, _DEFAULT_MAX_CAPTURES)
    )
    capture_ttl: float | None = field(
        default_factory=lambda: _env_optional_float(CAPTURE_TTL_ENV, None)
    )
    require_model_call: bool = False  # drop captures with no model_call span (opt-in gate)


#: The live configuration. Reset between tests via :func:`reset_config`.
_CONFIG: _Config = _Config()


def is_capture_enabled() -> bool:
    """True iff ``EVALSHIFT_CAPTURE`` is set to a truthy value (the off-by-default gate)."""
    return os.environ.get(CAPTURE_ENV, "").strip().lower() in _TRUTHY


def configure(
    *,
    sink: Sink | None = _UNSET,
    sample_rate: float | None = _UNSET,
    dedup: bool = _UNSET,
    max_captures: int | None = _UNSET,
    capture_ttl: float | None = _UNSET,
    require_model_call: bool = _UNSET,
) -> None:
    """Set process-wide capture options. Only the arguments you pass are changed (merge)."""
    if sink is not _UNSET:
        _CONFIG.sink = sink
    if sample_rate is not _UNSET:
        _CONFIG.sample_rate = sample_rate
    if dedup is not _UNSET:
        _CONFIG.dedup = dedup
    if max_captures is not _UNSET:
        _CONFIG.max_captures = max_captures
    if capture_ttl is not _UNSET:
        _CONFIG.capture_ttl = capture_ttl
    if require_model_call is not _UNSET:
        _CONFIG.require_model_call = require_model_call


def reset_config() -> None:
    """Clear all configured options back to defaults (used for test isolation).

    Hygiene knobs are re-read from the environment (via a fresh :class:`_Config`) so tests that
    ``monkeypatch.setenv(...)`` then ``reset_config()`` observe the env-derived defaults.
    """
    global _CONFIG
    _CONFIG = _Config()
    dedup.reset_registry()


def active_sink() -> Sink:
    """Return the sink captures are written to, wrapped with hygiene when any knob is set.

    With no hygiene option configured (``dedup``/``max_captures``/``capture_ttl``) the configured
    or default sink is returned **unchanged** (identity preserved). Otherwise it is wrapped in a
    :class:`~evalshift.sinks.hygiene.HygieneSink` that applies dedup + GC around every write.
    """
    base = _CONFIG.sink if _CONFIG.sink is not None else FileSink()
    if not _CONFIG.dedup and _CONFIG.max_captures is None and _CONFIG.capture_ttl is None:
        return base
    return HygieneSink(
        base,
        dedup=_CONFIG.dedup,
        max_captures=_CONFIG.max_captures,
        capture_ttl=_CONFIG.capture_ttl,
    )


def _toolset_base() -> str | os.PathLike[str] | None:
    """Resolve the base directory a toolset sidecar (:class:`~evalshift.sinks.toolset.ToolsetSink`)
    should be written under, matching whatever on-disk base the active capture sink itself
    resolves to -- so a capture and the sidecar its ``toolset_ref`` points at always land under
    the same root (D-toolset).

    Unwraps a :class:`~evalshift.sinks.hygiene.HygieneSink` (the transparent wrapper
    :func:`active_sink` applies whenever a hygiene knob is set) to inspect the sink underneath.
    Returns the matching :attr:`~evalshift.sinks.file.FileSink.base` when that sink is (or wraps)
    a :class:`~evalshift.sinks.file.FileSink` -- including the unconfigured case, where it is
    ``None`` and the sidecar falls back to the same ``EVALSHIFT_DIR``/CWD resolution ``FileSink``
    itself would use.

    Returns ``None`` -- the ``EVALSHIFT_DIR``/CWD default, unchanged -- for any other sink (a
    :class:`~evalshift.sinks.memory.MemorySink`, or a fully custom one): neither has an on-disk
    base of its own for a sidecar to match. This is a deliberate fallback, not an oversight: the
    sidecar is unconditionally file-based, so a host on a genuinely read-only filesystem using
    ``MemorySink`` for that reason must still point ``EVALSHIFT_DIR`` at a writable mount (e.g.
    ``/tmp``) for its captures to stay promotable -- see DOCS.md's ``MemorySink`` section.
    """
    sink: Sink = active_sink()
    if isinstance(sink, HygieneSink):
        sink = sink.wrapped
    if isinstance(sink, FileSink):
        return sink.base
    return None


ToolsetWriter = Callable[[list[dict[str, Any]], str], "str | None"]


def toolset_writer() -> ToolsetWriter:
    """The function :func:`evalshift.capture.api._stamp_toolset` writes a toolset sidecar with.

    An :class:`~evalshift.sinks.object_store.ObjectStoreSink` (unwrapped from the transparent
    :class:`~evalshift.sinks.hygiene.HygieneSink`) carries its own ``write_toolset`` so the
    sidecar lands in the same store as the capture. Every other sink -- ``FileSink``,
    ``MemorySink``, a custom one -- keeps today's file-based
    :class:`~evalshift.sinks.toolset.ToolsetSink` under :func:`_toolset_base`.
    """
    sink: Sink = active_sink()
    if isinstance(sink, HygieneSink):
        sink = sink.wrapped
    if isinstance(sink, ObjectStoreSink):
        return sink.write_toolset
    return ToolsetSink(base=_toolset_base()).write


def flush_captures(timeout: float | None = None) -> bool:
    """Wait for a background :class:`ObjectStoreSink` to finish uploading; ``True`` otherwise.

    Call this from your own shutdown hook (a server's lifespan shutdown, a worker's stop
    callback) or before a Lambda handler returns. Do not call it from inside a signal handler:
    the handler runs on the main thread and blocks forever on the sink's lock if the signal
    landed while the main thread held it. For ``SIGTERM``, install a handler that calls
    ``sys.exit(0)`` instead; the ``atexit`` flush then runs outside the handler. Sinks with
    nothing to flush -- ``FileSink``, ``MemorySink`` (whose own ``flush`` *drains* and is
    deliberately not called), custom sinks -- return ``True`` immediately.
    """
    sink: Sink = active_sink()
    if isinstance(sink, HygieneSink):
        sink = sink.wrapped
    if isinstance(sink, ObjectStoreSink):
        return sink.flush(timeout)
    return True


def should_capture_now() -> bool:
    """Sampling decision for one agent run (read at agent entry). Fail-open: capture on fault.

    A configured ``sample_rate`` decides the draw; a fault in the draw defaults to **capturing**
    so a sampling bug never silently disables all telemetry.
    """
    decision = safety.guard("sample decision", lambda: should_capture(_CONFIG.sample_rate))
    return decision is not False


def require_model_call() -> bool:
    """Whether the persistence gate is on: drop captures with no ``model_call`` span.

    Off by default (the SDK writes a capture per sampled invocation regardless of content).
    Hosts that want eval-grade captures enable it via ``configure(require_model_call=True)``.
    """
    return _CONFIG.require_model_call


__all__ = [
    "CAPTURE_ENV",
    "SINK_ENV",
    "active_sink",
    "configure",
    "flush_captures",
    "is_capture_enabled",
    "require_model_call",
    "reset_config",
    "should_capture_now",
    "toolset_writer",
]
