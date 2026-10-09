"""Content-addressed sidecar sink for a normalised toolset: ``<base>/toolsets/<hex>.json``.

A toolset (the tools an agent was offered on a given model call) is written **once** per
distinct fingerprint and referenced from every capture that used it by ``toolset_ref``, instead
of inlining the full schema into every capture -- a real toolset can be tens of KB against a
capture bundle averaging a few KB. This module is the storage layer only: it does not normalise
tools or compute the fingerprint (that is :mod:`evalshift.capture.toolset`); the model-call
recorders in :mod:`evalshift.capture.api` call it, via that module's ``_stamp_toolset`` helper.

Path layout is ``<base>/toolsets/<hex>.json``, where ``<hex>`` is the fingerprint with its
``sha256:`` prefix stripped. The prefixed ``sha256:...`` form is what a ``toolset_ref`` on a
capture carries -- it never appears in a path. ``base`` resolves in the same order, via the same
shared helper, as :class:`~evalshift.sinks.file.FileSink`: an explicit constructor argument wins,
else the ``EVALSHIFT_DIR`` env var, else ``.evalshift`` relative to the current working
directory. There is deliberately **no** repo-root walk (D-deps).

Content-addressed, so the write is idempotent by construction: if the sidecar already exists, its
content is correct by definition (its name *is* the hash of its content), and the write is
skipped -- the existing ref is simply handed back. Otherwise the write is atomic: a temp file is
created in the **same** directory (so the final rename stays on one filesystem and is therefore
atomic on POSIX), written, then moved into place with :func:`os.replace`. A concurrent reader --
including another agent process sharing the same ``.evalshift`` directory -- can therefore only
ever observe no file or a complete one, never a partially written one.

Sidecars are written ``0o644`` (owner read/write, group/other read), set via :func:`os.fchmod`
on the temp file's descriptor immediately after :func:`tempfile.mkstemp` and before anything is
written to it. ``mkstemp`` defaults new files to ``0o600`` (owner-only) -- unlike
:class:`~evalshift.sinks.file.FileSink`'s plain ``path.write_text``, whose umask-derived mode is
empirically ``0o644`` under the common ``022`` umask. Left uncorrected, captures would end up
readable by a principal their sidecar is not, which surfaces as a confusing "toolset missing"
rather than a permissions error in any topology where the recording and evaluating processes run
as different OS users (a split record/eval CI pipeline is the realistic case). The mode is set on
the descriptor via ``fchmod`` rather than threaded through ``open()`` because ``fchmod`` is not
filtered by the process umask -- deterministic regardless of the caller's environment -- and,
unlike reading ``os.umask()`` to compute a mode, touches no global process state and is
thread-safe. A failed ``fchmod`` aborts the write like any other ``OSError`` in this block (see
below) rather than proceeding with a mode that could not be verified.

If ``fchmod`` -- or, hypothetically, :func:`os.fdopen` itself -- raises before the descriptor is
handed off, the raw file descriptor from :func:`tempfile.mkstemp` is closed explicitly in a
``finally``, since otherwise it leaks: this sink runs once per model call, so on a filesystem
where ``fchmod`` consistently fails (a FUSE or network mount in CI is the realistic case), a
long-running agent process would leak one descriptor per call until it hit ``EMFILE`` --
and, being fail-open, would show no symptom until unrelated file operations started failing.
Once :func:`os.fdopen` returns, ownership of the descriptor has transferred to the returned file
object, and the ``with`` block wrapping it closes that descriptor exactly once; closing it again
afterward would be a bug -- in the worst case, closing an unrelated descriptor that has since
reused the same number -- so the explicit close is guarded to run only when that handoff never
happened.

The empty toolset (an agent offered no tools) is a real, first-class value here exactly as it is
in :func:`evalshift.capture.toolset.normalize_tools`: it fingerprints, gets a real sidecar
(``"tools": []``), and gets a real ref like any other toolset. Nothing in this module branches on
"no tools" as if it meant "nothing to write."

Tool definitions are config, not payload -- the same class as ``generation_config`` -- so, like
``generation_config``, they are never redacted; this sink has no redaction hook, by design.

Graceful degradation matches :class:`~evalshift.sinks.file.FileSink`: a filesystem ``OSError``
(read-only mount, disk full, permission denied) is swallowed and logged at ``debug`` via
:func:`evalshift.safety.guard` -- not :func:`evalshift.safety.fail_open`, which yields ``None``
as a context manager and cannot hand back a value. The write degrades to ``None`` rather than
crashing the host agent.

Stdlib only (D-deps).
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from typing import Any

from evalshift.safety import guard
from evalshift.sinks._paths import resolve_base

#: The sidecar's own schema version -- independent of the capture envelope's SCHEMA_VERSION
#: (``evalshift.trace.schema``). Bump only if the sidecar's on-disk shape itself changes.
SIDECAR_SCHEMA_VERSION = "1.0.0"

#: Mode forced onto every sidecar via ``os.fchmod`` (see module docstring): owner read/write,
#: group/other read. Overrides ``tempfile.mkstemp``'s owner-only ``0o600`` default so a sidecar
#: is readable by whoever can already read its sibling capture.
_SIDECAR_MODE = 0o644


def toolset_payload(normalized: list[dict[str, Any]], fingerprint: str) -> str:
    """The exact JSON text of a toolset sidecar for ``normalized`` under ``fingerprint``.

    Shared by :class:`ToolsetSink` (local disk) and
    :class:`~evalshift.sinks.object_store.ObjectStoreSink` (remote object store) so the two
    layouts stay byte-identical: the CLI reads a sidecar the same way wherever it came from.
    """
    return json.dumps(
        {
            "schema_version": SIDECAR_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "tools": normalized,
        },
        ensure_ascii=False,
    )


class ToolsetSink:
    """Write a normalised toolset to ``<base>/toolsets/<hex>.json``, once per distinct toolset."""

    def __init__(self, base: str | os.PathLike[str] | None = None) -> None:
        self._base = base

    def write(self, normalized: list[dict[str, Any]], fingerprint: str) -> str | None:
        """Persist ``normalized`` under its content address; return ``fingerprint`` as the ref.

        ``fingerprint`` is trusted to already be
        ``evalshift.capture.toolset.fingerprint_tools(normalized)`` -- this sink stores the pair
        it is given; it does not recompute or validate the hash. Returns ``None`` when the write
        degrades on a filesystem ``OSError``, so the caller can leave a capture's ``toolset_ref``
        unstamped rather than pointing at a sidecar that was never written.
        """
        return guard("toolset sink write", lambda: self._write(normalized, fingerprint))

    def _write(self, normalized: list[dict[str, Any]], fingerprint: str) -> str:
        target_dir = (resolve_base(self._base) / "toolsets").absolute()
        path = target_dir / f"{fingerprint.removeprefix('sha256:')}.json"
        if path.exists():
            return fingerprint  # content-addressed: existing content is correct by construction

        target_dir.mkdir(parents=True, exist_ok=True)
        payload = toolset_payload(normalized, fingerprint)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=".", suffix=".json.tmp")
        # Bare descriptor: this function owns tmp_fd, and must close it itself, until
        # os.fdopen() below hands it to a file object. Flipped to False the instant that handoff
        # completes (see the `with` block) so the finally knows whether it is still on the hook.
        fd_needs_close = True
        try:
            # Before anything is written: fchmod sets the mode on the descriptor directly, so
            # it is exact and umask-independent (see module docstring), unlike a mode threaded
            # through open(). If this raises, we deliberately do not fall back to writing with
            # mkstemp's owner-only 0o600 default -- that would silently reproduce the unreadable-
            # sidecar bug this exists to fix, with no signal beyond a debug log. Instead the
            # OSError falls through to the same cleanup and propagates to guard() in write(),
            # same as a failure in the write or replace below. tmp_fd is still bare at this
            # point, so the finally below closes it.
            os.fchmod(tmp_fd, _SIDECAR_MODE)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                # os.fdopen() returned: tmp_fd is no longer ours to close. `handle` owns it now,
                # and this `with` block's __exit__ closes it exactly once on every path out of
                # the block below (success or exception) -- set this before doing anything else
                # that could raise, so that path is covered too.
                fd_needs_close = False
                handle.write(payload)
            os.replace(tmp_name, path)
        finally:
            if fd_needs_close:
                # fchmod, or os.fdopen() itself, raised before tmp_fd could be handed to a file
                # object -- nothing else will ever close it, so it is still this function's job.
                # suppress(OSError) tolerates the rare case where os.fdopen()'s underlying
                # io.open() already closed tmp_fd itself while unwinding a partially constructed
                # wrapper layer before raising -- without masking the original exception, which
                # propagates out of this finally unchanged either way.
                with suppress(OSError):
                    os.close(tmp_fd)
            # On success os.replace already moved tmp_name to path, so this is a harmless
            # FileNotFoundError no-op. On any failure it drops the half-written temp file instead
            # of littering <base>/toolsets/ with it. Either way the original exception (if any)
            # propagates unchanged to the guard() in write().
            with suppress(OSError):
                os.remove(tmp_name)
        return fingerprint


__all__ = ["SIDECAR_SCHEMA_VERSION", "ToolsetSink", "toolset_payload"]
