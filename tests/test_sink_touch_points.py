"""Every explicit touch point raises the recorded ``EVALSHIFT_SINK`` error while capture is on."""

from __future__ import annotations

import asyncio
import importlib
import logging

import pytest

from evalshift import SinkConfigurationError, capture, configure
from evalshift.config import is_capture_enabled, require_sink_ready, reset_config


@pytest.fixture
def broken_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALSHIFT_SINK", "ftp://bucket/prefix")
    monkeypatch.setenv("EVALSHIFT_CAPTURE", "1")
    reset_config()  # records, never raises


def test_agent_decorator_raises_at_decoration(broken_sink: None) -> None:
    with pytest.raises(SinkConfigurationError, match="EVALSHIFT_SINK is not a valid store URI"):
        capture.agent(suite="s", redact=False, tools=[])


def test_tool_decorator_raises_at_decoration(broken_sink: None) -> None:
    with pytest.raises(SinkConfigurationError):

        @capture.tool
        def lookup() -> None: ...

    with pytest.raises(SinkConfigurationError):
        capture.tool(name="lookup")


def test_agent_session_raises_on_entry(broken_sink: None) -> None:
    with (
        pytest.raises(SinkConfigurationError),
        capture.agent_session(suite="s", redact=False, tools=[]),
    ):
        pass


def test_agent_session_async_raises_on_entry(broken_sink: None) -> None:
    async def run() -> None:
        async with capture.agent_session_async(suite="s", redact=False, tools=[]):
            pass

    with pytest.raises(SinkConfigurationError):
        asyncio.run(run())


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("evalshift.adapters.openai", "wrap_openai"),
        ("evalshift.adapters.anthropic", "wrap_anthropic"),
        ("evalshift.adapters.genai", "wrap_genai"),
    ],
)
def test_client_wrappers_raise_at_wrap_time(broken_sink: None, module: str, name: str) -> None:
    wrap = getattr(importlib.import_module(module), name)
    with pytest.raises(SinkConfigurationError):
        wrap(object())


def test_langchain_handler_raises_at_construction(broken_sink: None) -> None:
    pytest.importorskip("langchain_core")
    from evalshift.adapters.langchain import EvalShiftCallbackHandler

    with pytest.raises(SinkConfigurationError):
        EvalShiftCallbackHandler(suite="s", redact=False, tools=[])


def test_gate_off_never_raises_or_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EVALSHIFT_SINK", "ftp://bucket/prefix")
    monkeypatch.delenv("EVALSHIFT_CAPTURE", raising=False)
    reset_config()
    with caplog.at_level(logging.DEBUG, logger="evalshift"):
        require_sink_ready()
        capture.agent(suite="s", redact=False, tools=[])
        capture.tool(name="x")
        with capture.agent_session(suite="s", redact=False, tools=[]) as tree:
            assert tree is None
        from evalshift.adapters.openai import wrap_openai

        wrap_openai(object())
        configure(dedup=False)
        assert is_capture_enabled() is False
    assert not caplog.records
