"""The public capture surface: ``@capture.agent``, ``@capture.tool``, ``record_model_call``.

Recording is **off unless** ``EVALSHIFT_CAPTURE`` is set (the :mod:`evalshift.config` gate). When
off, every entry point is a thin pass-through. When on, an ``@capture.agent`` invocation builds a
:class:`~evalshift.capture.span.SpanTree`, records the tool/model spans made beneath it, and
writes one capture file via :func:`evalshift.config.active_sink`.

**Fail-open is sacred.** The user's own function call is the only statement not wrapped by the
:mod:`evalshift.safety` guards; its real return value and exception always propagate. When the
user function raises, an ``error`` event is recorded and the (partial) capture is still written
before the original exception is re-raised — failed runs are the highest-value telemetry.

Stdlib only (D-deps).
"""

from __future__ import annotations

import functools
import inspect
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager, nullcontext
from typing import Any, ParamSpec, TypeVar, cast, overload

from evalshift import config, safety
from evalshift.capture import state
from evalshift.capture.generation import sanitize_generation_config
from evalshift.capture.span import SpanTree, is_persistable
from evalshift.capture.toolset import fingerprint_tools, normalize_tools
from evalshift.redaction import Redactor, RedactSetting, resolve_redactor
from evalshift.trace.serialize import build_capture

P = ParamSpec("P")
R = TypeVar("R")


def _now() -> float:
    """Wall-clock seconds for span timing (patched in tests for determinism)."""
    return time.time()


def _new_capture_id() -> str:
    return f"cap_{uuid.uuid4().hex}"


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex}"


# --- toolset resolution (D-toolset) ------------------------------------------------------------
#
# Tool definitions are config, not payload -- the same class as ``generation_config`` (see the
# docstring at :func:`record_model_call`). But the *mechanism* that keeps them safe from redaction
# differs, and is worth stating precisely so nobody "fixes" it later. ``generation_config`` is
# exempt because it lives in ``span.metadata``, and ``redact_tree`` (``redaction/base.py``) only
# ever walks ``span.data``. ``tools_offered`` / ``toolset_ref`` are exempt for a different reason:
# they *are* top-level ``span.data`` fields (stamped alongside ``model_id`` / ``input`` /
# ``output`` by the functions below), and are safe only because ``_REDACTABLE_FIELDS`` names
# ``model_call``'s redactable fields one by one and neither toolset field is among them. If a
# future change ever widens that tuple wholesale (e.g. to ``"*"`` or a computed set) rather than
# naming fields individually, it would silently start redacting these two as a side effect.
# ``requested_tool_calls`` (D-requested) is the counter-example that proves the rule: another
# top-level ``span.data`` field stamped by the same functions, but one that *is* in that tuple,
# because a requested call's arguments are model-generated payload, not a tool schema.


def _normalize_toolset(tools: Any) -> tuple[list[dict[str, Any]], str] | None:
    """Normalise one raw ``tools=`` value to ``(normalized, fingerprint)``, or ``None``.

    ``None`` means either ``tools`` matched no shape
    :func:`~evalshift.capture.toolset.normalize_tools` recognises, or it did normalise but
    :func:`~evalshift.capture.toolset.fingerprint_tools` could not hash the result (below) --
    either way there is nothing to stamp on a ``model_call`` event, and nothing for a session to
    offer a call that later inherits it. Used both for one call's own ``tools=`` (via
    :func:`_resolve_call_toolset`) and for a session's ``tools=`` at ``agent`` /
    ``agent_session`` / ``agent_session_async`` entry, where the result is stored in
    :func:`evalshift.capture.state.use_toolset` for the session's duration. This is the single
    funnel both paths normalise through, so it is also the one place that logs a rejection --
    every doc surface that promises "logged at debug" (this module's docstrings, D-toolset,
    DOCS.md, CHANGELOG.md, llms-full.txt) means *this* line.
    """
    normalized = normalize_tools(tools)
    if normalized is None:
        # Mirrors normalize_tools's own "bare sequence, not str/bytes" acceptance check
        # (capture/toolset.py) so a rejected tuple -- or any other Sequence -- gets the same
        # actionable "tuple[N]" shape in the log a rejected list already got, not just the bare
        # type name (which a list alone used to get too, before tuples were accepted).
        if isinstance(tools, Sequence) and not isinstance(tools, (str, bytes, bytearray)):
            shape = f"{type(tools).__name__}[{len(tools)}]"
        else:
            shape = type(tools).__name__
        safety.logger.debug(
            "evalshift: tools=%s did not match a recognised toolset shape (Anthropic/OpenAI/"
            "Gemini tool dict, or a list mixing them) -- recording neither tools_offered nor "
            "toolset_ref; the capture is structurally invalid for this event",
            shape,
        )
        return None
    # normalize_tools has no allow-list (D-toolset): an input_schema is arbitrary user JSON
    # passed through unvalidated, so a value json.dumps cannot serialise (a set, an Enum, a
    # datetime, ...) nested anywhere inside it reaches fingerprint_tools's json.dumps unchanged
    # and raises TypeError there. This must degrade exactly like an unrecognised shape above --
    # neither field stamped, logged at debug, event still recorded -- rather than propagate: an
    # unguarded call here would take the *entire* enclosing model_call event down with it (see
    # api.py's own fail_open at the two per-call sites, record_model_call and
    # _ModelCallRecorder._open, which wrap this whole function and everything else the event
    # needs -- input, output, tokens, cost). guard() logs "fingerprint toolset failed (swallowed)"
    # at debug with the exception traceback, same as any other fail-open bookkeeping fault.
    fingerprint = safety.guard("fingerprint toolset", lambda: fingerprint_tools(normalized))
    if fingerprint is None:
        return None
    return normalized, fingerprint


