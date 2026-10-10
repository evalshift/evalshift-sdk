"""Anthropic client wrapper: ``wrap_anthropic(client)`` records one ``model_call`` per request.

A drop-in proxy over an ``anthropic.Anthropic`` or ``anthropic.AsyncAnthropic`` instance the user
already built (D-wrappers). Exactly two methods are intercepted:

* ``client.messages.create(...)`` -- sync and async, including ``stream=True`` (the returned
  ``Stream`` / ``AsyncStream`` of raw events is proxied and recorded once it ends).
* ``client.messages.stream(...)`` -- the ``with client.messages.stream(...) as stream:`` helper,
  sync and async. The context manager is proxied; the yielded ``MessageStream`` is handed back
  **unchanged** (``text_stream``, ``until_done()``, ``get_final_message()`` all work), and one
  ``model_call`` is recorded on exit from the SDK's own accumulated final message.

Everything else forwards untouched and is **out of scope**: ``client.beta.*``,
``client.messages.batches``, ``client.messages.count_tokens``, ``with_raw_response`` /
``with_streaming_response`` and the ``parse`` helpers are not recorded.

What one recorded ``model_call`` carries:

* ``model_id`` -- the ``model`` kwarg.
* ``tools`` -- the ``tools`` kwarg as sent, or ``[]`` when absent (never ``None``; D-wrappers).
* ``input`` -- the ``messages`` list, with the top-level ``system`` kwarg folded in as a leading
  ``{"role": "system", "content": ...}`` message (see :func:`_input`). Coerced through
  :func:`~evalshift.capture.generation.jsonable`.
* ``output`` -- the response's ``text`` blocks joined. ``tool_use`` blocks are recorded separately
  as ``requested_tool_calls`` (extracted by the base from the final message, or assembled here
  from ``content_block_start`` + ``input_json_delta`` events when streaming).
* ``input_tokens`` / ``output_tokens`` -- ``usage.input_tokens`` / ``usage.output_tokens``; from
  ``message_start`` and ``message_delta`` when streaming.
* ``generation_config`` -- the raw kwargs, allow-listed by ``GENERATION_KEYS`` in the recorder.

Shapes are duck-typed (attribute or ``.get``), so dict and pydantic responses both work, and this
module never imports ``anthropic``: the SDK stays stdlib-only at runtime (D-deps). Fail-open as
in ``_wrap``: the real call is never guarded and a wrapper fault degrades to "not recorded".
"""

from __future__ import annotations

import json
import time
from typing import Any, TypeVar, cast

from evalshift import config, safety
from evalshift.adapters._wrap import (
    CallSpec,
    ClientProxy,
    Completion,
    Instrumentation,
    StreamState,
    record,
)
from evalshift.capture.generation import jsonable
from evalshift.capture.requested import extract_requested_tool_calls

C = TypeVar("C")

#: ``StreamState.scratch`` key under which in-flight ``tool_use`` blocks are kept, by block index.
_TOOL_USE = "tool_use"


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from a dict (``.get``) or an object (attribute); ``None`` when absent."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _input(messages: Any, system: Any) -> Any:
    """The recorded ``input``: ``messages`` with ``system`` prepended as a ``system`` message.

    Anthropic takes the system prompt as a top-level ``system`` kwarg (a string or a list of text
    blocks) rather than as a message. The CLI's replay (``captures/promote.py``) recovers a case
    from a ``model_call.input`` that is a messages list -- the last ``user`` message becomes the
    current turn, the rest becomes ``history``, and a leading ``{"role": "system"}`` message is
    the only place a system prompt is looked for. Folding ``system`` into the list is therefore
    what makes it replayable; anywhere else it would be invisible. A ``system`` that is a list of
    blocks is kept as-is (like any block-list message content).
    """
    recorded = (
        jsonable(list(messages)) if isinstance(messages, (list, tuple)) else jsonable(messages)
    )
    if isinstance(recorded, list) and isinstance(system, (str, list, tuple)) and system:
        return [{"role": "system", "content": jsonable(system)}, *recorded]
    return recorded


