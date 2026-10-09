"""ObjectStoreSink in its default background mode: ordering, bounded queue, flush, atexit."""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest

from evalshift.capture.span import SpanTree
from evalshift.capture.toolset import fingerprint_tools
from evalshift.sinks.object_store import ObjectStoreSink
from evalshift.stores import MemoryStore
from evalshift.trace.models import CaptureEnvelope
from evalshift.trace.serialize import build_capture

TOOLS: list[dict[str, Any]] = [
    {"name": "search", "description": "Search.", "input_schema": {"type": "object"}}
]


def _envelope(capture_id: str) -> CaptureEnvelope:
    return build_capture(SpanTree(), suite="s", agent_input="hi", capture_id=capture_id)


class GatedStore(MemoryStore):
    """A store whose puts block until the test opens the gate; records put order."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.order: list[str] = []

    def put(self, key: str, data: bytes) -> None:
        self.gate.wait(timeout=5)
        self.order.append(key)
        super().put(key, data)


def test_write_returns_before_put_completes_and_flush_drains_queue() -> None:
    store = GatedStore()
    sink = ObjectStoreSink(store)
    assert sink.write(_envelope("cap_1")) is None
    assert store.objects == {}  # not yet put: the gate is closed
    store.gate.set()
    assert sink.flush(timeout=5) is True
    assert set(store.objects) == {"captures/s/cap_1.json"}


def test_flush_times_out_while_store_is_blocked() -> None:
    store = GatedStore()
    sink = ObjectStoreSink(store)
    sink.write(_envelope("cap_1"))
    assert sink.flush(timeout=0.05) is False
    store.gate.set()
    assert sink.flush(timeout=5) is True


def test_toolset_is_put_before_the_capture_that_references_it() -> None:
    store = GatedStore()
    sink = ObjectStoreSink(store)
    fingerprint = fingerprint_tools(TOOLS)
    sink.write_toolset(TOOLS, fingerprint)
    sink.write(_envelope("cap_1"))
    store.gate.set()
    assert sink.flush(timeout=5)
    assert store.order == [
        f"toolsets/{fingerprint.removeprefix('sha256:')}.json",
        "captures/s/cap_1.json",
    ]


def test_full_queue_drops_newest_without_raising(caplog: pytest.LogCaptureFixture) -> None:
    store = GatedStore()
    # queue_size=1: the worker takes the first item and blocks, the second waits in the queue,
    # the third finds the queue full and is dropped.
    sink = ObjectStoreSink(store, queue_size=1)
    with caplog.at_level(logging.DEBUG, logger="evalshift"):
        sink.write(_envelope("cap_1"))
        # Give the worker a moment to take cap_1 off the queue.
        for _ in range(100):
            if sink._queue.empty():
                break
            threading.Event().wait(0.01)
        sink.write(_envelope("cap_2"))
        assert sink.write(_envelope("cap_3")) is None
    store.gate.set()
    assert sink.flush(timeout=5)
    assert set(store.objects) == {"captures/s/cap_1.json", "captures/s/cap_2.json"}
    assert any("queue full" in r.message for r in caplog.records)


def test_write_toolset_returns_none_when_dropped() -> None:
    store = GatedStore()
    sink = ObjectStoreSink(store, queue_size=1)
    sink.write(_envelope("cap_1"))
    for _ in range(100):
        if sink._queue.empty():
            break
        threading.Event().wait(0.01)
    sink.write(_envelope("cap_2"))
    assert sink.write_toolset(TOOLS, fingerprint_tools(TOOLS)) is None
    store.gate.set()
    sink.flush(timeout=5)


def test_atexit_registered_once_on_first_background_write(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[tuple[Any, ...]] = []
    # String target: the sink module's ``atexit`` is not an explicit export under mypy --strict.
    monkeypatch.setattr(
        "evalshift.sinks.object_store.atexit.register",
        lambda fn, *a: registered.append((fn, *a)),
    )
    store = MemoryStore()
    sink = ObjectStoreSink(store, flush_timeout=3.0)
    sink.write(_envelope("cap_1"))
    sink.write(_envelope("cap_2"))
    assert sink.flush(timeout=5)
    assert registered == [(sink.flush, 3.0)]


def test_flush_is_immediately_true_when_nothing_was_written() -> None:
    assert ObjectStoreSink(MemoryStore()).flush(timeout=0) is True


def test_worker_survives_a_raising_put_and_keeps_draining() -> None:
    class FlakyStore(MemoryStore):
        uri = "memory://flaky"

        def put(self, key: str, data: bytes) -> None:
            if key.endswith("cap_1.json"):
                raise RuntimeError("transient")
            super().put(key, data)

    store = FlakyStore()
    sink = ObjectStoreSink(store)
    sink.write(_envelope("cap_1"))
    sink.write(_envelope("cap_2"))
    assert sink.flush(timeout=5)
    assert set(store.objects) == {"captures/s/cap_2.json"}
