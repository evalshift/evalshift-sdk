"""Google GenAI (Gemini) client wrapper: ``wrap_genai(client)`` records one ``model_call`` per
request.

Hand :func:`wrap_genai` a ``google.genai.Client`` you already built and use the returned proxy
exactly like the client. Inside a capture session (``@capture.agent``) each call to one of the
intercepted methods records a ``model_call`` -- ``model_id``, ``tools``, ``input``, ``output``,
token usage, latency, the allow-listed generation settings and the function calls the model
requested. Outside a session the proxy is inert (D-wrappers).

**Intercepted:** ``models.generate_content``, ``models.generate_content_stream`` and their async
twins ``aio.models.generate_content`` / ``aio.models.generate_content_stream``. **Everything else
is forwarded untouched** -- ``chats`` (a ``Chat`` calls the *unwrapped* ``models`` it was created
from, so its turns are not recorded; call ``models.generate_content`` with the history yourself
to capture a multi-turn loop), ``embed_content``, ``count_tokens``, ``files``, ``caches``,
``batches``, ``tunings`` and ``live``. Those still work through the proxy; they are simply not
recorded.

**Recorded shapes.**

* ``input`` is a **messages-style list** -- ``[{"role", "content", ...}, ...]`` -- not the raw
  ``contents``. The CLI's promote step recovers a case from ``model_call.input`` by recognising a
  list of ``{"role", "content"}`` dicts (last ``user`` message = the current turn, everything
  before it = history, a leading ``system`` message = the system prompt); a dumped
  ``types.Content`` is ``{"role", "parts"}`` and would fall through to "could not recover
  inputs", and a bare string would lose the system instruction. So ``contents`` is translated
  the way the SDK's own ``t_contents`` reads it: a string, a ``Part`` or a run of loose
  parts/strings is one ``user`` turn (text joined with newlines); a ``Content`` object or dict
  keeps its role (``user`` / ``model``, which the CLI maps to ``assistant``). Function-call parts
  become ``tool_calls: [{id, name, arguments}]`` on their message; each function-response part
  becomes its own ``{"role": "tool", "content": <JSON of response>, "tool_call_id", "name"}``
  message, since a "user" turn that only carries tool results is not a user turn to replay.
  ``config.system_instruction`` (string, ``Content``, ``Part`` or a list of those) is folded in as
  a leading ``{"role": "system"}`` message. Only text is recorded: ``inline_data`` / ``file_data``
  / executable-code parts contribute nothing to ``content`` (an image as base64 is not a
  replayable input, and would dwarf the capture).
* ``output`` is the first candidate's text parts joined, skipping ``thought`` parts -- what
  ``response.text`` returns, assembled here because ``response.text`` logs a warning (and
  returns ``None``) for a function-call-only turn. ``""`` for such a turn.
* ``tools`` is ``config.tools`` translated to the canonical ``{name, description, input_schema}``
  dicts, or ``[]`` when the config has none. Translation happens here rather than in
  :mod:`evalshift.capture.toolset` because google-genai sends two things the shared normaliser
  cannot see: a **python callable** (automatic function calling) is declared on the wire via
  ``FunctionDeclaration.from_callable_with_api_option(..., use_json_schema=True)``, and a
  declaration may carry ``parameters_json_schema`` (plain JSON Schema) instead of the
  ``parameters`` ``Schema`` object the normaliser reads. Callables are declared through that
  same SDK classmethod, imported lazily and guarded; without the SDK the callable is left in the
  list and the toolset is *not stamped* (an unrecognised toolset is never guessed as ``[]``).
  A ``Tool`` carrying only a built-in (``google_search``, ``code_execution`` ...) has no
  declarations and normalises to nothing, so that call's toolset is not stamped either.
  With automatic function calling **enabled** (the default when callables are passed) the SDK
  runs the whole call/execute/re-ask loop inside one ``generate_content``: one ``model_call`` is
  recorded for it, whose ``requested_tool_calls`` are those of the *final* response (usually
  ``[]``) -- the executed calls live in ``response.automatic_function_calling_history`` and are
  not recorded here. Disable AFC (``automatic_function_calling={"disable": True}``) and dispatch
  with ``@capture.tool`` to see them as requested/executed calls.
* usage from ``usage_metadata.prompt_token_count`` / ``candidates_token_count`` (thinking tokens
  are reported separately by Gemini and are not added in).
* ``generation_config`` is the config as a dict -- ``model_dump(exclude_none=True)`` of a
  ``GenerateContentConfig``, a dict as is -- allow-listed by ``record_model_call`` (Gemini's
  ``temperature``, ``top_p``, ``max_output_tokens``, ``response_mime_type``, ``response_schema``,
  ``tool_config`` spellings are all in :data:`~evalshift.capture.generation.GENERATION_KEYS`).

**Streaming.** Each chunk is a ``GenerateContentResponse``: text parts are accumulated,
function-call parts collected across chunks (Gemini streams each whole, never as argument
deltas) and turned into ``requested_tool_calls`` when the stream ends -- ``[]`` when there were
none. Usage is taken from the last chunk that carries ``usage_metadata``. The async stream method
is an ``async def`` that resolves to an async iterator, so ``await client.aio.models
.generate_content_stream(...)`` hands back an :class:`~evalshift.adapters._wrap.AsyncStreamProxy`.

**Fail-open, unexpected shapes.** A response without a ``candidates`` list is handed back to the
caller and *not* recorded. Pydantic response objects and plain dicts (snake_case ``function_call``
or REST camelCase ``functionCall``) are read the same way; nothing is ``isinstance``-checked
against a google-genai type. ``google.genai`` is imported only lazily, inside a guard, and only
to declare python callables -- importing this module without the ``[google-genai]`` extra works
(D-deps).
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterable, Mapping
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


def _get_either(obj: Any, snake: str, camel: str) -> Any:
    """``snake`` (python SDK) or ``camel`` (REST / serialised) spelling, whichever is present."""
    value = _get(obj, snake)
    return _get(obj, camel) if value is None else value


def _items(value: Any) -> list[Any] | None:
    return list(value) if isinstance(value, (list, tuple)) else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _config_dict(config: Any) -> dict[str, Any] | None:
    """The ``config`` kwarg as a dict: a dict as is, a ``GenerateContentConfig`` dumped."""
    if config is None:
        return None
    if isinstance(config, dict):
        return dict(config)
    model_dump = getattr(config, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(exclude_none=True)
        except Exception:
            return None
        return dumped if isinstance(dumped, dict) else None
    return None


# --- input: contents -> messages-style list ---------------------------------------------------


def _part_text(part: Any) -> str | None:
    """A part's text, or ``None`` for a non-text part; a bare string is its own text."""
    if isinstance(part, str):
        return part
    text = _get(part, "text")
    return text if isinstance(text, str) else None