def _resolve_call_toolset(tools: Any) -> tuple[list[dict[str, Any]], str] | None:
    """Resolve one ``model_call``'s effective toolset.

    ``tools=None`` is the one explicit way a call can decline to assert its own toolset in favor
    of the enclosing session's (``state.current_toolset()``) -- required-but-nullable, not
    optional: the keyword itself must still be passed (``TypeError`` otherwise), but its value
    space includes "defer to the session". Per-call authority is otherwise absolute: any other
    value is normalised on its own terms and always wins, even a value that fails to normalise
    (this does *not* fall back to the session -- a call that asserted something, however badly,
    did not ask to inherit).
    """
    if tools is None:
        return state.current_toolset()
    return _normalize_toolset(tools)


def _stamp_toolset(data: dict[str, Any], resolved: tuple[list[dict[str, Any]], str] | None) -> None:
    """Stamp ``tools_offered`` (always, when ``resolved`` is usable) and ``toolset_ref`` (only if
    the sidecar write succeeds) onto a ``model_call`` span's ``data`` dict, in place.

    ``resolved`` is already normalised -- callers pass either :func:`_resolve_call_toolset`'s
    result (the manual API) or a toolset resolved once at construction (the LangChain adapter,
    which does not participate in :mod:`evalshift.capture.state`). ``None`` leaves ``data``
    unchanged: neither field is stamped, so the event's ``toolset_ref`` / ``tools_offered`` stay
    at their ``None`` default and the capture is structurally invalid for this event -- on
    purpose, so the CLI refuses to promote it rather than trusting an unstamped toolset.
    A sidecar write failure degrades to "no ``toolset_ref``", never a raise: the writer runs under
    :func:`evalshift.safety.guard` (``None`` on any exception), on top of
    :class:`~evalshift.sinks.toolset.ToolsetSink` degrading an ``OSError`` to ``None`` on its
    own. The guard is what keeps a fault in ``ObjectStoreSink.write_toolset`` (a worker thread
    that fails to start, say) from escaping to the caller's ``fail_open`` and dropping the whole
    ``model_call`` event.

    The writer comes from :func:`evalshift.config.toolset_writer`, so the sidecar lands wherever
    the active capture sink itself writes -- the same object store for an ``ObjectStoreSink``,
    the same on-disk base for everything else -- see that function's docstring.
    """
    if resolved is None:
        return
    normalized, fingerprint = resolved
    data["tools_offered"] = [tool["name"] for tool in normalized]
    ref = safety.guard("write toolset", lambda: config.toolset_writer()(normalized, fingerprint))
    if ref is not None:
        data["toolset_ref"] = ref


# --- requested tool calls (D-requested) --------------------------------------------------------


def _normalize_requested_tool_call(item: Any) -> dict[str, Any] | None:
    """Coerce one raw item to exactly ``{name, arguments, call_id}``, or ``None`` if unusable.

    ``name`` is the only load-bearing key: a non-mapping item, or one whose ``name`` is not a
    non-empty string, has nothing worth recording and is dropped. ``arguments`` defaults to ``{}``
    and a non-dict value (an unparsed JSON string, say) degrades to ``{}`` rather than dropping the
    call -- knowing the model asked for ``search_orders`` is worth keeping even when the arguments
    were unreadable. ``call_id`` defaults to ``None`` and is stringified if the provider used a
    non-string id, so the result always satisfies the CLI's strict ``RequestedToolCall``.
    """
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = item.get("arguments")
    call_id = item.get("call_id")
    return {
        "name": name,
        "arguments": dict(arguments) if isinstance(arguments, dict) else {},
        "call_id": call_id if isinstance(call_id, str) or call_id is None else str(call_id),
    }


