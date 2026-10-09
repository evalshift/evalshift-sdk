"""Sink that ships captures to a user-owned object store (S3, GCS, Azure Blob, ...).

Local disk is the SDK's default and stays so. This sink exists for hosts whose disk does not
survive them -- Fargate tasks, Lambda environments, pods -- where a capture written to
``.evalshift/`` is gone the moment the process is. Configure it with ``EVALSHIFT_SINK=<uri>``
(see :mod:`evalshift.config`) or ``configure(sink=ObjectStoreSink(store))``.

**Key layout is the local layout.** ``captures/<safe_suite>/<capture_id>.json`` and
``toolsets/<hex>.json`` under the store's prefix -- byte for byte what
:class:`~evalshift.sinks.file.FileSink` and :class:`~evalshift.sinks.toolset.ToolsetSink`
write, so the CLI mirrors a bucket into ``.evalshift/`` and reads it unchanged.

**Background by default.** ``write`` enqueues and returns; one daemon thread drains the queue
with ``store.put``. The queue is bounded (``queue_size``); when it is full the incoming item is
dropped with a debug line -- never block the agent. ``flush(timeout)`` waits for the queue to
drain; an exit flush (``flush_timeout``) is registered with :mod:`atexit` the first time the
worker starts, and logs one ``WARNING`` if it times out with items still pending. Python's default
``SIGTERM`` handling skips ``atexit``, so hosts that are stopped by signal must either install a
handler that calls ``sys.exit(0)`` or call :func:`evalshift.flush_captures` in their own
shutdown hook; on Lambda, where background threads freeze between invocations, call it before
the handler returns or construct the sink with ``background=False``.

**Fail-open.** A raising ``put`` is caught here: the first failure per sink logs at ``WARNING``
with the store URI (a wrong IAM role dropping every capture must not be silent), later ones at
``debug``. ``write`` always returns ``None`` -- there is no local path -- so
:class:`~evalshift.sinks.hygiene.HygieneSink`'s GC is correctly a no-op while its dedup still
applies. No retry layer: boto3, the GCS client and the Azure client each retry already.

Stdlib only (D-deps): the cloud client lives inside the store object the caller hands in.
"""

from __future__ import annotations

import atexit

# Imported for its side effect, not used here. ``concurrent.futures.thread`` calls
# ``threading._register_atexit`` when first imported, and boto3 imports it (via s3transfer) on
# the first put. When that first put runs in the exit flush, interpreter shutdown has already
# begun, the registration raises ``RuntimeError: can't register atexit after shutdown`` and the
# capture is dropped. Importing it here, while the process is alive, makes the later import a
# no-op. Stdlib, so D-deps holds.
import concurrent.futures.thread  # noqa: F401
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

from evalshift.safety import logger
from evalshift.sinks.file import _safe_segment
from evalshift.sinks.toolset import toolset_payload
from evalshift.stores.base import ObjectStore
from evalshift.trace.models import CaptureEnvelope
from evalshift.trace.serialize import capture_filename, dumps

# (key, data, fingerprint): the fingerprint is set for toolset sidecars and ``None`` for captures,
# so a failed background sidecar put can release it for retry.
_Item = tuple[str, bytes, str | None]