def _is_content(value: Any) -> bool:
    """A ``Content`` (object or ``{"role"/"parts"}`` dict) rather than a loose part."""
    if isinstance(value, str):
        return False
    if isinstance(value, dict):
        return "parts" in value
    return _items(_get(value, "parts")) is not None


def _tool_call(function_call: Any) -> dict[str, Any]:
    return {
        "id": _get(function_call, "id"),
        "name": _get(function_call, "name"),
        "arguments": jsonable(_get(function_call, "args")) or {},
    }


def _tool_result(function_response: Any) -> dict[str, Any]:
    response = jsonable(_get(function_response, "response"))
    return {
        "role": "tool",
        "content": response if isinstance(response, str) else json.dumps(response),
        "tool_call_id": _get(function_response, "id"),
        "name": _get(function_response, "name"),
    }


def _messages_from_parts(role: str, parts: Iterable[Any]) -> list[dict[str, Any]]:
    """One ``role`` message from the text / function-call parts, then one ``tool`` message per
    function-response part."""
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for part in parts:
        text = _part_text(part)
        if text is not None:
            texts.append(text)
            continue
        function_call = _get_either(part, "function_call", "functionCall")
        if function_call is not None:
            calls.append(_tool_call(function_call))
            continue
        function_response = _get_either(part, "function_response", "functionResponse")
        if function_response is not None:
            results.append(_tool_result(function_response))
    messages: list[dict[str, Any]] = []
    if texts or calls or not results:
        message: dict[str, Any] = {"role": role, "content": "\n".join(texts)}
        if calls:
            message["tool_calls"] = calls
        messages.append(message)
    messages.extend(results)
    return messages


