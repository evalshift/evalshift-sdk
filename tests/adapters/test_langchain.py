"""Phase 7 — the LangChain adapter callback handler.

Most tests drive ``EvalShiftCallbackHandler`` by calling its callback methods directly with the
kwargs LangChain passes (synthetic, deterministic, no langchain needed at run time). The final
test runs a real ``langchain_core`` chain through the handler (guarded by ``importorskip``) to
prove the real callback signatures match.

The written capture is validated through the vendored pydantic ``AgentTrace`` — the CLI contract —
exactly as the manual-instrumentation conformance tests do.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from evalshift import configure
from evalshift.adapters.langchain import EvalShiftCallbackHandler
from tests.conformance.cli_models_vendored import AgentTrace
from tests.conftest import CaptureReader


def _llm_result(text: str, *, input_tokens: int = 0, output_tokens: int = 0) -> SimpleNamespace:
    """A duck-typed ``LLMResult`` carrying one generation + token usage (no langchain import)."""
    message = SimpleNamespace(
        usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens}
    )
    generation = SimpleNamespace(text=text, message=message)
    return SimpleNamespace(generations=[[generation]], llm_output=None)


def _chat_llm_result(text: str, tool_calls: Any, **extra: Any) -> SimpleNamespace:
    """A duck-typed chat ``LLMResult`` whose ``AIMessage`` carries ``tool_calls``.

    LangChain normalises every provider into ``{"name", "args", "id", "type"}`` items, so the
    handler only has to rename them -- these fixtures use that exact shape.
    """
    message = SimpleNamespace(usage_metadata=None, tool_calls=tool_calls, **extra)
    generation = SimpleNamespace(text=text, message=message)
    return SimpleNamespace(generations=[[generation]], llm_output=None)


def _text_llm_result(text: str) -> SimpleNamespace:
    """A plain (non-chat) ``LLMResult``: a ``Generation`` carries no ``.message`` at all."""
    return SimpleNamespace(generations=[[SimpleNamespace(text=text)]], llm_output=None)


def _events(cap: dict[str, Any]) -> list[dict[str, Any]]:
    return list(cap["trace"]["events"])


def _of_type(cap: dict[str, Any], type_: str) -> list[dict[str, Any]]:
    return [e for e in _events(cap) if e["type"] == type_]


def test_chain_with_tool_and_model_call_is_schema_valid(
    capturing: Path, read_captures: CaptureReader
) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_agent", redact=False, tools=[])
    root, tool, mc = uuid4(), uuid4(), uuid4()

    handler.on_chain_start({"name": "chain"}, {"query": "hi"}, run_id=root, parent_run_id=None)
    handler.on_tool_start(
        {"name": "search"}, "hi", run_id=tool, parent_run_id=root, inputs={"q": "hi"}
    )
    handler.on_tool_end({"hits": 1}, run_id=tool, parent_run_id=root)
    handler.on_llm_start({"name": "llm"}, ["answer this"], run_id=mc, parent_run_id=root)
    handler.on_llm_end(
        _llm_result("answer", input_tokens=12, output_tokens=3), run_id=mc, parent_run_id=root
    )
    handler.on_chain_end({"output": "answer"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_agent")[0]
    AgentTrace.model_validate(cap["trace"])  # raises if not CLI-valid
    assert len(_of_type(cap, "tool_call")) == 1
    assert len(_of_type(cap, "tool_result")) == 1
    assert len(_of_type(cap, "model_call")) == 1
    assert _of_type(cap, "tool_call")[0]["name"] == "search"
    assert _of_type(cap, "model_call")[0]["model_id"] == "llm"
    assert _of_type(cap, "model_call")[0]["input_tokens"] == 12
    assert cap["suite"] == "lc_agent"


def test_nested_tool_parentage(capturing: Path, read_captures: CaptureReader) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_nest", redact=False, tools=[])
    root, tool_a, mc, tool_b = uuid4(), uuid4(), uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_tool_start({"name": "tool_a"}, "x", run_id=tool_a, parent_run_id=root)
    # model call nested *inside* tool_a
    handler.on_llm_start({"name": "llm"}, ["p"], run_id=mc, parent_run_id=tool_a)
    handler.on_llm_end(_llm_result("o"), run_id=mc, parent_run_id=tool_a)
    handler.on_tool_end("done", run_id=tool_a, parent_run_id=root)
    # sibling tool_b at top level
    handler.on_tool_start({"name": "tool_b"}, "y", run_id=tool_b, parent_run_id=root)
    handler.on_tool_end("done", run_id=tool_b, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_nest")[0]
    AgentTrace.model_validate(cap["trace"])
    call_a = next(e for e in _of_type(cap, "tool_call") if e["name"] == "tool_a")
    call_b = next(e for e in _of_type(cap, "tool_call") if e["name"] == "tool_b")
    model = _of_type(cap, "model_call")[0]
    # model call's parent is tool_a's call_id (carried in the evalshift meta block)
    assert model["metadata"]["evalshift"]["parent_call_id"] == call_a["call_id"]
    # sibling tool at top level has no tool parent
    assert call_b["parent_call_id"] is None


def test_tool_error_is_captured(capturing: Path, read_captures: CaptureReader) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_err", redact=False, tools=[])
    root, tool = uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_tool_start({"name": "boom"}, "x", run_id=tool, parent_run_id=root)
    handler.on_tool_error(ValueError("kaboom"), run_id=tool, parent_run_id=root)
    handler.on_chain_end({"output": ""}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_err")[0]
    AgentTrace.model_validate(cap["trace"])
    result = _of_type(cap, "tool_result")[0]
    assert result["error"] is not None and "kaboom" in result["error"]


def test_chain_error_records_error_event(capturing: Path, read_captures: CaptureReader) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_chain_err", redact=False, tools=[])
    root = uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_chain_error(RuntimeError("chain blew up"), run_id=root, parent_run_id=None)

    cap = read_captures("lc_chain_err")[0]
    trace = AgentTrace.model_validate(cap["trace"])
    assert any(e.type == "error" for e in trace.events)


def test_gate_off_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_captures: CaptureReader
) -> None:
    # EVALSHIFT_DIR set but EVALSHIFT_CAPTURE unset -> gate off, no files.
    monkeypatch.setenv("EVALSHIFT_DIR", str(tmp_path))
    monkeypatch.delenv("EVALSHIFT_CAPTURE", raising=False)
    handler = EvalShiftCallbackHandler(suite="lc_off", redact=False, tools=[])
    root, tool = uuid4(), uuid4()
    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_tool_start({"name": "t"}, "x", run_id=tool, parent_run_id=root)
    handler.on_tool_end("ok", run_id=tool, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)
    assert read_captures("lc_off") == []


def test_sampling_zero_writes_nothing(capturing: Path, read_captures: CaptureReader) -> None:
    configure(sample_rate=0.0)
    handler = EvalShiftCallbackHandler(suite="lc_sample", redact=False, tools=[])
    root = uuid4()
    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)
    assert read_captures("lc_sample") == []


def test_sampling_full_writes_one(capturing: Path, read_captures: CaptureReader) -> None:
    configure(sample_rate=1.0)
    handler = EvalShiftCallbackHandler(suite="lc_sample", redact=False, tools=[])
    root = uuid4()
    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)
    assert len(read_captures("lc_sample")) == 1


def test_redactor_fault_drops_capture_without_raising(
    capturing: Path, read_captures: CaptureReader
) -> None:
    def _boom(value: Any) -> Any:
        raise RuntimeError("redactor down")

    handler = EvalShiftCallbackHandler(suite="lc_redact", redact=_boom, tools=[])
    root = uuid4()
    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    # The terminal callback must not raise even though redaction fails inside finalize.
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)
    assert read_captures("lc_redact") == []  # fail-closed: dropped, not written unredacted


def test_malformed_callback_never_raises(capturing: Path) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_safe", redact=False, tools=[])
    root, tool = uuid4(), uuid4()
    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    # serialized=None would blow up an unguarded .get(...); must be swallowed.
    handler.on_tool_start(None, "x", run_id=tool, parent_run_id=root)
    handler.on_tool_end("ok", run_id=tool, parent_run_id=root)
    # an end for an unknown run id must be a no-op, not a KeyError
    handler.on_tool_end("ok", run_id=uuid4(), parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)


def test_provenance_metadata(capturing: Path, read_captures: CaptureReader) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_prov", redact=False, tools=[])
    root, tool = uuid4(), uuid4()
    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_tool_start({"name": "t"}, "x", run_id=tool, parent_run_id=root)
    handler.on_tool_end("ok", run_id=tool, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_prov")[0]
    call = _of_type(cap, "tool_call")[0]
    assert call["metadata"]["adapter"] == "langchain"
    assert call["metadata"]["lc_run_id"] == str(tool)
    # the serializer's own namespace must survive alongside the provenance keys
    assert "evalshift" in call["metadata"]


def test_handler_reuse_across_invocations(capturing: Path, read_captures: CaptureReader) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_reuse", redact=False, tools=[])
    for q in ("a", "b"):
        root = uuid4()
        handler.on_chain_start({"name": "c"}, {"q": q}, run_id=root, parent_run_id=None)
        handler.on_chain_end({"output": q}, run_id=root, parent_run_id=None)
    caps = read_captures("lc_reuse")
    assert len(caps) == 2
    ids = {c["capture_id"] for c in caps}
    assert len(ids) == 2  # distinct captures, no state bleed


def test_real_langchain_chain_smoke(capturing: Path, read_captures: CaptureReader) -> None:
    pytest.importorskip("langchain_core")
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import tool

    @tool
    def shout(text: str) -> str:
        """Uppercase the text."""
        return text.upper()

    def run(inputs: dict[str, Any]) -> str:
        return str(shout.invoke(inputs["text"]))

    chain = RunnableLambda(run)
    handler = EvalShiftCallbackHandler(suite="lc_smoke", redact=False, tools=[])
    result = chain.invoke({"text": "hi"}, config={"callbacks": [handler]})
    assert result == "HI"

    caps = read_captures("lc_smoke")
    assert len(caps) == 1
    AgentTrace.model_validate(caps[0]["trace"])
    assert any(e["name"] == "shout" for e in _of_type(caps[0], "tool_call"))


def test_handler_requires_redact() -> None:
    """``redact=`` is required on the handler exactly as on ``capture.agent`` (D-4c)."""
    with pytest.raises(TypeError):
        EvalShiftCallbackHandler(suite="lc_req")  # type: ignore[call-arg]


def test_handler_rejects_none_redact() -> None:
    with pytest.raises(TypeError):
        EvalShiftCallbackHandler(suite="lc_req", redact=None, tools=[])  # type: ignore[arg-type]


def test_handler_requires_tools() -> None:
    """``tools=`` is required on the handler exactly as on the other five capture points
    (D-toolset) -- the handler is the "session" here: it has no per-call override surface
    (LangChain callbacks carry no user-supplied tools kwarg), so its one constructor-time value
    is what every model_call span this handler ever opens gets stamped with."""
    with pytest.raises(TypeError):
        EvalShiftCallbackHandler(suite="lc_req", redact=False)  # type: ignore[call-arg]


def test_handler_stamps_tools_offered_and_toolset_ref_on_model_call(
    capturing: Path, read_captures: CaptureReader
) -> None:
    tool = {
        "name": "search_orders",
        "description": "Look up a customer's orders.",
        "input_schema": {"type": "object"},
    }
    handler = EvalShiftCallbackHandler(suite="lc_tools", redact=False, tools=[tool])
    root, mc = uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_llm_start({"name": "llm"}, ["p"], run_id=mc, parent_run_id=root)
    handler.on_llm_end(_llm_result("o"), run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_tools")[0]
    AgentTrace.model_validate(cap["trace"])
    model = _of_type(cap, "model_call")[0]
    assert model["tools_offered"] == ["search_orders"]
    assert model["toolset_ref"] is not None


def test_handler_stamps_tools_on_chat_model_start_too(
    capturing: Path, read_captures: CaptureReader
) -> None:
    tool = {"name": "search", "description": "", "input_schema": {}}
    handler = EvalShiftCallbackHandler(suite="lc_tools_chat", redact=False, tools=[tool])
    root, mc = uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_chat_model_start({"name": "llm"}, [], run_id=mc, parent_run_id=root)
    handler.on_llm_end(_llm_result("o"), run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_tools_chat")[0]
    model = _of_type(cap, "model_call")[0]
    assert model["tools_offered"] == ["search"]


def test_handler_empty_tools_is_a_real_stamped_value(
    capturing: Path, read_captures: CaptureReader
) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_tools_empty", redact=False, tools=[])
    root, mc = uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_llm_start({"name": "llm"}, ["p"], run_id=mc, parent_run_id=root)
    handler.on_llm_end(_llm_result("o"), run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_tools_empty")[0]
    model = _of_type(cap, "model_call")[0]
    assert model["tools_offered"] == []
    assert model["toolset_ref"] is not None


def test_handler_unrecognised_tools_leaves_both_fields_unstamped(
    capturing: Path, read_captures: CaptureReader
) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_tools_bad", redact=False, tools="not-a-toolset")
    root, mc = uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_llm_start({"name": "llm"}, ["p"], run_id=mc, parent_run_id=root)
    handler.on_llm_end(_llm_result("o"), run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_tools_bad")[0]
    model = _of_type(cap, "model_call")[0]
    assert model["tools_offered"] is None
    assert model["toolset_ref"] is None


def test_handler_true_masks(capturing: Path, read_captures: CaptureReader) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_req", redact=True, tools=[])
    root = uuid4()
    handler.on_chain_start({}, {"q": "mail bob@corp.com"}, run_id=root, parent_run_id=None)
    handler.on_llm_start({}, ["mail bob@corp.com"], run_id=uuid4(), parent_run_id=root)
    handler.on_chain_end({"out": "ok"}, run_id=root, parent_run_id=None)

    caps = read_captures("lc_req")
    assert len(caps) == 1
    assert "bob@corp.com" not in str(caps[0]["trace"]["events"])


def test_chat_model_start_extracts_generation_config(
    capturing: Path, read_captures: CaptureReader
) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_gen", redact=False, tools=[])
    root, mc = uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_llm_start(
        {"name": "llm"},
        ["p"],
        run_id=mc,
        parent_run_id=root,
        invocation_params={
            "temperature": 0.2,
            "response_mime_type": "application/json",
            "response_schema": {"type": "object"},
            "model": "gemini-x",
            "streaming": False,
        },
    )
    handler.on_llm_end(_llm_result("o"), run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_gen")[0]
    AgentTrace.model_validate(cap["trace"])
    model = _of_type(cap, "model_call")[0]
    assert model["metadata"]["generation_config"] == {
        "temperature": 0.2,
        "response_mime_type": "application/json",
        "response_schema": {"type": "object"},
    }


def test_llm_start_without_generation_params_emits_no_key(
    capturing: Path, read_captures: CaptureReader
) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_gen_none", redact=False, tools=[])
    root, mc = uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_llm_start(
        {"name": "llm"}, ["p"], run_id=mc, parent_run_id=root, invocation_params={"model": "gpt-x"}
    )
    handler.on_llm_end(_llm_result("o"), run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_gen_none")[0]
    model = _of_type(cap, "model_call")[0]
    assert "generation_config" not in model["metadata"]


def test_nested_generation_config_dict_is_merged(
    capturing: Path, read_captures: CaptureReader
) -> None:
    handler = EvalShiftCallbackHandler(suite="lc_gen_nested", redact=False, tools=[])
    root, mc = uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_chat_model_start(
        {"name": "llm"},
        [],
        run_id=mc,
        parent_run_id=root,
        invocation_params={
            "generation_config": {"response_mime_type": "application/json", "top_p": 0.9},
            "model": "gemini-x",
        },
    )
    handler.on_llm_end(_llm_result("o"), run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_gen_nested")[0]
    model = _of_type(cap, "model_call")[0]
    assert model["metadata"]["generation_config"] == {
        "top_p": 0.9,
        "response_mime_type": "application/json",
    }


def test_bind_tools_constraints_are_recorded(capturing: Path, read_captures: CaptureReader) -> None:
    """``bind_tools(tool_choice=..., parallel_tool_calls=False)`` lands in invocation_params."""
    handler = EvalShiftCallbackHandler(suite="lc_gen_tools", redact=False, tools=[])
    root, mc = uuid4(), uuid4()

    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_chat_model_start(
        {"name": "llm"},
        [],
        run_id=mc,
        parent_run_id=root,
        invocation_params={
            "model": "gpt-x",
            "tool_choice": {"type": "function", "function": {"name": "search"}},
            "parallel_tool_calls": False,
            "tools": [{"type": "function", "function": {"name": "search"}}],
        },
    )
    handler.on_llm_end(_llm_result("o"), run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_gen_tools")[0]
    AgentTrace.model_validate(cap["trace"])
    model = _of_type(cap, "model_call")[0]
    assert model["metadata"]["generation_config"] == {
        "tool_choice": {"type": "function", "function": {"name": "search"}},
        "parallel_tool_calls": False,
    }


# --- requested tool calls (D-requested) --------------------------------------------------------


def _model_call_for(suite: str, response: Any, read_captures: CaptureReader) -> dict[str, Any]:
    """Drive one chat model call to completion with ``response`` and return its ``model_call``."""
    handler = EvalShiftCallbackHandler(suite=suite, redact=False, tools=[])
    root, mc = uuid4(), uuid4()
    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_chat_model_start({"name": "llm"}, [], run_id=mc, parent_run_id=root)
    handler.on_llm_end(response, run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures(suite)[0]
    AgentTrace.model_validate(cap["trace"])  # the CLI's strict RequestedToolCall must accept it
    return _of_type(cap, "model_call")[0]


def test_requested_tool_calls_from_ai_message(
    capturing: Path, read_captures: CaptureReader
) -> None:
    """``AIMessage.tool_calls`` is already normalised: only args -> arguments, id -> call_id."""
    response = _chat_llm_result(
        "",
        [{"name": "search", "args": {"q": "hi"}, "id": "call_1", "type": "tool_call"}],
    )
    model = _model_call_for("lc_req_one", response, read_captures)
    assert model["requested_tool_calls"] == [
        {"name": "search", "arguments": {"q": "hi"}, "call_id": "call_1"}
    ]


def test_requested_tool_calls_keep_response_order(
    capturing: Path, read_captures: CaptureReader
) -> None:
    response = _chat_llm_result(
        "",
        [
            {"name": "search", "args": {"q": "hi"}, "id": "call_1", "type": "tool_call"},
            {"name": "refund", "args": {"order": 7}, "id": "call_2", "type": "tool_call"},
        ],
    )
    model = _model_call_for("lc_req_two", response, read_captures)
    assert [c["name"] for c in model["requested_tool_calls"]] == ["search", "refund"]
    assert [c["call_id"] for c in model["requested_tool_calls"]] == ["call_1", "call_2"]


def test_chat_message_with_empty_tool_calls_records_empty_list(
    capturing: Path, read_captures: CaptureReader
) -> None:
    """A chat model that asked for nothing is a real value ``[]``, not "not recorded"."""
    model = _model_call_for("lc_req_empty", _chat_llm_result("hi", []), read_captures)
    assert model["requested_tool_calls"] == []


def test_message_without_tool_calls_attribute_records_empty_list(
    capturing: Path, read_captures: CaptureReader
) -> None:
    model = _model_call_for("lc_req_noattr", _llm_result("hi"), read_captures)
    assert model["requested_tool_calls"] == []


def test_plain_generation_without_message_records_nothing(
    capturing: Path, read_captures: CaptureReader
) -> None:
    """A non-chat ``Generation`` says nothing about tool calls -> ``None``, never ``[]``."""
    model = _model_call_for("lc_req_text", _text_llm_result("hi"), read_captures)
    assert model["requested_tool_calls"] is None


def test_requested_tool_call_without_name_is_dropped(
    capturing: Path, read_captures: CaptureReader
) -> None:
    response = _chat_llm_result(
        "",
        [
            {"name": "", "args": {}, "id": "call_1", "type": "tool_call"},
            {"name": "search", "args": {"q": "hi"}, "id": "call_2", "type": "tool_call"},
        ],
    )
    model = _model_call_for("lc_req_noname", response, read_captures)
    assert model["requested_tool_calls"] == [
        {"name": "search", "arguments": {"q": "hi"}, "call_id": "call_2"}
    ]


def test_requested_tool_call_non_dict_args_degrade_to_empty_object(
    capturing: Path, read_captures: CaptureReader
) -> None:
    response = _chat_llm_result(
        "", [{"name": "search", "args": "q=hi", "id": None, "type": "tool_call"}]
    )
    model = _model_call_for("lc_req_badargs", response, read_captures)
    assert model["requested_tool_calls"] == [{"name": "search", "arguments": {}, "call_id": None}]


def test_invalid_tool_calls_are_not_recorded(capturing: Path, read_captures: CaptureReader) -> None:
    """``invalid_tool_calls`` are parse failures, not requests the app could have executed."""
    response = _chat_llm_result(
        "",
        [],
        invalid_tool_calls=[
            {"name": "search", "args": "{not json", "id": "call_1", "error": "boom"}
        ],
    )
    model = _model_call_for("lc_req_invalid", response, read_captures)
    assert model["requested_tool_calls"] == []


def test_requested_tool_calls_are_redacted(capturing: Path, read_captures: CaptureReader) -> None:
    """Requested arguments are model-generated payload, so the redactor walks them (D-requested)."""
    handler = EvalShiftCallbackHandler(suite="lc_req_redact", redact=True, tools=[])
    root, mc = uuid4(), uuid4()
    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_chat_model_start({"name": "llm"}, [], run_id=mc, parent_run_id=root)
    handler.on_llm_end(
        _chat_llm_result(
            "", [{"name": "mail", "args": {"to": "bob@corp.com"}, "id": "c1", "type": "tool_call"}]
        ),
        run_id=mc,
        parent_run_id=root,
    )
    handler.on_chain_end({"output": "ok"}, run_id=root, parent_run_id=None)

    cap = read_captures("lc_req_redact")[0]
    model = _of_type(cap, "model_call")[0]
    assert model["requested_tool_calls"][0]["name"] == "mail"
    assert "bob@corp.com" not in str(model["requested_tool_calls"])


# --- chat history: tool calls stay paired with their results ---------------------------------


def _history_input(
    suite: str, history: list[Any], read_captures: CaptureReader
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one chat model call over ``history``; return the capture and the model call's input."""
    handler = EvalShiftCallbackHandler(suite=suite, redact=False, tools=[])
    root, mc = uuid4(), uuid4()
    handler.on_chain_start({"name": "c"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_chat_model_start({"name": "llm"}, [history], run_id=mc, parent_run_id=root)
    handler.on_llm_end(_chat_llm_result("done", []), run_id=mc, parent_run_id=root)
    handler.on_chain_end({"output": "done"}, run_id=root, parent_run_id=None)
    cap = read_captures(suite)[0]
    return cap, _of_type(cap, "model_call")[0]["input"]


def test_chat_input_keeps_tool_calls_and_tool_call_id(
    capturing: Path, read_captures: CaptureReader
) -> None:
    """The CLI's replay shape: ``tool_calls: [{id, name, arguments}]`` and ``tool_call_id``."""
    history = [
        SimpleNamespace(type="human", content="how many hits for x?"),
        SimpleNamespace(
            type="ai",
            content="",
            tool_calls=[
                {"name": "search", "args": {"q": "x"}, "id": "call_1", "type": "tool_call"}
            ],
        ),
        SimpleNamespace(type="tool", content="3 hits", tool_call_id="call_1"),
    ]
    cap, recorded = _history_input("lc_hist", history, read_captures)

    AgentTrace.model_validate(cap["trace"])  # raises if not CLI-valid
    assert recorded == [
        {"role": "human", "content": "how many hits for x?"},
        {
            "role": "ai",
            "content": "",
            "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "x"}}],
        },
        {"role": "tool", "content": "3 hits", "tool_call_id": "call_1"},
    ]


def test_chat_input_without_tool_calls_keeps_its_shape(
    capturing: Path, read_captures: CaptureReader
) -> None:
    history = [
        SimpleNamespace(type="system", content="be brief"),
        SimpleNamespace(type="human", content="hi"),
        SimpleNamespace(type="ai", content="hello", tool_calls=[]),
    ]
    _cap, recorded = _history_input("lc_hist_plain", history, read_captures)
    assert recorded == [
        {"role": "system", "content": "be brief"},
        {"role": "human", "content": "hi"},
        {"role": "ai", "content": "hello"},
    ]


def test_tool_call_already_in_a_tool_use_block_is_not_repeated(
    capturing: Path, read_captures: CaptureReader
) -> None:
    """ChatAnthropic puts each call in ``content`` as a ``tool_use`` block *and* in ``tool_calls``.

    The CLI reads both, so recording both would replay the call twice.
    """
    history = [
        SimpleNamespace(type="human", content="q"),
        SimpleNamespace(
            type="ai",
            content=[
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
            ],
            tool_calls=[
                {"name": "search", "args": {"q": "x"}, "id": "toolu_1", "type": "tool_call"},
                {"name": "fetch", "args": {"u": "y"}, "id": "toolu_2", "type": "tool_call"},
            ],
        ),
        SimpleNamespace(type="tool", content="3 hits", tool_call_id="toolu_1"),
        SimpleNamespace(type="tool", content="page", tool_call_id="toolu_2"),
    ]
    _cap, recorded = _history_input("lc_hist_anthropic", history, read_captures)
    assert recorded[1]["content"][1]["id"] == "toolu_1"  # the block is kept as recorded
    assert recorded[1]["tool_calls"] == [
        {"id": "toolu_2", "name": "fetch", "arguments": {"u": "y"}}
    ]
    assert [m.get("tool_call_id") for m in recorded[2:]] == ["toolu_1", "toolu_2"]


def test_real_langchain_messages_keep_tool_pairing(
    capturing: Path, read_captures: CaptureReader
) -> None:
    pytest.importorskip("langchain_core")
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    history = [
        HumanMessage(content="how many hits for x?"),
        AIMessage(content="", tool_calls=[{"name": "search", "args": {"q": "x"}, "id": "call_1"}]),
        ToolMessage(content="3 hits", tool_call_id="call_1"),
    ]
    cap, recorded = _history_input("lc_hist_real", history, read_captures)

    AgentTrace.model_validate(cap["trace"])
    assert recorded[1]["tool_calls"] == [
        {"id": "call_1", "name": "search", "arguments": {"q": "x"}}
    ]
    assert recorded[2]["tool_call_id"] == "call_1"