def _normalize_requested_tool_calls(value: Any) -> list[dict[str, Any]] | None:
    """Normalise a raw ``requested_tool_calls=`` value, or ``None`` to record nothing.

    ``None`` in gives ``None`` out ("not recorded"), and an empty list stays an empty list ("the
    model requested no tools") -- the CLI's fallback to executed tool calls turns on exactly that
    distinction, so the two are never conflated. Anything that is not a list/tuple of items, and a
    non-empty list from which no item survives :func:`_normalize_requested_tool_call`, both degrade
    to ``None`` (logged at ``debug``): "we could not read what the model asked for" is honest,
    ``[]`` would not be. Partial garbage keeps the readable items and logs the rest. Nothing here
    raises -- the enclosing ``model_call`` event is still recorded either way (fail-open).
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        safety.logger.debug(
            "evalshift: requested_tool_calls=%s is not a list of {name, arguments, call_id} "
            "items -- recording nothing for this model call",
            type(value).__name__,
        )
        return None
    candidates = (_normalize_requested_tool_call(item) for item in value)
    normalized = [call for call in candidates if call is not None]
    if len(normalized) != len(value):
        safety.logger.debug(
            "evalshift: dropped %d of %d requested_tool_calls item(s) with no usable 'name'",
            len(value) - len(normalized),
            len(value),
        )
    if value and not normalized:
        return None  # nothing readable -> "not recorded", never an asserted empty list
    return normalized


def _bind(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Best-effort ``{param: value}`` view of a call; never raises (for hashing + tool args)."""
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return {"args": list(args), "kwargs": dict(kwargs)}


def _record_error(tree: SpanTree, exc: BaseException) -> None:
    with safety.fail_open("record error event"):
        ts = _now()
        span = tree.open_span(
            "error",
            span_id=f"err_{uuid.uuid4().hex}",
            start_ts=ts,
            parent_call_id=state.current_parent(),
            # str(exc) is empty for e.g. CancelledError; the trace contract
            # requires a non-empty message, so fall back to the type name.
            data={"message": str(exc) or type(exc).__name__, "category": type(exc).__name__},
        )
        tree.close_span(span, end_ts=ts)


def _finalize(
    tree: SpanTree,
    *,
    suite: str,
    agent_input: Any,
    capture_id: str,
    code_version: str,
    redact: Redactor | None,
    conversation_id: str | None = None,
    turn_index: int | None = None,
    parent_capture_id: str | None = None,
) -> None:
    # Opt-in persistence gate (configure(require_model_call=True)): a capture with no
    # model_call span carries no scoreable ground truth and cannot be promoted by the CLI
    # reader, so drop it rather than write content-free noise. Off by default, preserving the
    # SDK's write-per-invocation contract. Silent; the debug line lets a developer find a drop.
    if config.require_model_call() and not is_persistable(tree):
        safety.logger.debug("evalshift: skipped capture (no model_call): suite=%s", suite)
        return
    # ``redact`` arrives already resolved by the entry point (True -> default_redactor,
    # False -> None, callable -> itself); there is no process-wide fallback to consult (D-4c).
    envelope = safety.guard(
        "build capture",
        lambda: build_capture(
            tree,
            suite=suite,
            agent_input=agent_input,
            capture_id=capture_id,
            code_version=code_version,
            redact=redact,
            conversation_id=conversation_id,
            turn_index=turn_index,
            parent_capture_id=parent_capture_id,
        ),
    )
    if envelope is None:
        return
    with safety.fail_open("sink write"):
        config.active_sink().write(envelope)


