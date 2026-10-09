"""ObjectStoreSink: key layout, fail-open puts and toolset routing (inline mode).

Background-queue behaviour is in ``tests/test_object_store_sink_background.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from evalshift.capture.span import SpanTree
from evalshift.capture.toolset import fingerprint_tools
from evalshift.sinks.base import Sink
from evalshift.sinks.object_store import ObjectStoreSink
from evalshift.stores import MemoryStore
from evalshift.trace.models import CaptureEnvelope
from evalshift.trace.serialize import build_capture, dumps

TOOLS: list[dict[str, Any]] = [
    {"name": "search", "description": "Search.", "input_schema": {"type": "object"}}
]


def _envelope(suite: str = "support_agent", capture_id: str = "cap_abc") -> CaptureEnvelope:
    return build_capture(SpanTree(), suite=suite, agent_input="hi", capture_id=capture_id)


class RaisingStore:
    uri = "s3://broken/prefix"

    def put(self, key: str, data: bytes) -> None:
        raise PermissionError("AccessDenied")


def test_satisfies_sink_protocol() -> None:
    assert isinstance(ObjectStoreSink(MemoryStore(), background=False), Sink)


def test_write_uses_local_layout_and_returns_none() -> None:
    store = MemoryStore()
    env = _envelope()
    assert ObjectStoreSink(store, background=False).write(env) is None
    assert store.objects == {"captures/support_agent/cap_abc.json": dumps(env).encode("utf-8")}


def test_suite_segment_is_sanitised_in_key() -> None:
    store = MemoryStore()
    ObjectStoreSink(store, background=False).write(_envelope(suite="../../etc"))
    [key] = store.objects
    assert key.startswith("captures/")
    assert ".." not in key
    assert key.count("/") == 2  # captures/<one segment>/<file>


def test_write_toolset_writes_sidecar_payload_and_returns_ref() -> None:
    store = MemoryStore()
    sink = ObjectStoreSink(store, background=False)
    fingerprint = fingerprint_tools(TOOLS)
    assert sink.write_toolset(TOOLS, fingerprint) == fingerprint
    key = f"toolsets/{fingerprint.removeprefix('sha256:')}.json"
    assert json.loads(store.objects[key]) == {
        "schema_version": "1.0.0",
        "fingerprint": fingerprint,
        "tools": TOOLS,
    }


def test_write_toolset_is_idempotent_per_process() -> None:
    class CountingStore(MemoryStore):
        puts = 0

        def put(self, key: str, data: bytes) -> None:
            self.puts += 1
            super().put(key, data)

    store = CountingStore()
    sink = ObjectStoreSink(store, background=False)
    fingerprint = fingerprint_tools(TOOLS)
    assert sink.write_toolset(TOOLS, fingerprint) == fingerprint
    assert sink.write_toolset(TOOLS, fingerprint) == fingerprint
    assert store.puts == 1


def test_put_failure_never_raises_and_returns_none() -> None:
    sink = ObjectStoreSink(RaisingStore(), background=False)
    assert sink.write(_envelope()) is None
    assert sink.write_toolset(TOOLS, fingerprint_tools(TOOLS)) is None


def test_put_failure_first_warning_then_debug(caplog: pytest.LogCaptureFixture) -> None:
    sink = ObjectStoreSink(RaisingStore(), background=False)
    with caplog.at_level(logging.DEBUG, logger="evalshift"):
        sink.write(_envelope(capture_id="cap_1"))
        sink.write(_envelope(capture_id="cap_2"))
    puts = [r for r in caplog.records if "object store put" in r.message]
    assert [r.levelno for r in puts] == [logging.WARNING, logging.DEBUG]
    assert "s3://broken/prefix" in puts[0].message
    assert "PermissionError" in puts[0].message


def test_put_failure_names_what_was_dropped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="evalshift"):
        ObjectStoreSink(RaisingStore(), background=False).write(_envelope())
        ObjectStoreSink(RaisingStore(), background=False).write_toolset(
            TOOLS, fingerprint_tools(TOOLS)
        )
    capture_msg, sidecar_msg = [
        r.message for r in caplog.records if "object store put" in r.message
    ]
    assert "(capture dropped)" in capture_msg
    assert "(toolset sidecar dropped)" in sidecar_msg


def test_store_property_exposes_the_store() -> None:
    store = MemoryStore()
    assert ObjectStoreSink(store, background=False).store is store


def test_failed_background_toolset_put_releases_fingerprint_for_retry() -> None:
    class FlakyToolsetStore(MemoryStore):
        failed = False

        def put(self, key: str, data: bytes) -> None:
            if key.startswith("toolsets/") and not self.failed:
                self.failed = True
                raise ConnectionError("outage")
            super().put(key, data)

    store = FlakyToolsetStore()
    sink = ObjectStoreSink(store, background=True)
    fingerprint = fingerprint_tools(TOOLS)
    key = f"toolsets/{fingerprint.removeprefix('sha256:')}.json"

    assert sink.write_toolset(TOOLS, fingerprint) == fingerprint
    assert sink.flush(timeout=5.0)
    assert store.failed
    assert key not in store.objects

    assert sink.write_toolset(TOOLS, fingerprint) == fingerprint
    assert sink.flush(timeout=5.0)
    assert key in store.objects