def _content_messages(content: Any, default_role: str) -> list[dict[str, Any]]:
    role = _get(content, "role")
    parts = _items(_get(content, "parts")) or []
    return _messages_from_parts(role if isinstance(role, str) and role else default_role, parts)


def _loose_parts_role(parts: list[Any]) -> str:
    """A run of loose parts is a ``model`` turn only when it carries a function call (as the SDK's
    ``t_contents`` decides), else a ``user`` turn."""
    for part in parts:
        if _get_either(part, "function_call", "functionCall") is not None:
            return "model"
    return "user"


def _messages(contents: Any, default_role: str = "user") -> list[dict[str, Any]]:
    """Translate a ``contents`` / ``system_instruction`` value to messages (see module doc)."""
    if contents is None:
        return []
    items = contents if isinstance(contents, list) else [contents]
    messages: list[dict[str, Any]] = []
    loose: list[Any] = []

    def flush() -> None:
        if loose:
            role = default_role if default_role != "user" else _loose_parts_role(loose)
            messages.extend(_messages_from_parts(role, loose))
            loose.clear()

    for item in items:
        if _is_content(item):
            flush()
            messages.extend(_content_messages(item, default_role))
        else:
            loose.append(item)
    flush()
    return messages


def _input(contents: Any, config: Any) -> list[dict[str, Any]]:
    system = _messages(_get(config, "system_instruction"), default_role="system")
    return [*system, *_messages(contents)]


# --- tools ------------------------------------------------------------------------------------


def _declare_callable(function: Any) -> Any:
    """Declare a python callable the way the SDK does on the wire; ``None`` without the SDK."""
    try:
        from google.genai import types
    except ImportError:
        return None
    try:
        return types.FunctionDeclaration.from_callable_with_api_option(
            callable=function, use_json_schema=True
        )
    except TypeError:  # an older SDK without ``use_json_schema``
        return types.FunctionDeclaration.from_callable_with_api_option(callable=function)


def _canonical_declaration(declaration: Any) -> dict[str, Any] | None:
    name = _get(declaration, "name")
    if not isinstance(name, str) or not name:
        return None
    schema = _get_either(declaration, "parameters_json_schema", "parametersJsonSchema")
    if schema is None:
        schema = _get(declaration, "parameters")
    coerced = jsonable(schema) if schema is not None else {}
    return {
        "name": name,
        "description": _get(declaration, "description") or "",
        "input_schema": coerced if isinstance(coerced, dict) else {},
    }


def _canonical_tool(tool: Any) -> list[Any]:
    """One ``config.tools`` entry -> canonical tool dicts; anything unrecognised passes through
    unchanged so the shared normaliser gets the final say (and refuses what it cannot read)."""
    if inspect.isfunction(tool) or inspect.ismethod(tool):
        declaration = _declare_callable(tool)
        if declaration is None:
            return [tool]
        canonical = _canonical_declaration(declaration)
        return [tool] if canonical is None else [canonical]
    declarations = _get_either(tool, "function_declarations", "functionDeclarations")
    if not isinstance(declarations, Iterable) or isinstance(declarations, (str, bytes)):
        return [tool]
    canonical_all: list[Any] = []
    for declaration in declarations:
        canonical = _canonical_declaration(declaration)
        if canonical is None:
            return [tool]
        canonical_all.append(canonical)
    return canonical_all


