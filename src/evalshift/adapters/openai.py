"""OpenAI client wrapper: ``wrap_openai(client)`` records one ``model_call`` per request.

Hand :func:`wrap_openai` an ``openai.OpenAI`` or ``openai.AsyncOpenAI`` you already built and use
the returned proxy exactly like the client. Inside a capture session (``@capture.agent``) each
call to one of the intercepted methods records a ``model_call`` -- ``model_id``, ``tools``,
``input``, ``output``, token usage, latency, the allow-listed generation settings and the tool
calls the model requested. Outside a session the proxy is inert (D-wrappers).

**Intercepted:** ``chat.completions.create`` and ``responses.create`` -- sync, async, and
streaming (``stream=True``) forms. **Everything else is forwarded untouched**, including
``chat.completions.parse`` / ``beta``, ``with_raw_response`` / ``with_streaming_response``, the
``responses.stream`` / ``chat.completions.stream`` helper context managers, embeddings, audio,
images and files. Those calls still work through the proxy; they are simply not recorded.

Since any OpenAI-compatible server is reached through the same client (``OpenAI(base_url=...)``
for Ollama, vLLM, Groq, OpenRouter ...), this one wrapper covers them all; a server that omits
``usage`` records zero tokens.

**Recorded shapes.**

* Chat Completions: ``input`` is the ``messages`` list as sent (JSON-coerced), ``output`` the
  first choice's assistant text (``""`` for a tool-call-only turn), usage from
  ``usage.prompt_tokens`` / ``usage.completion_tokens``. Streaming accumulates
  ``choices[0].delta.content`` and reassembles tool-call deltas by ``index`` (the deprecated
  ``function_call`` delta too); usage arrives only on the final chunk, and only when the caller
  passed ``stream_options={"include_usage": True}`` -- otherwise it stays 0.
* Responses: ``input`` is recorded as a **messages-style list** so the CLI's existing
  messages-list handling applies unchanged: a string ``instructions`` becomes a leading
  ``{"role": "system"}`` message, a string ``input`` a ``{"role": "user"}`` message, and a list
  ``input`` is appended item by item as sent (``function_call_output`` items included). Server-
  held state (``previous_response_id`` / ``conversation``) is not expanded: the record shows what
  this request carried. ``output`` is ``output_text`` (or the joined ``output_text`` parts of the
  ``message`` items for a plain-dict response), usage from ``usage.input_tokens`` /
  ``usage.output_tokens``. The flat Responses tool shape (``{type: "function", name,
  parameters}``) is translated to the nested Chat shape before recording so the schema and
  ``strict`` flag survive normalisation. Streaming takes text from
  ``response.output_text.delta`` events and usage + requested tool calls from the terminal
  ``response.completed`` / ``response.incomplete`` event's ``response``; a stream that never
  reaches one records its text with zero usage and no ``requested_tool_calls`` (an honest
  "not recorded", never ``[]``).

**Fail-open, unexpected shapes.** A response without the expected top-level list (``choices`` /
``output``) is handed back to the caller and *not* recorded: a record saying "no output, zero
tokens" would assert something on no evidence. Both pydantic response objects and plain dicts
(what a test double typically returns) are read the same way.

This module never imports ``openai`` -- not even guarded. Everything is duck-typed by attribute
or key, exactly as :mod:`evalshift.capture.requested` and :mod:`evalshift.capture.toolset` do,
so the runtime stays stdlib-only (D-deps) and the ``[openai]`` extra only pins a floor for the
client you construct yourself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from evalshift import config
from evalshift.adapters._wrap import (
    CallSpec,
    ClientProxy,
    Completion,
    Instrumentation,
    StreamState,
)
from evalshift.capture.generation import jsonable
from evalshift.capture.requested import extract_requested_tool_calls

C = TypeVar("C")


# --- duck-typed readers -----------------------------------------------------------------------


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from a dict, or the same-named attribute from anything else; ``None`` if
    absent."""
    try:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)
    except Exception:
        return None


def _items(value: Any) -> list[Any] | None:
    return list(value) if isinstance(value, (list, tuple)) else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _tokens(usage: Any, input_key: str, output_key: str) -> tuple[int, int]:
    return _int(_get(usage, input_key)), _int(_get(usage, output_key))