class ObjectStoreSink:
    """Write capture envelopes and toolset sidecars to an :class:`ObjectStore`."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        background: bool = True,
        queue_size: int = 1000,
        flush_timeout: float = 10.0,
    ) -> None:
        self._store = store
        self._background = background
        self._flush_timeout = flush_timeout
        self._queue: queue.Queue[_Item] = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)
        self._pending = 0
        self._worker: threading.Thread | None = None
        self._atexit_registered = False
        self._seen_toolsets: set[str] = set()
        self._failures = 0

    @property
    def store(self) -> ObjectStore:
        """The store this sink writes to."""
        return self._store

    def write(self, envelope: CaptureEnvelope) -> Path | None:
        """Ship ``envelope`` under ``captures/<safe_suite>/<capture_id>.json``; return ``None``."""
        key = f"captures/{_safe_segment(envelope.suite)}/{capture_filename(envelope.capture_id)}"
        self._submit(key, dumps(envelope).encode("utf-8"))
        return None

    def write_toolset(self, normalized: list[dict[str, Any]], fingerprint: str) -> str | None:
        """Ship the sidecar for ``fingerprint`` once per process; return the ref, or ``None``.

        "Once" is a per-process set: the put is idempotent across processes anyway because the
        key is the content hash. Returns ``None`` when the item could not be accepted (queue
        full, or an inline put failed) so the caller leaves ``toolset_ref`` unstamped rather than
        pointing at a sidecar that was never written. Accepted means *enqueued*, not uploaded;
        if the background put later fails, the worker releases the fingerprint so the next call
        for it enqueues the sidecar again. Captures stamped before that retry lands point at a
        missing sidecar, and the CLI refuses to promote them, exactly as it refuses an unstamped
        one today.
        """
        with self._lock:
            if fingerprint in self._seen_toolsets:
                return fingerprint
        key = f"toolsets/{fingerprint.removeprefix('sha256:')}.json"
        data = toolset_payload(normalized, fingerprint).encode("utf-8")
        # Mark as seen *before* submitting: the worker may fail the put and release the
        # fingerprint before ``_submit`` even returns, and that release must not be undone.
        with self._lock:
            if fingerprint in self._seen_toolsets:
                return fingerprint
            self._seen_toolsets.add(fingerprint)
        if not self._submit(key, data, fingerprint):
            with self._lock:
                self._seen_toolsets.discard(fingerprint)
            return None
        return fingerprint

    def flush(self, timeout: float | None = None) -> bool:
        """Block until every queued item has been put, or ``timeout`` seconds pass.

        Returns ``True`` when the queue drained, ``False`` on timeout. Immediate ``True`` when
        nothing is pending (including in inline mode).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._drained:
            while self._pending:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._drained.wait(remaining)
        return True

    # --- internals -----------------------------------------------------------------------

    def _submit(self, key: str, data: bytes, fingerprint: str | None = None) -> bool:
        """Put inline, or enqueue for the worker. Returns whether the item was accepted.

        ``fingerprint`` is passed for toolset sidecars only; see :meth:`_run`.
        """
        if not self._background:
            return self._put(key, data)
        self._ensure_worker()
        with self._lock:
            try:
                self._queue.put_nowait((key, data, fingerprint))
            except queue.Full:
                logger.debug("evalshift: object store queue full; dropped %s", key)
                return False
            self._pending += 1
        return True

    def _put(self, key: str, data: bytes) -> bool:
        """One ``store.put``, fail-open. First failure per sink is a WARNING, later ones debug."""
        try:
            self._store.put(key, data)
        except Exception as exc:
            with self._lock:
                self._failures += 1
                first = self._failures == 1
            logger.log(
                logging.WARNING if first else logging.DEBUG,
                "evalshift: object store put to %s failed for %s: %s: %s (capture dropped)",
                getattr(self._store, "uri", "<store>"),
                key,
                type(exc).__name__,
                exc,
                exc_info=not first,
            )
            return False
        return True

    def _ensure_worker(self) -> None:
        """Start the daemon worker (and register the exit flush) on first use."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run, name="evalshift-object-store", daemon=True
            )
            self._worker.start()
            if not self._atexit_registered:
                atexit.register(self._flush_at_exit)
                self._atexit_registered = True

    def _flush_at_exit(self) -> None:
        """The :mod:`atexit` hook: flush, and say so once if anything is left behind.

        A timeout here is the last chance to report loss -- the process is about to take the
        queue with it -- so it is a WARNING naming the store and the backlog, never silent.
        """
        if self.flush(self._flush_timeout):
            return
        with self._lock:
            pending = self._pending
        logger.warning(
            "evalshift: object store flush to %s timed out after %.1fs with %d item(s) pending "
            "(dropped at exit)",
            getattr(self._store, "uri", "<store>"),
            self._flush_timeout,
            pending,
        )

    def _run(self) -> None:
        while True:
            key, data, fingerprint = self._queue.get()
            try:
                if not self._put(key, data) and fingerprint is not None:
                    # Release the sidecar so the next ``write_toolset`` for it retries the put.
                    with self._lock:
                        self._seen_toolsets.discard(fingerprint)
            finally:
                with self._drained:
                    self._pending -= 1
                    if self._pending == 0:
                        self._drained.notify_all()


__all__ = ["ObjectStoreSink"]