def _tools(config: Any) -> list[Any]:
    raw = _get(config, "tools")
    if raw is None:
        return []
    tools = _items(raw)
    if tools is None:
        tools = [raw]
    return [canonical for tool in tools for canonical in _canonical_tool(tool)]


# --- describe / complete ----------------------------------------------------------------------


def _describe(kwargs: Mapping[str, Any]) -> CallSpec | None:
    model = kwargs.get("model")
    if not isinstance(model, str) or not model:
        return None
    config = kwargs.get("config")
    return CallSpec(
        model_id=model,
        tools=_tools(config),
        input=_input(kwargs.get("contents"), config),
        generation_config=_config_dict(config),
    )


def _first_parts(response: Any) -> list[Any] | None:
    """``candidates[0].content.parts``; ``[]`` for no candidates, ``None`` if not that shape."""
    candidates = _items(_get(response, "candidates"))
    if candidates is None:
        return None
    if not candidates:
        return []
    return _items(_get(_get(candidates[0], "content"), "parts")) or []


def _output_text(parts: Iterable[Any]) -> str:
    return "".join(
        text
        for text in (_part_text(part) for part in parts if not _get(part, "thought"))
        if text is not None
    )


def _usage(response: Any) -> tuple[int, int] | None:
    usage = _get_either(response, "usage_metadata", "usageMetadata")
    if usage is None:
        return None
    return (
        _int(_get_either(usage, "prompt_token_count", "promptTokenCount")),
        _int(_get_either(usage, "candidates_token_count", "candidatesTokenCount")),
    )


def _complete(response: Any) -> Completion | None:
    parts = _first_parts(response)
    if parts is None:
        return None
    prompt, candidates = _usage(response) or (0, 0)
    return Completion(output=_output_text(parts), input_tokens=prompt, output_tokens=candidates)


def _is_stream(kwargs: Mapping[str, Any], response: Any) -> bool:
    return hasattr(response, "__next__") or hasattr(response, "__anext__")


def _on_chunk(chunk: Any, state: StreamState) -> None:
    usage = _usage(chunk)
    if usage is not None:
        state.input_tokens, state.output_tokens = usage
    parts = _first_parts(chunk) or []
    state.text.append(_output_text(parts))
    for part in parts:
        function_call = _get_either(part, "function_call", "functionCall")
        if function_call is not None:
            state.scratch.setdefault("function_calls", []).append(jsonable(function_call))


def _on_stream_end(state: StreamState) -> None:
    """Assemble the collected function calls into a Gemini-shaped response and extract them, so
    the argument coercion and the refusal rule (a call with no name -> ``None``) stay in one
    place."""
    calls = state.scratch.get("function_calls") or []
    parts = [{"function_call": call} for call in calls]
    state.requested_tool_calls = extract_requested_tool_calls(
        {"candidates": [{"content": {"parts": parts}}]}
    )


_GENERATE = Instrumentation(describe=_describe, complete=_complete)
_GENERATE_STREAM = Instrumentation(
    describe=_describe,
    complete=lambda response: None,  # a non-iterator from a stream method: record nothing
    is_stream=_is_stream,
    on_chunk=_on_chunk,
    on_stream_end=_on_stream_end,
)

_MODELS: Mapping[str, Any] = {
    "generate_content": _GENERATE,
    "generate_content_stream": _GENERATE_STREAM,
}
_OVERRIDES: Mapping[str, Any] = {"models": _MODELS, "aio": {"models": _MODELS}}


def wrap_genai(client: C) -> C:
    """Return a recording proxy over a ``google.genai.Client``.

    The proxy forwards every attribute of ``client`` and intercepts only
    ``models.generate_content``, ``models.generate_content_stream`` and their ``aio.models``
    twins (see the module docstring for what is recorded and what is left untouched). It is
    typed as returning the client's own type purely for editor ergonomics -- at runtime it is a
    :class:`~evalshift.adapters._wrap.ClientProxy`, so ``isinstance(wrapped, Client)`` is
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


__all__ = ["wrap_genai"]
