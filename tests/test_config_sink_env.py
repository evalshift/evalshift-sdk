"""``EVALSHIFT_CAPTURE_STORE`` (or its 0.5.0 name ``EVALSHIFT_SINK``) selects an ObjectStoreSink
at config construction; an unusable value is recorded there and raised by
``require_sink_ready()`` once capture is on. ``configure(sink=...)`` still beats the env."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from pathlib import Path

import pytest

from evalshift import SinkConfigurationError, capture
from evalshift import config as cfg
from evalshift.config import (
    active_sink,
    configure,
    flush_captures,
    is_capture_enabled,
    require_sink_ready,
    reset_config,
    toolset_writer,
)
from evalshift.sinks.file import FileSink
from evalshift.sinks.hygiene import HygieneSink
from evalshift.sinks.memory import MemorySink
from evalshift.sinks.object_store import ObjectStoreSink
from evalshift.sinks.toolset import ToolsetSink
from evalshift.stores import MemoryStore
from evalshift.stores.uri import MissingStoreDependencyError


def _unwrap(sink: object) -> object:
    return sink.wrapped if isinstance(sink, HygieneSink) else sink


def _recording_open_store(seen: list[str]) -> Callable[[str], MemoryStore]:
    """An ``open_store`` stand-in that records which URI the config asked for."""

    def _open(uri: str) -> MemoryStore:
        seen.append(uri)
        return MemoryStore()

    return _open


def test_env_sink_selects_object_store_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    monkeypatch.setattr(cfg, "open_store", lambda uri: store)
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "s3://acme-evals/prefix")
    reset_config()
    sink = _unwrap(active_sink())
    assert isinstance(sink, ObjectStoreSink)
    assert sink.store is store


def test_legacy_sink_name_still_selects_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # EVALSHIFT_SINK shipped in 0.5.0; deployments that set it keep working, silently.
    seen: list[str] = []
    monkeypatch.setattr(cfg, "open_store", _recording_open_store(seen))
    monkeypatch.setenv("EVALSHIFT_SINK", "s3://legacy-bucket/prefix")
    reset_config()
    assert isinstance(_unwrap(active_sink()), ObjectStoreSink)
    assert seen == ["s3://legacy-bucket/prefix"]


def test_new_name_wins_over_the_legacy_one(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(cfg, "open_store", _recording_open_store(seen))
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "s3://new-bucket/prefix")
    monkeypatch.setenv("EVALSHIFT_SINK", "s3://legacy-bucket/prefix")
    reset_config()
    assert seen == ["s3://new-bucket/prefix"]


def test_blank_new_name_falls_back_to_the_legacy_one(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(cfg, "open_store", _recording_open_store(seen))
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "   ")
    monkeypatch.setenv("EVALSHIFT_SINK", "s3://legacy-bucket/prefix")
    reset_config()
    assert seen == ["s3://legacy-bucket/prefix"]


def test_error_names_the_legacy_variable_the_user_actually_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A user who set EVALSHIFT_SINK must be told to fix EVALSHIFT_SINK, not a name they never used.
    monkeypatch.setenv("EVALSHIFT_SINK", "ftp://bucket/prefix")
    reset_config()
    message = str(_recorded())
    assert message.startswith("EVALSHIFT_SINK is not a valid store URI.")
    assert "EVALSHIFT_CAPTURE_STORE" not in message


def test_env_sink_unset_keeps_filesink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVALSHIFT_CAPTURE_STORE", raising=False)
    reset_config()
    assert isinstance(_unwrap(active_sink()), FileSink)


def test_env_sink_blank_is_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "   ")
    with caplog.at_level(logging.WARNING, logger="evalshift"):
        reset_config()
    assert isinstance(_unwrap(active_sink()), FileSink)
    assert not caplog.records


def _recorded() -> SinkConfigurationError:
    err = cfg._CONFIG.sink_error
    assert isinstance(err, SinkConfigurationError)
    return err


def test_bad_grammar_is_recorded_at_reset_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "ftp://bucket/prefix")
    with caplog.at_level(logging.DEBUG, logger="evalshift"):
        reset_config()  # must not raise: this runs at `import evalshift`
    err = _recorded()
    assert str(err) == (
        "EVALSHIFT_CAPTURE_STORE is not a valid store URI. Accepted forms: s3://<bucket>/<prefix>, "
        "gs://<bucket>/<prefix>, az://<account>/<container>/<prefix>. "
        "Credentials never go in the URI."
    )
    assert not caplog.records
    assert cfg._CONFIG.sink is None


def test_missing_library_is_recorded_naming_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(uri: str) -> object:
        raise MissingStoreDependencyError("s3", "boto3")

    monkeypatch.setattr(cfg, "open_store", _raise)
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "s3://bucket/prefix")
    reset_config()
    assert str(_recorded()) == (
        "EVALSHIFT_CAPTURE_STORE points at an object store (s3://), but boto3 is not installed. "
        "Run: pip install boto3, or unset EVALSHIFT_CAPTURE_STORE to capture to local disk."
    )


def test_missing_azure_library_names_both_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(uri: str) -> object:
        raise MissingStoreDependencyError("az", "azure.identity")

    monkeypatch.setattr(cfg, "open_store", _raise)
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "az://acct/c/p")
    reset_config()
    assert "Run: pip install azure-storage-blob azure-identity," in str(_recorded())


def test_unexpected_open_error_is_recorded_without_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(uri: str) -> object:
        raise RuntimeError(f"boom {uri}")

    monkeypatch.setattr(cfg, "open_store", _raise)
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "s3://bucket/SECRETPREFIX")
    reset_config()
    err = _recorded()
    assert str(err) == (
        "EVALSHIFT_CAPTURE_STORE could not be opened (RuntimeError). "
        "Unset it to capture to local disk."
    )
    assert "SECRETPREFIX" not in "".join(traceback.format_exception(err))
    assert err.__context__ is None and err.__traceback__ is None


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ("az://acct/c?sv=1&sig=SECRETSIG", "SECRETSIG"),
        (
            "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=SECRETKEY==;"
            "EndpointSuffix=core.windows.net",
            "SECRETKEY",
        ),
        ("az://acct#sig=SECRETSIG", "SECRETSIG"),
    ],
)
def test_recorded_error_never_leaks_a_secret_bearing_value(
    monkeypatch: pytest.MonkeyPatch, value: str, secret: str
) -> None:
    # The parser's grammar errors may quote the value they reject; the recorded error must
    # carry a fixed message and no chained cause or context that reaches a traceback.
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", value)
    reset_config()
    err = _recorded()
    assert secret not in "".join(traceback.format_exception(err))
    assert err.__cause__ is None and err.__suppress_context__
    assert err.__context__ is None


def test_require_sink_ready_raises_only_with_the_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "ftp://bucket/prefix")
    monkeypatch.delenv("EVALSHIFT_CAPTURE", raising=False)
    reset_config()
    require_sink_ready()  # gate off: nothing
    monkeypatch.setenv("EVALSHIFT_CAPTURE", "1")
    with pytest.raises(
        SinkConfigurationError, match="EVALSHIFT_CAPTURE_STORE is not a valid store URI"
    ):
        require_sink_ready()
    with pytest.raises(SinkConfigurationError):
        require_sink_ready()  # raising twice must work (a fresh instance is raised each time)


def test_require_sink_ready_does_not_mutate_the_stored_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # require_sink_ready() must raise a fresh SinkConfigurationError each time, never the stored
    # instance itself -- otherwise raising it (especially from inside a caller's except block, as
    # here) leaves the module-global error carrying a traceback (frames can hold a client's API
    # key alive) and a __context__ from whatever the caller was handling.
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "ftp://bucket/prefix")
    monkeypatch.setenv("EVALSHIFT_CAPTURE", "1")
    reset_config()
    try:
        raise ValueError("unrelated to the sink")
    except ValueError:
        with pytest.raises(SinkConfigurationError):
            require_sink_ready()
    assert cfg._CONFIG.sink_error is not None
    assert cfg._CONFIG.sink_error.__traceback__ is None
    assert cfg._CONFIG.sink_error.__context__ is None


def test_configure_with_a_sink_clears_the_error_and_without_one_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "ftp://bucket/prefix")
    monkeypatch.setenv("EVALSHIFT_CAPTURE", "1")
    reset_config()
    with pytest.raises(SinkConfigurationError):
        configure(dedup=False)
    assert cfg._CONFIG.dedup is True  # the raise comes first; nothing was merged
    memory = MemorySink()
    configure(sink=memory)
    assert cfg._CONFIG.sink_error is None
    require_sink_ready()
    assert is_capture_enabled() is True
    assert _unwrap(active_sink()) is memory


def test_is_capture_enabled_is_false_while_the_sink_is_blocked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "ftp://bucket/prefix")
    monkeypatch.setenv("EVALSHIFT_CAPTURE", "1")
    reset_config()
    with caplog.at_level(logging.WARNING, logger="evalshift"):
        assert is_capture_enabled() is False
        assert is_capture_enabled() is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert (
        warnings[0].getMessage().startswith("evalshift: capture disabled: EVALSHIFT_CAPTURE_STORE")
    )


def test_gate_turned_on_late_drops_captures_instead_of_writing_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EVALSHIFT_DIR", str(tmp_path))
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "ftp://bucket/prefix")
    monkeypatch.delenv("EVALSHIFT_CAPTURE", raising=False)
    reset_config()

    @capture.agent(suite="s", redact=False, tools=[])  # gate off: decorating must not raise
    def run(q: str) -> str:
        return q.upper()

    monkeypatch.setenv("EVALSHIFT_CAPTURE", "1")
    with caplog.at_level(logging.WARNING, logger="evalshift"):
        assert run("hi") == "HI"
        assert run("again") == "AGAIN"
    assert not list(tmp_path.rglob("*.json"))
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_configure_sink_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "open_store", lambda uri: MemoryStore())
    monkeypatch.setenv("EVALSHIFT_CAPTURE_STORE", "s3://bucket/prefix")
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
