"""``EVALSHIFT_SINK`` selects an ObjectStoreSink at config construction; invalid values warn
once and fall back to local disk. ``configure(sink=...)`` still beats the env."""

from __future__ import annotations

import logging

import pytest

from evalshift import config as cfg
from evalshift.config import active_sink, configure, flush_captures, reset_config, toolset_writer
from evalshift.sinks.file import FileSink
from evalshift.sinks.hygiene import HygieneSink
from evalshift.sinks.memory import MemorySink
from evalshift.sinks.object_store import ObjectStoreSink
from evalshift.sinks.toolset import ToolsetSink
from evalshift.stores import MemoryStore
from evalshift.stores.uri import MissingExtraError, parse_store_uri


def _unwrap(sink: object) -> object:
    return sink.wrapped if isinstance(sink, HygieneSink) else sink


def test_env_sink_selects_object_store_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    monkeypatch.setattr(cfg, "open_store", lambda uri: store)
    monkeypatch.setenv("EVALSHIFT_SINK", "s3://acme-evals/prefix")
    reset_config()
    sink = _unwrap(active_sink())
    assert isinstance(sink, ObjectStoreSink)
    assert sink.store is store


def test_env_sink_unset_keeps_filesink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVALSHIFT_SINK", raising=False)
    reset_config()
    assert isinstance(_unwrap(active_sink()), FileSink)


def test_env_sink_blank_is_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EVALSHIFT_SINK", "   ")
    with caplog.at_level(logging.WARNING, logger="evalshift"):
        reset_config()
    assert isinstance(_unwrap(active_sink()), FileSink)
    assert not caplog.records


def test_env_sink_bad_grammar_warns_once_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EVALSHIFT_SINK", "ftp://bucket/prefix")
    with caplog.at_level(logging.WARNING, logger="evalshift"):
        reset_config()
        active_sink()
        active_sink()
    assert isinstance(_unwrap(active_sink()), FileSink)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "EVALSHIFT_SINK" in warnings[0].message
    assert "accepted forms" in warnings[0].message
    assert "local disk" in warnings[0].message


def test_env_sink_missing_extra_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise(uri: str) -> object:
        raise MissingExtraError(parse_store_uri(uri), "boto3")

    monkeypatch.setattr(cfg, "open_store", _raise)
    monkeypatch.setenv("EVALSHIFT_SINK", "s3://bucket/prefix")
    with caplog.at_level(logging.WARNING, logger="evalshift"):
        reset_config()
    assert isinstance(_unwrap(active_sink()), FileSink)
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert 'pip install "evalshift-sdk[s3]"' in warning.message


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        # The parser refuses `?` without echoing the URI...
        ("az://acct/c?sv=1&sig=SECRETSIG", "SECRETSIG"),
        # ...but its scheme / bucket / container errors quote the value they reject.
        (
            "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=SECRETKEY==;"
            "EndpointSuffix=core.windows.net",
            "SECRETKEY",
        ),
        ("az://acct#sig=SECRETSIG", "SECRETSIG"),
    ],
)
def test_env_sink_warning_does_not_leak_secret_bearing_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str, secret: str
) -> None:
    # A secret in the env value must never reach the logs: the warning names the env var, the
    # accepted forms and the fallback -- never the raw value, nor an error message quoting it.
    monkeypatch.setenv("EVALSHIFT_SINK", value)
    with caplog.at_level(logging.DEBUG, logger="evalshift"):
        reset_config()
    assert isinstance(_unwrap(active_sink()), FileSink)
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "EVALSHIFT_SINK" in warning.message
    assert "local disk" in warning.message
    assert all(secret not in r.getMessage() for r in caplog.records)


def test_configure_sink_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "open_store", lambda uri: MemoryStore())
    monkeypatch.setenv("EVALSHIFT_SINK", "s3://bucket/prefix")
    reset_config()
    memory = MemorySink()
    configure(sink=memory)
    assert _unwrap(active_sink()) is memory


def test_toolset_writer_routes_to_object_store_sink() -> None:
    sink = ObjectStoreSink(MemoryStore(), background=False)
    configure(sink=sink)
    writer = toolset_writer()
    assert writer == sink.write_toolset


def test_toolset_writer_defaults_to_file_sidecar() -> None:
    configure(sink=MemorySink())
    writer = toolset_writer()
    assert getattr(writer, "__self__", None).__class__ is ToolsetSink


def test_flush_captures_is_true_for_filesink() -> None:
    assert flush_captures(timeout=0) is True


def test_flush_captures_drains_object_store_sink() -> None:
    store = MemoryStore()
    sink = ObjectStoreSink(store)
    configure(sink=sink)
    from evalshift.capture.span import SpanTree
    from evalshift.trace.serialize import build_capture

    sink.write(build_capture(SpanTree(), suite="s", agent_input="hi", capture_id="cap_1"))
    assert flush_captures(timeout=5) is True
    assert "captures/s/cap_1.json" in store.objects


def test_flush_captures_does_not_drain_memory_sink() -> None:
    # MemorySink.flush() drains the buffer; flush_captures must never call it.
    memory = MemorySink()
    configure(sink=memory)
    from evalshift.capture.span import SpanTree
    from evalshift.trace.serialize import build_capture

    memory.write(build_capture(SpanTree(), suite="s", agent_input="hi", capture_id="cap_1"))
    assert flush_captures() is True
    assert len(memory.captures) == 1
