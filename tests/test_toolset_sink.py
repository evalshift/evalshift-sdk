"""Unit tests for the toolset sidecar sink (``evalshift.sinks.toolset.ToolsetSink``).

Fixtures below use the real :func:`~evalshift.capture.toolset.fingerprint_tools` to derive
fingerprints rather than hardcoding pinned hash literals -- this file is testing the *sink*
(storage layer), not the hashing algorithm (already pinned by ``tests/test_toolset.py``), so it
should stay correct even if the algorithm's output changes, as long as sink and fingerprinter
stay in sync -- which is exactly the pairing ``write(normalized, fingerprint)`` assumes.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from evalshift.capture.toolset import fingerprint_tools
from evalshift.sinks.toolset import ToolsetSink

SAMPLE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_schedule",
        "description": "Look up a user's schedule for a given date.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string"}},
            "required": ["date"],
        },
    },
    {
        "name": "add_task",
        "description": "Add a task to the user's to-do list.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
]
SAMPLE_FINGERPRINT = fingerprint_tools(SAMPLE_TOOLS)


def _sidecar_path(base: Path, fingerprint: str) -> Path:
    return base / "toolsets" / f"{fingerprint.removeprefix('sha256:')}.json"


def test_first_write_creates_sidecar(tmp_path: Path) -> None:
    ref = ToolsetSink(base=tmp_path).write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT)

    assert ref == SAMPLE_FINGERPRINT
    path = _sidecar_path(tmp_path, SAMPLE_FINGERPRINT)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "schema_version": "1.0.0",
        "fingerprint": SAMPLE_FINGERPRINT,
        "tools": SAMPLE_TOOLS,
    }


def test_sidecar_is_group_and_other_readable_regardless_of_umask(tmp_path: Path) -> None:
    """The sidecar must stay readable by a different OS user than the one who wrote it -- e.g. a
    split record/eval CI pipeline where the recording and evaluating processes run as different
    users. ``tempfile.mkstemp`` defaults new files to ``0o600`` (owner-only), so the sink must
    force the mode explicitly via ``os.fchmod`` on the fd, which -- unlike a mode passed to
    ``open()`` -- is never filtered by the process umask.

    The test deliberately runs under a restrictive umask (``0o077``, which would mask a
    ``0o644`` request for group/other bits down to ``0o600`` were the mode umask-filtered) so
    that the assertion actually pins "the sink forces 0o644" rather than "0o644 happens to be
    this machine's ambient umask result" -- a bare ``assert mode == 0o644`` without overriding
    the umask would pass or fail depending on who runs it, for reasons unrelated to the sink.
    """
    restrictive_umask = 0o077
    old_umask = os.umask(restrictive_umask)
    try:
        ref = ToolsetSink(base=tmp_path).write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT)
    finally:
        os.umask(old_umask)  # restore immediately -- umask is global process state

    assert ref == SAMPLE_FINGERPRINT
    path = _sidecar_path(tmp_path, SAMPLE_FINGERPRINT)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o644


def test_write_degrades_to_none_when_fchmod_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_fd: int, _mode: int) -> None:
        raise OSError("operation not permitted")

    monkeypatch.setattr(os, "fchmod", boom)

    ref = ToolsetSink(base=tmp_path).write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT)

    assert ref is None
    # Aborts rather than writing with an unverified (possibly owner-only) mode: no destination
    # sidecar and no leftover temp file under <base>/toolsets/ (the directory itself is created
    # up front regardless, same as in test_write_degrades_to_none_on_readonly_dir above).
    assert list(tmp_path.rglob("*.json")) == []
    assert list(tmp_path.rglob("*.tmp")) == []


def test_fchmod_failure_closes_the_temp_file_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion to test_write_degrades_to_none_when_fchmod_fails above: that test proves the
    write degrades to None with no files left behind, but not that the raw fd opened by
    ``tempfile.mkstemp`` -- and never handed to ``os.fdopen()``, since ``fchmod`` raises before
    that call runs -- was actually closed. ``ToolsetSink.write()`` runs once per model call on a
    long-running agent process; on a filesystem where ``fchmod`` consistently fails (a FUSE or
    network mount in CI is the realistic case), an unclosed fd here leaks one descriptor per call
    until the process hits ``EMFILE`` -- and because the sink is fail-open, nothing surfaces
    until unrelated file operations start failing elsewhere.

    Captures the real fd via a ``mkstemp`` spy, then asserts it is no longer open afterwards: a
    closed fd makes ``os.fstat(fd)`` raise ``OSError`` (``EBADF``), so this observes the leak
    directly on the fd itself rather than asserting anything about the sink's internals.
    """
    opened_fds: list[int] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        opened_fds.append(fd)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)

    def boom(_fd: int, _mode: int) -> None:
        raise OSError("operation not permitted")

    monkeypatch.setattr(os, "fchmod", boom)

    ref = ToolsetSink(base=tmp_path).write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT)

    assert ref is None
    assert len(opened_fds) == 1
    with pytest.raises(OSError):
        os.fstat(opened_fds[0])