def _describe(kwargs: Any) -> CallSpec | None:
    model = kwargs.get("model")
    if not isinstance(model, str) or not model:
        return None
    tools = kwargs.get("tools")
    return CallSpec(
        model_id=model,
        tools=list(tools) if isinstance(tools, (list, tuple)) else [],
        input=_input(kwargs.get("messages"), kwargs.get("system")),
        generation_config=dict(kwargs),
    )


def _text_of(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if _get(block, "type") == "text":
            text = _get(block, "text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _complete(response: Any) -> Completion | None:
    """A finished ``Message`` -> :class:`Completion`; ``None`` (record nothing) for other shapes."""
    content = _get(response, "content")
    if not isinstance(content, list):
        return None
    usage = _get(response, "usage")
    return Completion(
        output=_text_of(content),
        input_tokens=_int(_get(usage, "input_tokens")),
        output_tokens=_int(_get(usage, "output_tokens")),
    )


# --- messages.create(stream=True): raw event stream --------------------------------------------


def _is_stream(kwargs: Any, response: Any) -> bool:
    return kwargs.get("stream") is True


def _on_chunk(event: Any, state: StreamState) -> None:
    kind = _get(event, "type")
    if kind == "message_start":
        usage = _get(_get(event, "message"), "usage")
        state.input_tokens = _int(_get(usage, "input_tokens"))
        state.output_tokens = _int(_get(usage, "output_tokens"))
    elif kind == "content_block_start":
        block = _get(event, "content_block")
        if _get(block, "type") == "tool_use":
            pending = state.scratch.setdefault(_TOOL_USE, {})
            pending[_int(_get(event, "index"))] = {
                "id": _get(block, "id"),
                "name": _get(block, "name"),
                "parts": [],
            }
    elif kind == "content_block_delta":
        delta = _get(event, "delta")
        delta_type = _get(delta, "type")
        if delta_type == "text_delta":
            text = _get(delta, "text")
            if isinstance(text, str):
                state.text.append(text)
        elif delta_type == "input_json_delta":
            entry = state.scratch.get(_TOOL_USE, {}).get(_int(_get(event, "index")))
            fragment = _get(delta, "partial_json")
            if entry is not None and isinstance(fragment, str):
                entry["parts"].append(fragment)
    elif kind == "message_delta":
        usage = _get(event, "usage")
        output_tokens = _get(usage, "output_tokens")
        if isinstance(output_tokens, int):
            state.output_tokens = output_tokens
        input_tokens = _get(usage, "input_tokens")
        if isinstance(input_tokens, int):
            state.input_tokens = input_tokens


def _on_stream_end(state: StreamState) -> None:
    """Assemble the streamed ``tool_use`` blocks into ``requested_tool_calls`` (``[]`` if none)."""
    pending: dict[int, dict[str, Any]] = state.scratch.get(_TOOL_USE, {})
    blocks: list[dict[str, Any]] = []
    for index in sorted(pending):
        entry = pending[index]
        raw = "".join(entry["parts"])
        try:
            arguments = json.loads(raw) if raw.strip() else {}
        except ValueError:
            safety.logger.debug(
                "evalshift: streamed tool_use input was not valid JSON; recorded as {}"
            )
            arguments = {}
        blocks.append(
            {"type": "tool_use", "id": entry["id"], "name": entry["name"], "input": arguments}
        )
    state.requested_tool_calls = extract_requested_tool_calls({"content": blocks}) if blocks else []


_CREATE = Instrumentation(
    describe=_describe,
    complete=_complete,
    is_stream=_is_stream,
    on_chunk=_on_chunk,
    on_stream_end=_on_stream_end,
)


# --- messages.stream(): the MessageStreamManager helper ----------------------------------------


class _ManagerBase:
    """Proxy over a ``MessageStreamManager``; the yielded ``MessageStream`` is returned unchanged.

    Recording happens once, on exit, from the message the SDK accumulated itself:

    * On a clean exit, ``stream.get_final_message()`` -- which returns immediately when the caller
      consumed the stream (``text_stream``, iteration, ``until_done()``) and otherwise reads the
      remainder first, so an abandoned-but-successful stream is still recorded in full.
    * When the ``with`` body raised, ``stream.current_message_snapshot`` -- whatever had arrived,
      with no further network read on an error path.

    If either raises (e.g. the stream errored before ``message_start``), nothing is recorded.
    Latency is measured from enter to exit. Every other attribute forwards to the manager.
    """

    __slots__ = ("_manager", "_spec", "_start", "_stream")

    def __init__(self, manager: Any, spec: CallSpec) -> None:
        self._manager = manager
        self._spec = spec
        self._start = 0.0
        self._stream: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    def __repr__(self) -> str:
        return f"<evalshift stream wrapper of {self._manager!r}>"

    def _record(self, final: Any) -> None:
        latency = max(0, int((time.perf_counter() - self._start) * 1000))
        completion = safety.guard("wrapper complete", lambda: _complete(final))
        if completion is not None:
            record(self._spec, completion, latency_ms=latency, raw_response=final)


class _StreamManagerProxy(_ManagerBase):
    __slots__ = ()

    def __enter__(self) -> Any:
        self._start = time.perf_counter()
        self._stream = self._manager.__enter__()
        return self._stream

    def __exit__(self, *exc: Any) -> Any:
        stream = self._stream
        final = safety.guard(
            "wrapper final message",
            lambda: stream.current_message_snapshot if exc[0] else stream.get_final_message(),
        )
        if final is not None:
            self._record(final)
        return self._manager.__exit__(*exc)


class _AsyncStreamManagerProxy(_ManagerBase):
    __slots__ = ()

    async def __aenter__(self) -> Any:
        self._start = time.perf_counter()
        self._stream = await self._manager.__aenter__()
        return self._stream

    async def __aexit__(self, *exc: Any) -> Any:
        stream = self._stream
        final: Any = None
        with safety.fail_open("wrapper final message"):
            final = stream.current_message_snapshot if exc[0] else await stream.get_final_message()
        if final is not None:
            self._record(final)
        return await self._manager.__aexit__(*exc)


# --- the proxies ------------------------------------------------------------------------------


class _MessagesProxy(ClientProxy):
    """``client.messages``: ``create`` instrumented, ``stream`` wrapped in a manager proxy."""

    __slots__ = ()

    def __init__(self, target: Any) -> None:
        super().__init__(target, {"create": _CREATE})

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        spec = safety.guard("wrapper describe", lambda: _describe(kwargs))
        manager = self._target.stream(*args, **kwargs)
        if spec is None:
            return manager
        if hasattr(manager, "__aenter__"):
            return _AsyncStreamManagerProxy(manager, spec)
        if hasattr(manager, "__enter__"):
            return _StreamManagerProxy(manager, spec)
        return manager


class _AnthropicProxy(ClientProxy):
    __slots__ = ()

    def __init__(self, target: Any) -> None:
        super().__init__(target, {})

    @property
    def messages(self) -> _MessagesProxy:
        return _MessagesProxy(self._target.messages)


def wrap_anthropic(client: C) -> C:
    """Wrap an ``anthropic.Anthropic`` / ``anthropic.AsyncAnthropic`` instance for capture.

    Returns a proxy that forwards everything to ``client`` and records one ``model_call`` for
    each ``messages.create`` (sync, async, ``stream=True``) and ``messages.stream`` call made
    inside an active capture session (``@capture.agent`` ...). Outside a session it is inert.
    The proxy is not an instance of the client's class; use
    :func:`evalshift.adapters._wrap.unwrap` to get the real client back. See the module
    docstring for what is recorded and what is out of scope. Typed as returning the client's
    own type purely for editor ergonomics (like ``wrap_openai`` / ``wrap_genai``).

    Raises:
        ~evalshift.SinkConfigurationError: when capture is on and ``EVALSHIFT_CAPTURE_STORE``
            cannot be built.
    """
    config.require_sink_ready()  # a sink the SDK cannot build fails here, at startup
    return cast(C, _AnthropicProxy(client))


__all__ = ["wrap_anthropic"]