def record_model_call(
    *,
    model_id: str,
    tools: Any,
    input: Any = None,
    output: Any = None,
    requested_tool_calls: Any = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int | None = None,
    generation_config: dict[str, Any] | None = None,
) -> None:
    """Record a completed model call into the active capture session. No-op outside one.

    ``tools`` is **required** (D-toolset): the toolset an agent was offered on this call, in
    Anthropic (``{name, description, input_schema}``), OpenAI (``{type: "function", function:
    {...}}``), or Gemini ``types.Tool`` shape (see
    :func:`evalshift.capture.toolset.normalize_tools`) -- a bare list may mix shapes. Pass
    ``tools=[]`` to assert "this call had no tools"; that is a deliberate, real value, not a
    default a forgetful caller falls into (the parameter has none). Pass ``tools=None`` to defer
    to the enclosing session's own ``tools=`` (``capture.agent`` / ``agent_session`` /
    ``agent_session_async``) instead of asserting one for this call -- the session's normalised
    toolset is inherited automatically. A call's own non-``None`` value always wins over the
    session's, even across repeated calls in one session that each choose differently (the
    reason this is per-call at all: a real agent can switch toolsets mid-run). Recorded as
    ``tools_offered`` (the tool names, always stamped when the effective value normalises) and
    ``toolset_ref`` (a content-addressed pointer to the full schema, written once per distinct
    toolset by :class:`~evalshift.sinks.toolset.ToolsetSink` and stamped only if that write
    succeeds). A value matching no recognised shape -- the call's own or, when ``tools=None``, the
    session's -- normalises to ``None``: neither field is stamped, logged at ``debug``, and the
    capture is left structurally invalid for this event rather than guessing. Like
    ``generation_config``, toolsets are config, not payload and never redacted -- unlike it, they
    are **not** allow-listed, because an ``input_schema`` is arbitrary user JSON needed in full to
    dispatch; normalisation only recognises or rejects tool *shapes*, never prunes keys within one.

    ``requested_tool_calls`` (D-requested, schema 2.1.0) is what the **model asked for** in this
    response -- a third, independent fact alongside ``tools`` (what it was *offered*, i.e. allowed
    to ask for) and the ``tool_call`` events ``@capture.tool`` records (what the app *executed*).
    The three diverge routinely -- an app can refuse a requested call, run one the model never
    asked for, or fail before dispatch -- so all three are recorded rather than one inferred from
    another. Pass a list of ``{"name": str, "arguments": dict, "call_id": str | None}`` items;
    :func:`evalshift.capture.requested.extract_requested_tool_calls` builds one from a raw
    Anthropic/OpenAI/Gemini response dict, so a caller rarely writes it by hand. Each item is
    normalised to exactly those three keys (extra provider keys dropped, ``arguments`` defaulting
    to ``{}``, ``call_id`` to ``None``) because the CLI's ``RequestedToolCall`` is
    ``extra="forbid"``. Unlike ``tools``, this is **optional**: omitting it (or passing ``None``)
    records nothing -- an honest "not recorded", distinguishable from ``[]``, which asserts the
    model requested no tools. A malformed value (not a list, or a list with no usable item) is
    dropped fail-open and logged at ``debug``; the event is still recorded. Unlike ``tools``,
    these arguments **are** redacted -- they are model-generated payload, not config, so
    ``_REDACTABLE_FIELDS`` lists the field and the same redactor that masks a tool call's
    arguments masks these (D-4c).

    ``generation_config`` is recorded under the event's ``metadata["generation_config"]`` so the
    CLI can replay the call with the same settings. It is config, not payload — the redactor never
    touches it, so only the ``GENERATION_KEYS`` allow-list is kept (``temperature``,
    ``response_mime_type``, ``response_schema``, ...) and every kept value is JSON-coerced; see
    :func:`evalshift.capture.generation.sanitize_generation_config`. A non-dict value, and one
    with no allow-listed key, records nothing.
    """
    tree = state.current_tree()
    if tree is None:
        return
    with safety.fail_open("record model call"):
        data: dict[str, Any] = {
            "model_id": model_id,
            "input": input,
            "output": output,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }
        if latency_ms is not None:
            data["latency_ms"] = latency_ms
        _stamp_toolset(data, _resolve_call_toolset(tools))
        requested = _normalize_requested_tool_calls(requested_tool_calls)
        if requested is not None:
            data["requested_tool_calls"] = requested
        metadata: dict[str, Any] = {}
        sanitized = sanitize_generation_config(generation_config)
        if sanitized is not None:
            metadata["generation_config"] = sanitized
        ts = _now()
        span = tree.open_span(
            "model_call",
            span_id=f"mc_{uuid.uuid4().hex}",
            start_ts=ts,
            parent_call_id=state.current_parent(),
            data=data,
            metadata=metadata,
        )
        tree.close_span(span, end_ts=ts)


