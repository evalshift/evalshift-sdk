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
from evalshift.stores.uri import STORE_URI_FORMS, MissingStoreDependencyError, open_store

#: Env var that gates capture on/off.
CAPTURE_ENV = "EVALSHIFT_CAPTURE"

#: Env vars that set the built-in hygiene defaults (overridden by an explicit ``configure(...)``).
MAX_CAPTURES_ENV = "EVALSHIFT_MAX_CAPTURES"
CAPTURE_TTL_ENV = "EVALSHIFT_CAPTURE_TTL"
DEDUP_ENV = "EVALSHIFT_DEDUP"
SAMPLE_RATE_ENV = "EVALSHIFT_SAMPLE_RATE"

#: Env var naming an object store to ship captures to (``s3://`` / ``gs://`` / ``az://``). Same
#: name and meaning as the CLI's ``captures.store``.
CAPTURE_STORE_ENV = "EVALSHIFT_CAPTURE_STORE"

#: The 0.5.0 name of :data:`CAPTURE_STORE_ENV`. Still read, silently, when the new name is unset
#: or blank, so deployments that set it keep working.
LEGACY_SINK_ENV = "EVALSHIFT_SINK"

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


class SinkConfigurationError(RuntimeError):
    """``EVALSHIFT_CAPTURE_STORE`` names a store the SDK cannot build.

    Recorded when the process-wide config is built (at ``import evalshift``) and raised by
    :func:`require_sink_ready` at the first explicit touch -- a ``capture.*`` decorator, a client
    wrapper, the LangChain handler, or :func:`configure` without an explicit ``sink`` -- while
    ``EVALSHIFT_CAPTURE`` is on. Never raised on bare import, never when capture is off. The
    message names the variable, the scheme and the fix; never the value, which may carry a
    pasted credential.
    """


def _store_env() -> tuple[str, str] | None:
    """The object-store URI from the environment, and the name of the variable that held it.

    ``EVALSHIFT_CAPTURE_STORE`` wins; ``EVALSHIFT_SINK`` (its 0.5.0 name) is read only when the
    new name is unset or blank. Blank counts as unset. The name comes back so every message
    points at the variable the user actually set.
    """
    for name in (CAPTURE_STORE_ENV, LEGACY_SINK_ENV):
        raw = os.environ.get(name, "").strip()
        if raw:
            return name, raw
    return None


def _env_sink() -> Sink | None:
    """Build the sink ``EVALSHIFT_CAPTURE_STORE`` (or ``EVALSHIFT_SINK``) names, or ``None`` for
    the default ``FileSink``.

    The store's client is built lazily, so this never touches the network or the credential
    chain.

    Raises:
        SinkConfigurationError: when the value is set but unusable. The text never includes the
            raw value, directly or through a chained cause: several of the parser's grammar
            errors quote the URI they reject, so every branch raises ``from None`` with a fixed
            message naming only the variable, the scheme, the accepted forms or the package to
            install. ``from None`` only suppresses the display of a chained ``__cause__`` here;
            it is :meth:`_Config.__post_init__` that clears the caught instance's
            ``__context__`` and ``__traceback__`` before storing it as ``sink_error``, and any
            other caller holding onto a caught ``SinkConfigurationError`` must clear both the
            same way first.
    """
    found = _store_env()
    if found is None:
        return None
    name, raw = found
    try:
        return ObjectStoreSink(open_store(raw))
    except MissingStoreDependencyError as exc:
        raise SinkConfigurationError(
            f"{name} points at an object store ({exc.scheme}://), but {exc.package} is not "
            f"installed. Run: pip install {exc.packages}, or unset {name} to capture to "
            "local disk."
        ) from None
    except ValueError:
        raise SinkConfigurationError(
            f"{name} is not a valid store URI. Accepted forms: {STORE_URI_FORMS}. "
            "Credentials never go in the URI."
        ) from None
    except Exception as exc:
        # Not a grammar or missing-library error, so its text is unknown and may quote the value.
        raise SinkConfigurationError(
            f"{name} could not be opened ({type(exc).__name__}). Unset it to capture to local disk."
        ) from None