def _text(content: Any) -> str:
    """A message ``content`` as text: a string as-is, a list of parts by their ``text`` fields."""
    if isinstance(content, str):
        return content
    parts = _items(content)
    if parts is None:
        return ""
    return "".join(t for t in (_get(part, "text") for part in parts) if isinstance(t, str))


def _model_id(kwargs: Mapping[str, Any]) -> str | None:
    model = kwargs.get("model")
    return model if isinstance(model, str) and model else None


def _is_stream(kwargs: Mapping[str, Any], response: Any) -> bool:
    return bool(kwargs.get("stream"))


# --- chat completions -------------------------------------------------------------------------


def _describe_chat(kwargs: Mapping[str, Any]) -> CallSpec | None:
    model = _model_id(kwargs)
    if model is None:
        return None
    tools = kwargs.get("tools")
    return CallSpec(
        model_id=model,
        tools=[] if tools is None else jsonable(tools),
        input=jsonable(kwargs.get("messages")),
        generation_config=dict(kwargs),
    )


def _complete_chat(response: Any) -> Completion | None:
    choices = _items(_get(response, "choices"))
    if choices is None:
        return None
    message = _get(choices[0], "message") if choices else None
    prompt, completion = _tokens(_get(response, "usage"), "prompt_tokens", "completion_tokens")
    return Completion(
        output=_text(_get(message, "content")), input_tokens=prompt, output_tokens=completion
    )


def _on_chat_chunk(chunk: Any, state: StreamState) -> None:
    usage = _get(chunk, "usage")
    if usage is not None:  # only the final chunk carries it, and only with include_usage
        state.input_tokens, state.output_tokens = _tokens(
            usage, "prompt_tokens", "completion_tokens"
        )
    choices = _items(_get(chunk, "choices"))
    if not choices:
        return
    delta = _get(choices[0], "delta")
    content = _get(delta, "content")
    if isinstance(content, str):
        state.text.append(content)
    for call in _items(_get(delta, "tool_calls")) or []:
        pending = state.scratch.setdefault("tool_calls", {})
        index = _get(call, "index")
        key = index if isinstance(index, int) else len(pending)
        entry = pending.setdefault(key, {"id": None, "name": None, "arguments": []})
        _merge_function_delta(entry, _get(call, "function"), _get(call, "id"))
    legacy = _get(delta, "function_call")
    if legacy is not None:
        entry = state.scratch.setdefault(
            "function_call", {"id": None, "name": None, "arguments": []}
        )
        _merge_function_delta(entry, legacy, None)


def _merge_function_delta(entry: dict[str, Any], function: Any, call_id: Any) -> None:
    if isinstance(call_id, str) and call_id:
        entry["id"] = call_id
    name = _get(function, "name")
    if isinstance(name, str) and name:
        entry["name"] = name
    arguments = _get(function, "arguments")
    if isinstance(arguments, str):
        entry["arguments"].append(arguments)


def _assembled(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "type": "function",
        "function": {"name": entry["name"], "arguments": "".join(entry["arguments"])},
    }


def _end_chat_stream(state: StreamState) -> None:
    """Turn the accumulated deltas into a Chat-shaped message and extract its requested calls.

    Going through :func:`extract_requested_tool_calls` keeps one implementation of the argument
    parsing and of the refusal rule (a call with no name poisons the list -> ``None``).
    """
    pending: dict[int, dict[str, Any]] = state.scratch.get("tool_calls") or {}
    legacy = state.scratch.get("function_call")
    message: dict[str, Any] = {}
    if pending:
        message["tool_calls"] = [_assembled(pending[key]) for key in sorted(pending)]
    elif legacy is not None:
        message["function_call"] = _assembled(legacy)["function"]
    state.requested_tool_calls = extract_requested_tool_calls({"choices": [{"message": message}]})


_CHAT = Instrumentation(
    describe=_describe_chat,
    complete=_complete_chat,
    is_stream=_is_stream,
    on_chunk=_on_chat_chunk,
    on_stream_end=_end_chat_stream,
)


# --- responses --------------------------------------------------------------------------------