def _run_tool(
    fn: Callable[P, R],
    tool_name: str,
    tree: SpanTree,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> R:
    call_id = _new_call_id()
    arguments = safety.guard("bind tool args", lambda: _bind(fn, args, kwargs)) or {}
    parent = state.current_parent()
    start = _now()
    span = safety.guard(
        "open tool span",
        lambda: tree.open_span(
            "tool",
            span_id=call_id,
            start_ts=start,
            parent_call_id=parent,
            data={"name": tool_name, "arguments": arguments},
        ),
    )
    parent_cm = state.use_parent(call_id) if span is not None else nullcontext()
    with parent_cm:
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            if span is not None:
                with safety.fail_open("close tool span (error)"):
                    tree.close_span(span, end_ts=_now(), result=None, error=str(exc))
            raise
        else:
            if span is not None:
                with safety.fail_open("close tool span"):
                    tree.close_span(span, end_ts=_now(), result=result)
            return result


async def _run_tool_async(
    fn: Callable[P, Any],
    tool_name: str,
    tree: SpanTree,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    # Async mirror of _run_tool. The sync ``with state.use_parent(call_id)`` set/resets its
    # contextvar token in *this* task, so parentage holds across the await; asyncio.gather copies
    # the context into child tasks, so concurrent tools each see the right parent (state.py docs).
    call_id = _new_call_id()
    arguments = safety.guard("bind tool args", lambda: _bind(fn, args, kwargs)) or {}
    parent = state.current_parent()
    start = _now()
    span = safety.guard(
        "open tool span",
        lambda: tree.open_span(
            "tool",
            span_id=call_id,
            start_ts=start,
            parent_call_id=parent,
            data={"name": tool_name, "arguments": arguments},
        ),
    )
    parent_cm = state.use_parent(call_id) if span is not None else nullcontext()
    with parent_cm:
        try:
            result = await fn(*args, **kwargs)
        except BaseException as exc:
            if span is not None:
                with safety.fail_open("close tool span (error)"):
                    tree.close_span(span, end_ts=_now(), result=None, error=str(exc))
            raise
        else:
            if span is not None:
                with safety.fail_open("close tool span"):
                    tree.close_span(span, end_ts=_now(), result=result)
            return result


def _run_agent(
    fn: Callable[P, R],
    *,
    suite: str,
    code_version: str,
    redact: Redactor | None,
    tools: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    conversation_id: str | None = None,
    turn_index: int | None = None,
    parent_capture_id: str | None = None,
) -> R:
    if not config.should_capture_now():
        return fn(*args, **kwargs)  # not sampled -> transparent pass-through (no tree built)
    tree = safety.guard("open session", SpanTree)
    if tree is None:
        return fn(*args, **kwargs)  # bookkeeping failed -> transparent pass-through
    agent_input = safety.guard("derive agent input", lambda: _bind(fn, args, kwargs))
    session_toolset = safety.guard("normalize session toolset", lambda: _normalize_toolset(tools))
    capture_id = _new_capture_id()
    with state.use_tree(tree), state.use_toolset(session_toolset):
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            _record_error(tree, exc)
            _finalize(
                tree,
                suite=suite,
                agent_input=agent_input,
                capture_id=capture_id,
                code_version=code_version,
                redact=redact,
                conversation_id=conversation_id,
                turn_index=turn_index,
                parent_capture_id=parent_capture_id,
            )
            raise
        else:
            _finalize(
                tree,
                suite=suite,
                agent_input=agent_input,
                capture_id=capture_id,
                code_version=code_version,
                redact=redact,
                conversation_id=conversation_id,
                turn_index=turn_index,
                parent_capture_id=parent_capture_id,
            )
            return result


async def _run_agent_async(
    fn: Callable[P, Any],
    *,
    suite: str,
    code_version: str,
    redact: Redactor | None,
    tools: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    conversation_id: str | None = None,
    turn_index: int | None = None,
    parent_capture_id: str | None = None,
) -> Any:
    # Async mirror of _run_agent. _record_error/_finalize are sync (Sink.write is sync), so the
    # async path reuses them verbatim — no await. Same fail-open + error-event-then-raise contract.
    if not config.should_capture_now():
        return await fn(*args, **kwargs)  # not sampled -> transparent pass-through (no tree)
    tree = safety.guard("open session", SpanTree)
    if tree is None:
        return await fn(*args, **kwargs)  # bookkeeping failed -> transparent pass-through
    agent_input = safety.guard("derive agent input", lambda: _bind(fn, args, kwargs))
    session_toolset = safety.guard("normalize session toolset", lambda: _normalize_toolset(tools))
    capture_id = _new_capture_id()
    with state.use_tree(tree), state.use_toolset(session_toolset):
        try:
            result = await fn(*args, **kwargs)
        except BaseException as exc:
            _record_error(tree, exc)
            _finalize(
                tree,
                suite=suite,
                agent_input=agent_input,
                capture_id=capture_id,
                code_version=code_version,
                redact=redact,
                conversation_id=conversation_id,
                turn_index=turn_index,
                parent_capture_id=parent_capture_id,
            )
            raise
        else:
            _finalize(
                tree,
                suite=suite,
                agent_input=agent_input,
                capture_id=capture_id,
                code_version=code_version,
                redact=redact,
                conversation_id=conversation_id,
                turn_index=turn_index,
                parent_capture_id=parent_capture_id,
            )
            return result


class _ModelCallRecorder:
    """Streaming model-call span: open on enter, record accumulated text + usage once on close.

    Dual-protocol — usable as ``with capture.model_call(...)`` or ``async with`` — so it wraps
    either a sync generator or an ``async for`` token stream. Accumulate output with
    :meth:`add_text`, usage with :meth:`set_usage`, and the tool calls the model asked for with
    :meth:`set_requested_tool_calls` (all optional); on exit it records exactly one ``model_call``
    span carrying the joined output. No active session => inert (no span, no write).
    Every bookkeeping step is fail-open, so a recorder fault never breaks the host stream loop.
    """

    __slots__ = (
        "_generation_config",
        "_input",
        "_model_id",
        "_parts",
        "_requested_tool_calls",
        "_span",
        "_tools",
        "_tree",
        "_usage",
    )

    def __init__(
        self,
        *,
        model_id: str,
        tools: Any,
        input: Any,
        generation_config: dict[str, Any] | None = None,
    ) -> None:
        self._model_id = model_id
        self._input = input
        self._tools = tools
        self._parts: list[str] = []
        self._usage: dict[str, Any] = {}
        self._requested_tool_calls: list[dict[str, Any]] | None = None
        self._tree: SpanTree | None = None
        self._span: Any = None
        self._generation_config: dict[str, Any] | None = safety.guard(
            "model_call sanitize generation_config",
            lambda: sanitize_generation_config(generation_config),
        )

    def add_text(self, text: str) -> None:
        """Append a streamed chunk to the accumulated model output."""
        with safety.fail_open("model_call add_text"):
            self._parts.append(text)

    def set_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int | None = None,
    ) -> None:
        """Record token counts / cost (optional; omit to keep serializer defaults)."""
        with safety.fail_open("model_call set_usage"):
            usage: dict[str, Any] = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            }
            if latency_ms is not None:
                usage["latency_ms"] = latency_ms
            self._usage = usage

    def set_requested_tool_calls(self, calls: Any) -> None:
        """Record the tool calls the model asked for in this response (last write wins).

        Usable before or during the ``with`` block -- a streamed tool call is typically only
        complete once its argument deltas have all arrived. ``calls`` is a list of
        ``{"name": str, "arguments": dict, "call_id": str | None}`` items (see
        :func:`evalshift.capture.requested.extract_requested_tool_calls` for building one from a
        raw provider response), normalised to exactly those three keys. Requested is not executed:
        see :func:`record_model_call` for the full contract, including why a malformed value is
        dropped fail-open rather than raised and why these arguments *are* redacted.
        """
        with safety.fail_open("model_call set_requested_tool_calls"):
            normalized = _normalize_requested_tool_calls(calls)
            if normalized is None:
                return
            self._requested_tool_calls = normalized
            if self._span is not None:
                self._span.data["requested_tool_calls"] = normalized

    def set_generation_config(self, config: dict[str, Any]) -> None:
        """Record the call's generation config (last write wins).

        Usable mid-stream — e.g. when the effective settings only become known from the first
        response chunk. Recorded under the event's ``metadata["generation_config"]``; it is
        config, not payload, so the redactor never touches it and only the allow-listed,
        JSON-coerced keys are kept (see :func:`record_model_call`).
        """
        with safety.fail_open("model_call set_generation_config"):
            sanitized = sanitize_generation_config(config)
            if sanitized is not None:
                self._generation_config = sanitized
                if self._span is not None:
                    self._span.metadata["generation_config"] = sanitized

    def _open(self) -> None:
        self._tree = state.current_tree()
        if self._tree is None:  # no active agent session -> inert
            return
        with safety.fail_open("open model_call span"):
            metadata: dict[str, Any] = {}
            if self._generation_config is not None:
                metadata["generation_config"] = self._generation_config
            data: dict[str, Any] = {"model_id": self._model_id, "input": self._input}
            _stamp_toolset(data, _resolve_call_toolset(self._tools))
            # A value set before the ``with`` (the recorder is constructed first) is carried onto
            # the span here; one set inside the block is stamped straight onto ``span.data`` by
            # :meth:`set_requested_tool_calls`. Either way it is in place before redaction runs.
            if self._requested_tool_calls is not None:
                data["requested_tool_calls"] = self._requested_tool_calls
            self._span = self._tree.open_span(
                "model_call",
                span_id=f"mc_{uuid.uuid4().hex}",
                start_ts=_now(),
                parent_call_id=state.current_parent(),
                data=data,
                metadata=metadata,
            )

    def _close(self) -> None:
        if self._tree is None or self._span is None:
            return
        with safety.fail_open("close model_call span"):
            self._tree.close_span(
                self._span, end_ts=_now(), output="".join(self._parts), **self._usage
            )
        self._span = None  # idempotent: a second exit is a no-op

    def __enter__(self) -> _ModelCallRecorder:
        self._open()
        return self

    def __exit__(self, *exc: object) -> None:
        self._close()

    async def __aenter__(self) -> _ModelCallRecorder:
        self._open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._close()