def test_fdopen_failure_closes_the_temp_file_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same leak, different raise point. ``fchmod`` is left real (it succeeds); ``os.fdopen``
    itself is replaced outright, so it never gets the chance to hand ``tmp_fd`` to a file object
    -- this exercises the "opened but ``fdopen`` does not take ownership" case directly and
    independently of the ``fchmod``-specific test above, matching the shape this already existed
    for (a failing ``fdopen`` was a latent gap even before ``fchmod`` was introduced).
    """
    opened_fds: list[int] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        opened_fds.append(fd)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)

    def boom(_fd: int, *_args: Any, **_kwargs: Any) -> Any:
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(os, "fdopen", boom)

    ref = ToolsetSink(base=tmp_path).write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT)

    assert ref is None
    assert len(opened_fds) == 1
    with pytest.raises(OSError):
        os.fstat(opened_fds[0])
    assert list(tmp_path.rglob("*.json")) == []
    assert list(tmp_path.rglob("*.tmp")) == []


def test_successful_write_never_directly_closes_the_temp_file_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the flip side of the two leak tests above: once ``os.fdopen()`` has handed
    ``tmp_fd`` to a file object, that object -- not the sink -- owns the descriptor, and closing
    it again is a bug (in the worst case, closing an unrelated fd that has since reused the same
    number).

    The real close that happens when the ``with os.fdopen(...)`` block exits is performed
    internally by the C ``_io`` implementation and does not call back through the Python-level
    ``os.close`` -- confirmed empirically (mkstemp's own internal tempdir probing is the only
    thing observed calling it, and only when no ``dir=`` is given, which is not our case here).
    So a spy on ``os.close`` sees zero calls on a correct success path; any call at all here can
    only be an extra, direct close the sink's own cleanup issued on a descriptor it no longer
    owns -- which is exactly the bug this guards against, independent of the sink's internals.
    """
    close_calls: list[int] = []
    real_close = os.close

    def spy_close(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "close", spy_close)

    ref = ToolsetSink(base=tmp_path).write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT)

    assert ref == SAMPLE_FINGERPRINT
    assert close_calls == []


def test_second_write_is_noop_returns_same_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = ToolsetSink(base=tmp_path)
    first = sink.write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT)
    assert first == SAMPLE_FINGERPRINT

    path = _sidecar_path(tmp_path, SAMPLE_FINGERPRINT)
    mtime_before = path.stat().st_mtime_ns

    def boom(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("mkstemp must not run when the sidecar already exists")

    monkeypatch.setattr(tempfile, "mkstemp", boom)

    second = sink.write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT)

    assert second == SAMPLE_FINGERPRINT
    assert path.stat().st_mtime_ns == mtime_before


def test_write_degrades_to_none_on_readonly_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise OSError("read-only file system")

    monkeypatch.setattr(tempfile, "mkstemp", boom)

    ref = ToolsetSink(base=tmp_path).write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT)

    assert ref is None
    assert list(tmp_path.rglob("*.json")) == []


def test_concurrent_writers_converge_without_partial_file(tmp_path: Path) -> None:
    sink = ToolsetSink(base=tmp_path)
    path = _sidecar_path(tmp_path, SAMPLE_FINGERPRINT)
    expected = {
        "schema_version": "1.0.0",
        "fingerprint": SAMPLE_FINGERPRINT,
        "tools": SAMPLE_TOOLS,
    }

    saw_partial = threading.Event()
    stop = threading.Event()

    def reader() -> None:
        # Poll the target path throughout the writer burst. A torn/partial write would surface
        # here as either a read of a truncated file (JSONDecodeError) or an OS-level race
        # (OSError); a true atomic rename can never produce either.
        while not stop.is_set():
            if path.exists():
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    saw_partial.set()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(
                pool.map(lambda _: sink.write(SAMPLE_TOOLS, SAMPLE_FINGERPRINT), range(32))
            )
    finally:
        stop.set()
        reader_thread.join()

    assert not saw_partial.is_set()
    assert results == [SAMPLE_FINGERPRINT] * 32
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    # Exactly one file: no stray temp files left behind under <base>/toolsets/.
    assert list((tmp_path / "toolsets").iterdir()) == [path]


def test_empty_toolset_gets_real_sidecar_and_ref(tmp_path: Path) -> None:
    empty_fingerprint = fingerprint_tools([])

    ref = ToolsetSink(base=tmp_path).write([], empty_fingerprint)

    assert ref == empty_fingerprint
    assert ref is not None
    path = _sidecar_path(tmp_path, empty_fingerprint)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": "1.0.0",
        "fingerprint": empty_fingerprint,
        "tools": [],
    }


def test_toolset_payload_matches_what_the_sink_writes(tmp_path: Path) -> None:
    from evalshift.sinks.toolset import toolset_payload

    fingerprint = fingerprint_tools(SAMPLE_TOOLS)
    ToolsetSink(base=tmp_path).write(SAMPLE_TOOLS, fingerprint)
    on_disk = (tmp_path / "toolsets" / f"{fingerprint.removeprefix('sha256:')}.json").read_text(
        encoding="utf-8"
    )
    assert toolset_payload(SAMPLE_TOOLS, fingerprint) == on_disk
    assert json.loads(on_disk) == {
        "schema_version": "1.0.0",
        "fingerprint": fingerprint,
        "tools": SAMPLE_TOOLS,
    }