def _nest_tool(tool: Any) -> Any:
    """Translate the flat Responses function tool to the nested Chat shape; others pass through."""
    if not isinstance(tool, dict) or tool.get("type") != "function" or "function" in tool:
        return tool
    name = tool.get("name")
    if not isinstance(name, str):
        return tool
    function = {
        key: tool[key]
        for key in ("name", "description", "parameters", "strict")
        if tool.get(key) is not None
    }
    return {"type": "function", "function": function}


def _responses_input(kwargs: Mapping[str, Any]) -> list[Any]:
    messages: list[Any] = []
    instructions = kwargs.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    raw = kwargs.get("input")
    if isinstance(raw, str):
        messages.append({"role": "user", "content": raw})
    elif raw is not None:
        coerced = jsonable(raw)
        messages.extend(coerced if isinstance(coerced, list) else [coerced])
    return messages


def _describe_responses(kwargs: Mapping[str, Any]) -> CallSpec | None:
    model = _model_id(kwargs)
    if model is None:
        return None
    tools = kwargs.get("tools")
    return CallSpec(
        model_id=model,
        tools=[] if tools is None else jsonable([_nest_tool(t) for t in _items(tools) or [tools]]),
        input=_responses_input(kwargs),
        generation_config=dict(kwargs),
    )


def _response_text(response: Any) -> str:
    text = _get(response, "output_text")  # the SDK's convenience property
    if isinstance(text, str):
        return text
    parts: list[str] = []
    for item in _items(_get(response, "output")) or []:
        if _get(item, "type") != "message":
            continue
        for block in _items(_get(item, "content")) or []:
            block_text = _get(block, "text")
            if _get(block, "type") == "output_text" and isinstance(block_text, str):
                parts.append(block_text)
    return "".join(parts)


def _complete_responses(response: Any) -> Completion | None:
    if _items(_get(response, "output")) is None:
        return None
    prompt, completion = _tokens(_get(response, "usage"), "input_tokens", "output_tokens")
    return Completion(
        output=_response_text(response), input_tokens=prompt, output_tokens=completion
    )


_TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete"})


def _on_responses_event(event: Any, state: StreamState) -> None:
    kind = _get(event, "type")
    if kind == "response.output_text.delta":
        delta = _get(event, "delta")
        if isinstance(delta, str):
            state.text.append(delta)
    elif kind in _TERMINAL_EVENTS:
        response = _get(event, "response")
        state.input_tokens, state.output_tokens = _tokens(
            _get(response, "usage"), "input_tokens", "output_tokens"
        )
        state.requested_tool_calls = extract_requested_tool_calls(response)
        if not state.text:  # a server that streams no text deltas: fall back to the final text
            state.text.append(_response_text(response))


_RESPONSES = Instrumentation(
    describe=_describe_responses,
    complete=_complete_responses,
    is_stream=_is_stream,
    on_chunk=_on_responses_event,
)


_OVERRIDES: Mapping[str, Any] = {
    "chat": {"completions": {"create": _CHAT}},
    "responses": {"create": _RESPONSES},
}


def wrap_openai(client: C) -> C:
    """Return a recording proxy over an ``openai.OpenAI`` / ``openai.AsyncOpenAI`` client.

    The proxy forwards every attribute of ``client`` and intercepts only
    ``chat.completions.create`` and ``responses.create`` (see the module docstring for what each
    records and what is left untouched). It is typed as returning the client's own type purely
    for editor ergonomics -- at runtime it is a
    :class:`~evalshift.adapters._wrap.ClientProxy`, so ``isinstance(wrapped, OpenAI)`` is
    ``False``; use :func:`evalshift.adapters._wrap.unwrap` to reach the real client.

    Args:
        client: The client to wrap. Wrapping is per instance -- other clients in the process,
            and code that never sees the proxy, are unaffected.

    Returns:
        A drop-in proxy that records inside an active capture session and is inert outside one.

    Raises:
        ~evalshift.SinkConfigurationError: when capture is on and ``EVALSHIFT_SINK`` cannot be
            built.
    """
    config.require_sink_ready()  # a sink the SDK cannot build fails here, at startup
    return cast(C, ClientProxy(client, _OVERRIDES))


__all__ = ["wrap_openai"]