class _Capture:
    """Public capture facade; the module exposes a single instance named ``capture``."""

    def agent(
        self,
        *,
        suite: str,
        redact: RedactSetting,  # required: masks payloads before any byte hits disk (D-4c)
        tools: Any,  # required: the toolset offered on this session's calls (D-toolset)
        code_version: str = "",
        conversation_id: str | None = None,
        turn_index: int | None = None,
        parent_capture_id: str | None = None,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Decorator that captures one agent invocation (no-op unless the gate is on).

        Works on both ``def`` and ``async def`` agents: coroutine functions are detected and wrapped
        in an async wrapper that awaits the call (contextvars propagate across ``await`` and into
        ``asyncio.gather`` child tasks, so concurrent tool calls get correct parentage).

        ``redact`` is **required** — captures hold the inside of a run (tool arguments and results,
        model input and output), so masking is an explicit decision at every capture point, never a
        default (D-4c). Pass:

        * ``True`` — mask with :func:`~evalshift.default_redactor`. It covers emails, ``sk-…`` and
          ``AKIA…`` API keys, and ``Bearer`` tokens, and **nothing else**; structured secrets
          (SSNs, account numbers, internal id formats) need a callable of your own.
        * ``False`` — capture payloads verbatim, on purpose. Right for goldens you need byte-exact.
        * a ``(value) -> value`` callable — your own redactor.

        Any other value, ``None`` included, raises :exc:`TypeError` at decoration time, whether or
        not the capture gate is on. If your redactor raises at capture time the capture is dropped
        rather than written unredacted (D-4a) — the host agent is never affected.

        ``tools`` is also **required** (D-toolset) — this session's toolset, in the shape
        :func:`~evalshift.capture.toolset.normalize_tools` accepts (Anthropic / OpenAI / Gemini /
        a mixed list). Unlike ``redact``, an unrecognised value never raises — it degrades to no
        session toolset (logged at ``debug``), the same as any ``model_call`` whose own ``tools``
        doesn't normalise. Every ``record_model_call`` / ``capture.model_call`` invoked from
        inside this decorated function inherits this value automatically when it passes its own
        ``tools=None``; a call that passes its own list always overrides it (the switching case —
        one agent, one suite, two toolsets chosen by an ``if`` — is why this is per-call at all,
        not decorator-static like ``conversation_id`` below). Pass ``tools=[]`` if this agent
        never calls tools — a real, asserted value, not a default.

        ``conversation_id`` / ``turn_index`` / ``parent_capture_id`` (schema 1.1.0) stamp
        multi-turn conversation identity onto every capture this decorated function writes.
        Because a decorator's keyword arguments are fixed at decoration time, these are **static**
        for every call — fine for a single-turn agent, but every invocation of the decorated
        function would carry the same ``turn_index``. For a dynamic, per-turn conversation (the
        common case — one call per turn, each with its own ``turn_index``), use
        :meth:`agent_session` or :meth:`agent_session_async` instead and pass fresh values on each
        ``with``/``async with``.
        """
        redactor = resolve_redactor(redact)  # eager: an invalid value fails at decoration time

        def decorate(fn: Callable[P, R]) -> Callable[P, R]:
            if inspect.iscoroutinefunction(fn):
                afn = cast("Callable[P, Any]", fn)

                @functools.wraps(fn)
                async def awrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                    if not config.is_capture_enabled():
                        return await afn(*args, **kwargs)
                    return await _run_agent_async(
                        afn,
                        suite=suite,
                        code_version=code_version,
                        redact=redactor,
                        tools=tools,
                        args=args,
                        kwargs=kwargs,
                        conversation_id=conversation_id,
                        turn_index=turn_index,
                        parent_capture_id=parent_capture_id,
                    )

                return cast("Callable[P, R]", awrapper)

            @functools.wraps(fn)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                if not config.is_capture_enabled():
                    return fn(*args, **kwargs)
                return _run_agent(
                    fn,
                    suite=suite,
                    code_version=code_version,
                    redact=redactor,
                    tools=tools,
                    args=args,
                    kwargs=kwargs,
                    conversation_id=conversation_id,
                    turn_index=turn_index,
                    parent_capture_id=parent_capture_id,
                )

            return wrapper

        return decorate

    @contextmanager
    def agent_session(
        self,
        *,
        suite: str,
        redact: RedactSetting,  # required: see :meth:`agent` for the accepted values (D-4c)
        tools: Any,  # required: see :meth:`agent` for the accepted values (D-toolset)
        code_version: str = "",
        agent_input: Any = None,
        conversation_id: str | None = None,
        turn_index: int | None = None,
        parent_capture_id: str | None = None,
    ) -> Iterator[SpanTree | None]:
        """Context-manager form of :meth:`agent` for inline/manual instrumentation.

        ``tools`` behaves exactly as in :meth:`agent` — this session's toolset, inherited by any
        ``record_model_call`` / ``capture.model_call`` inside the block that passes its own
        ``tools=None``, overridable per call.

        ``conversation_id`` / ``turn_index`` / ``parent_capture_id`` (schema 1.1.0) stamp
        multi-turn conversation identity onto this one capture. Unlike the ``@agent`` decorator,
        each ``with`` block can pass fresh values — this is the recommended way to capture a
        dynamic, per-turn conversation (one ``with capture.agent_session(...)`` per turn).
        """
        redactor = resolve_redactor(redact)  # eager: ahead of the gate, so a bad value always fails
        if not config.is_capture_enabled():
            yield None
            return
        if not config.should_capture_now():
            yield None
            return
        tree = safety.guard("open session", SpanTree)
        if tree is None:
            yield None
            return
        session_toolset = safety.guard(
            "normalize session toolset", lambda: _normalize_toolset(tools)
        )
        capture_id = _new_capture_id()
        with state.use_tree(tree), state.use_toolset(session_toolset):
            try:
                yield tree
            except BaseException as exc:
                _record_error(tree, exc)
                _finalize(
                    tree,
                    suite=suite,
                    agent_input=agent_input,
                    capture_id=capture_id,
                    code_version=code_version,
                    redact=redactor,
                    conversation_id=conversation_id,
                    turn_index=turn_index,
                    parent_capture_id=parent_capture_id,
                )
                raise
            else:
                _finalize(
                    tree,
                    suite=suite,
                    agent_input=agent_input,
                    capture_id=capture_id,
                    code_version=code_version,
                    redact=redactor,
                    conversation_id=conversation_id,
                    turn_index=turn_index,
                    parent_capture_id=parent_capture_id,
                )

    @asynccontextmanager
    async def agent_session_async(
        self,
        *,
        suite: str,
        redact: RedactSetting,  # required: see :meth:`agent` for the accepted values (D-4c)
        tools: Any,  # required: see :meth:`agent` for the accepted values (D-toolset)
        code_version: str = "",
        agent_input: Any = None,
        conversation_id: str | None = None,
        turn_index: int | None = None,
        parent_capture_id: str | None = None,
    ) -> AsyncIterator[SpanTree | None]:
        """``async with`` form of :meth:`agent_session` for inline async instrumentation.

        Body identical to the sync session: ``_finalize``/``_record_error`` are sync (no I/O to
        await), so this generator never awaits internally; the ``state.use_tree`` token is set and
        reset in the caller's task. ``tools`` and ``conversation_id`` / ``turn_index`` /
        ``parent_capture_id`` behave exactly as in :meth:`agent_session` — pass fresh values per
        ``async with`` for a dynamic, per-turn conversation.
        """
        redactor = resolve_redactor(redact)  # eager: ahead of the gate, so a bad value always fails
        if not config.is_capture_enabled():
            yield None
            return
        if not config.should_capture_now():
            yield None
            return
        tree = safety.guard("open session", SpanTree)
        if tree is None:
            yield None
            return
        session_toolset = safety.guard(
            "normalize session toolset", lambda: _normalize_toolset(tools)
        )
        capture_id = _new_capture_id()
        with state.use_tree(tree), state.use_toolset(session_toolset):
            try:
                yield tree
            except BaseException as exc:
                _record_error(tree, exc)
                _finalize(
                    tree,
                    suite=suite,
                    agent_input=agent_input,
                    capture_id=capture_id,
                    code_version=code_version,
                    redact=redactor,
                    conversation_id=conversation_id,
                    turn_index=turn_index,
                    parent_capture_id=parent_capture_id,
                )
                raise
            else:
                _finalize(
                    tree,
                    suite=suite,
                    agent_input=agent_input,
                    capture_id=capture_id,
                    code_version=code_version,
                    redact=redactor,
                    conversation_id=conversation_id,
                    turn_index=turn_index,
                    parent_capture_id=parent_capture_id,
                )

    def model_call(
        self,
        *,
        model_id: str,
        tools: Any,
        input: Any = None,
        generation_config: dict[str, Any] | None = None,
    ) -> _ModelCallRecorder:
        """Open a streaming model-call recorder (sync ``with`` or ``async with``).

        Accumulate output via :meth:`_ModelCallRecorder.add_text`, usage via
        :meth:`_ModelCallRecorder.set_usage`, and the tool calls the model asked for via
        :meth:`_ModelCallRecorder.set_requested_tool_calls`; exactly one ``model_call`` span is
        recorded on exit. Use :func:`record_model_call` instead for an already-complete (atomic)
        call.

        ``tools`` is **required**, with the same contract as :func:`record_model_call`'s: the
        toolset offered on this call, ``[]`` to assert none, or ``None`` to inherit the enclosing
        session's (``capture.agent`` / ``agent_session`` / ``agent_session_async``). Resolved once
        the recorder actually opens a span (a ``with``/``async with`` outside any active session
        stays inert, as before — no toolset resolution or sidecar write happens for a no-op).

        ``generation_config`` is allow-listed and JSON-coerced into the event's
        ``metadata["generation_config"]`` (see :func:`record_model_call`); values that only
        become known mid-stream go through :meth:`_ModelCallRecorder.set_generation_config`.
        """
        return _ModelCallRecorder(
            model_id=model_id, tools=tools, input=input, generation_config=generation_config
        )

    @overload
    def tool(self, fn: Callable[P, R]) -> Callable[P, R]: ...

    @overload
    def tool(self, *, name: str | None = None) -> Callable[[Callable[P, R]], Callable[P, R]]: ...

    def tool(self, fn: Callable[..., Any] | None = None, *, name: str | None = None) -> Any:
        """Decorator that records a tool span (no-op when no agent session is active).

        Supports both ``@capture.tool`` and ``@capture.tool(name="...")``.
        """

        def decorate(target: Callable[P, R]) -> Callable[P, R]:
            tool_name = name or target.__name__

            if inspect.iscoroutinefunction(target):
                atarget = cast("Callable[P, Any]", target)

                @functools.wraps(target)
                async def awrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                    tree = state.current_tree()
                    if tree is None:
                        return await atarget(*args, **kwargs)
                    return await _run_tool_async(atarget, tool_name, tree, args, kwargs)

                return cast("Callable[P, R]", awrapper)

            @functools.wraps(target)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                tree = state.current_tree()
                if tree is None:
                    return target(*args, **kwargs)
                return _run_tool(target, tool_name, tree, args, kwargs)

            return wrapper

        return decorate if fn is None else decorate(fn)


capture = _Capture()


__all__ = ["capture", "record_model_call"]