@dataclass
class _Config:
    """Process-wide capture configuration, mutated by :func:`configure`.

    ``sink`` defaults from ``EVALSHIFT_CAPTURE_STORE``, or its 0.5.0 name ``EVALSHIFT_SINK``: an
    object-store URI selects an :class:`~evalshift.sinks.object_store.ObjectStoreSink`; unset or
    blank leaves it ``None``, i.e. the default ``FileSink``; an unusable value leaves it ``None``
    and records ``sink_error`` instead. An explicit ``configure(sink=...)`` still wins and clears
    ``sink_error``.
    Hygiene fields (``dedup`` / ``max_captures`` / ``capture_ttl`` / ``sample_rate``) default from
    env vars so a host that never calls :func:`configure` still gets bounded ``captures/``. A fault
    while reading any env var fails open to the built-in default via :func:`safety.guard`.
    """

    sink: Sink | None = None
    sink_error: SinkConfigurationError | None = None
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

    def __post_init__(self) -> None:
        if self.sink is not None or self.sink_error is not None:
            return
        try:
            self.sink = _env_sink()
        except SinkConfigurationError as exc:
            exc.__context__ = None  # the parser's error may quote the value
            exc.__traceback__ = None  # frames of _env_sink hold the raw value in locals
            self.sink_error = exc  # raised later by require_sink_ready(); import stays safe
        except Exception:  # pragma: no cover - _env_sink converts everything; belt and braces
            safety.logger.debug("evalshift: sink env failed (swallowed)", exc_info=True)


#: The live configuration. Reset between tests via :func:`reset_config`.
_CONFIG: _Config = _Config()


#: Whether the once-per-process "capture disabled" warning has been logged. Reset by reset_config().
_SINK_BLOCK_WARNED = False


def _gate_on() -> bool:
    """The raw gate: ``EVALSHIFT_CAPTURE`` is set to a truthy value."""
    return os.environ.get(CAPTURE_ENV, "").strip().lower() in _TRUTHY


def _sink_blocked() -> bool:
    """True while a recorded store-variable error must stop captures; warns once per process."""
    global _SINK_BLOCK_WARNED
    if _CONFIG.sink_error is None:
        return False
    if not _SINK_BLOCK_WARNED:
        _SINK_BLOCK_WARNED = True
        safety.logger.warning("evalshift: capture disabled: %s", _CONFIG.sink_error)
    return True


def is_capture_enabled() -> bool:
    """True iff ``EVALSHIFT_CAPTURE`` is truthy and no unusable store variable blocks capture.

    A sink the user asked for is never silently replaced by local disk: with the gate on and a
    recorded sink error this is ``False`` (one ``WARNING`` per process), so every entry point
    behaves as if capture were off. Normally the error is raised earlier, at startup, by
    :func:`require_sink_ready`; this is the backstop for a gate that turned on after that.
    """
    return _gate_on() and not _sink_blocked()


def require_sink_ready() -> None:
    """Raise a fresh ``SinkConfigurationError`` if one is recorded and capture is on; else a no-op.

    Every explicit touch point calls this -- the ``capture.*`` decorators, the client wrappers,
    the LangChain handler and :func:`configure` without a ``sink`` -- so in a real agent the
    raise lands at process start, in the deploy logs, before any traffic. A new instance is
    raised each time rather than re-raising the stored one: raising mutates an exception's
    ``__traceback__`` (frames can hold a caller's locals, e.g. an API key, alive) and, without
    ``from None``, its ``__context__`` would pick up whatever the caller is already handling.
    The module-global record must stay untouched across repeated calls.
    """
    if _CONFIG.sink_error is not None and _gate_on():
        raise SinkConfigurationError(*_CONFIG.sink_error.args) from None


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
        _CONFIG.sink_error = None  # an explicit sink replaces whatever the store env var named
    else:
        require_sink_ready()
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
    global _CONFIG, _SINK_BLOCK_WARNED
    _CONFIG = _Config()
    _SINK_BLOCK_WARNED = False
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
    "CAPTURE_STORE_ENV",
    "LEGACY_SINK_ENV",
    "SinkConfigurationError",
    "active_sink",
    "configure",
    "flush_captures",
    "is_capture_enabled",
    "require_model_call",
    "require_sink_ready",
    "reset_config",
    "should_capture_now",
    "toolset_writer",
]
