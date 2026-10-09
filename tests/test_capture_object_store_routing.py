"""End to end through the public API: with an ObjectStoreSink configured, the capture and its
toolset sidecar both land in the store and nothing is written to disk; with MemorySink the
sidecar still goes to disk (unchanged behaviour)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evalshift import MemorySink, ObjectStoreSink, capture, configure, record_model_call
from evalshift.capture.toolset import fingerprint_tools, normalize_tools
from evalshift.stores import MemoryStore

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_orders",
        "description": "Search orders.",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
]


def test_capture_and_sidecar_go_to_the_store_not_disk(capturing: Path) -> None:
    store = MemoryStore()
    configure(sink=ObjectStoreSink(store, background=False))

    @capture.agent(suite="s", redact=False, tools=[])
    def agent() -> str:
        record_model_call(model_id="m", tools=TOOLS, input="hi", output="ok")
        return "ok"

    agent()

    normalized = normalize_tools(TOOLS)
    assert normalized is not None
    fingerprint = fingerprint_tools(normalized)
    keys = sorted(store.objects)
    assert keys[1] == f"toolsets/{fingerprint.removeprefix('sha256:')}.json"
    assert keys[0].startswith("captures/s/cap_")
    envelope = json.loads(store.objects[keys[0]])
    [mc] = [e for e in envelope["trace"]["events"] if e["type"] == "model_call"]
    assert mc["toolset_ref"] == fingerprint
    assert not (capturing / "captures").exists()
    assert not (capturing / "toolsets").exists()


def test_memory_sink_still_writes_file_sidecar(capturing: Path) -> None:
    configure(sink=MemorySink())

    @capture.agent(suite="s", redact=False, tools=[])
    def agent() -> str:
        record_model_call(model_id="m", tools=TOOLS, input="hi", output="ok")
        return "ok"

    agent()
    normalized = normalize_tools(TOOLS)
    assert normalized is not None
    fingerprint = fingerprint_tools(normalized)
    assert (capturing / "toolsets" / f"{fingerprint.removeprefix('sha256:')}.json").exists()


def test_toolset_write_fault_keeps_the_model_call_event(
    capturing: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fault inside ObjectStoreSink.write_toolset (e.g. the worker thread failing to start)
    # must leave toolset_ref unstamped, not take the whole model_call event down with it.
    store = MemoryStore()
    sink = ObjectStoreSink(store, background=False)

    def _boom(normalized: list[dict[str, Any]], fingerprint: str) -> str | None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(sink, "write_toolset", _boom)
    configure(sink=sink)

    @capture.agent(suite="s", redact=False, tools=[])
    def agent() -> str:
        record_model_call(model_id="m", tools=TOOLS, input="hi", output="ok")
        return "ok"

    assert agent() == "ok"

    [key] = [k for k in store.objects if k.startswith("captures/")]
    envelope = json.loads(store.objects[key])
    [mc] = [e for e in envelope["trace"]["events"] if e["type"] == "model_call"]
    assert mc["model_id"] == "m"
    assert mc["tools_offered"] == ["search_orders"]
    assert mc.get("toolset_ref") is None
    assert not any(k.startswith("toolsets/") for k in store.objects)
