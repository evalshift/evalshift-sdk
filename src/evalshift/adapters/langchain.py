"""LangChain adapter: a callback handler that captures a chain/agent run with zero hand-wiring.

Drop :class:`EvalShiftCallbackHandler` into any LangChain ``callbacks=[...]`` list and a run is
recorded into the SDK's span tree and written exactly like manual ``@capture.agent``
instrumentation — no decorators on the user's own code.

**Why this can't reuse the contextvar machinery.** The manual API (``evalshift.capture.state``)
infers parentage from the Python call stack: a tool span opened *inside* another tool nests via a
contextvar. LangChain callbacks fire **flat** — every callback carries a ``run_id`` and a
``parent_run_id`` (UUIDs) instead of nesting on the stack. So this handler keeps its own
``run_id -> span`` maps, resolves ``parent_call_id`` by walking the ``parent_run_id`` chain to the
nearest enclosing *tool*, owns the :class:`~evalshift.capture.span.SpanTree` for each root run, and
finalizes the capture when that root run ends. It deliberately does **not** bind
``state.use_tree`` — so mixing this handler with ``@capture.tool``-decorated code won't
double-record (use one or the other).

**Fail-open is sacred.** Every callback body runs under :func:`evalshift.safety.fail_open`, so a
handler fault can never propagate into — and break — the user's chain. Framework payloads
(messages, documents, tool args) are coerced to JSON-able primitives before they reach a span, so
a non-serializable object can't silently drop the capture at sink-write time.

Runtime-optional: ``langchain_core`` is import-guarded, so importing this module without the
``[langchain]`` extra installed does not fail — the SDK stays stdlib-only at runtime (D-deps).
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from evalshift import config, safety
from evalshift.capture import api
from evalshift.capture.generation import GENERATION_KEYS, jsonable, sanitize_generation_config
from evalshift.capture.span import Span, SpanTree
from evalshift.redaction import RedactSetting, resolve_redactor

if TYPE_CHECKING:
    from langchain_core.callbacks import BaseCallbackHandler
else:  # runtime: usable without the langchain extra installed
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except ImportError:  # pragma: no cover - exercised only when the extra is absent
        BaseCallbackHandler = object

#: Sentinel distinguishing "no final output for this finish" from a real ``None`` output.
_UNSET: Any = object()

#: Provenance stamp written into each event's (freeform) ``metadata`` dict.
_ADAPTER = "langchain"


def _coerce_text(value: Any) -> str:
    """Best-effort human-readable string for a chain's final output."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("output", "text", "result", "answer", "content"):
            found = value.get(key)
            if isinstance(found, str):
                return found
    return json.dumps(jsonable(value), ensure_ascii=False)


def _tool_name(serialized: Any) -> str:
    if isinstance(serialized, dict):
        name = serialized.get("name")
        if isinstance(name, str) and name:
            return name
    return "tool"


def _retriever_source(serialized: Any) -> str:
    if isinstance(serialized, dict):
        name = serialized.get("name")
        if isinstance(name, str) and name:
            return name
    return "retriever"


def _model_id(serialized: Any, kwargs: dict[str, Any]) -> str:
    invocation = kwargs.get("invocation_params")
    if isinstance(invocation, dict):
        for key in ("model", "model_name", "model_id", "deployment_name"):
            value = invocation.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(serialized, dict):
        name = serialized.get("name")
        if isinstance(name, str) and name:
            return name
        ident = serialized.get("id")
        if isinstance(ident, list) and ident:
            return str(ident[-1])
    return "unknown"


def _generation_config(kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Known generation params from ``invocation_params``, or ``None`` when there are none.

    Reads the flat params plus a Gemini-style nested ``generation_config`` dict (flat keys win),
    then hands the merge to the shared allow-list + JSON coercion that the manual capture API also
    uses — so a schema object can't poison the sink write and nothing outside
    :data:`~evalshift.capture.generation.GENERATION_KEYS` reaches the (never-redacted) metadata.
    """
    invocation = kwargs.get("invocation_params")
    if not isinstance(invocation, dict):
        return None
    merged: dict[str, Any] = {}
    nested = invocation.get("generation_config")
    if isinstance(nested, dict):
        merged.update(nested)
    merged.update(invocation)
    # Narrow before sanitizing: invocation_params legitimately carries non-generation keys
    # (model, stop, streaming, ...) that are not "dropped" in any interesting sense.
    return sanitize_generation_config({k: merged[k] for k in GENERATION_KEYS if k in merged})


def _messages_to_input(messages: Any) -> list[dict[str, Any]]:
    """Flatten LangChain ``list[list[BaseMessage]]`` into JSON-able ``{role, content}`` dicts."""
    out: list[dict[str, Any]] = []
    for batch in messages or []:
        for message in batch or []:
            out.append(
                {
                    "role": str(getattr(message, "type", "message")),
                    "content": jsonable(getattr(message, "content", str(message))),
                }
            )
    return out


def _documents_to_list(documents: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc in documents or []:
        out.append(
            {
                "page_content": str(getattr(doc, "page_content", doc)),
                "metadata": jsonable(getattr(doc, "metadata", {})),
            }
        )
    return out


def _llm_usage(response: Any) -> tuple[str, int, int]:
    """Extract ``(output_text, input_tokens, output_tokens)`` from an ``LLMResult``, defensively.

    Prefers the modern per-message ``usage_metadata``; falls back to the older
    ``llm_output["token_usage"]`` (OpenAI-style). Any missing piece degrades to ``0`` / ``""``.
    """
    output_text = ""
    input_tokens = 0
    output_tokens = 0
    generations = getattr(response, "generations", None) or []
    if generations and generations[0]:
        first = generations[0][0]
        output_text = str(getattr(first, "text", "") or "")
        message = getattr(first, "message", None)
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict):
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
    if input_tokens == 0 and output_tokens == 0:
        llm_output = getattr(response, "llm_output", None)
        if isinstance(llm_output, dict):
            token_usage = llm_output.get("token_usage")
            if isinstance(token_usage, dict):
                input_tokens = int(
                    token_usage.get("prompt_tokens", token_usage.get("input_tokens", 0)) or 0
                )
                output_tokens = int(
                    token_usage.get("completion_tokens", token_usage.get("output_tokens", 0)) or 0
                )
    return output_text, input_tokens, output_tokens


def _tool_call_field(item: Any, key: str) -> Any:
    """Read one field from a LangChain ``ToolCall`` -- a ``TypedDict``, so normally a plain dict.

    Falls back to attribute access so a provider integration handing back an object-shaped call
    (or a partially-constructed test double) still reads, rather than being silently dropped.
    """
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _requested_tool_calls(response: Any) -> list[dict[str, Any]] | None:
    """The tool calls the model *asked for*, from an ``LLMResult``'s chat message (D-requested).

    LangChain has already normalised the provider's response: ``AIMessage.tool_calls`` items are
    ``{"name": str, "args": dict, "id": str | None, "type": "tool_call"}`` whatever the model
    behind the chain was. So the raw-response walking in
    :mod:`evalshift.capture.requested` is not needed here -- only a rename (``args`` ->
    ``arguments``, ``id`` -> ``call_id``, ``type`` dropped) before the *same* normaliser the
    manual ``record_model_call`` uses, so an empty name is dropped and a non-dict ``args``
    degrades to ``{}`` identically on both paths.

    ``invalid_tool_calls`` is deliberately **not** read: those are calls whose arguments failed to
    parse -- LangChain's record of a malformed generation, not a request an app could have
    dispatched.

    ``[]`` and ``None`` mean different things (D-requested), and the generation shape decides
    which: a chat generation carries an ``AIMessage``, so "no tool calls" there is the real value
    ``[]`` ("the model asked for nothing"); a plain text ``Generation`` has no ``.message`` at all
    and asserts nothing, so the field is left unrecorded (``None``).
    """
    generations = getattr(response, "generations", None) or []
    if not generations or not generations[0]:
        return None
    message = getattr(generations[0][0], "message", None)
    if message is None:
        return None  # a non-chat Generation says nothing about tool calls
    raw = getattr(message, "tool_calls", None) or []
    if not isinstance(raw, (list, tuple)):
        return api._normalize_requested_tool_calls(raw)  # unreadable shape -> logged, None
    mapped = [
        {
            "name": _tool_call_field(item, "name"),
            "arguments": _tool_call_field(item, "args"),
            "call_id": _tool_call_field(item, "id"),
        }
        for item in raw
    ]
    return api._normalize_requested_tool_calls(mapped)


@dataclass
class _Session:
    """Per-root-run capture state. ``inert`` sessions (gate off / sampled out) record nothing."""

    tree: SpanTree | None
    capture_id: str
    agent_input: Any
    inert: bool
    open_spans: dict[UUID, Span] = field(default_factory=dict)
    tool_callid: dict[UUID, str] = field(default_factory=dict)


class EvalShiftCallbackHandler(BaseCallbackHandler):
    """LangChain ``BaseCallbackHandler`` that records a run as an EvalShift capture.

    One handler instance may be reused across many invocations and across threads: per-root state
    is keyed by the root ``run_id`` and guarded by a lock (the underlying ``SpanTree`` is itself
    thread-safe).

    ``redact`` is **required**, exactly as on ``capture.agent`` (D-4c): ``True`` to mask with
    ``default_redactor``, ``False`` to capture verbatim on purpose, or your own
    ``(value) -> value`` callable. Any other value, ``None`` included, raises :exc:`TypeError` at
    construction. If your redactor raises at capture time the capture is dropped rather than
    written unredacted (fail-closed), and the chain is unaffected.

    ``tools`` is also **required** (D-toolset): the toolset this handler's chain was offered, in
    the shape :func:`evalshift.capture.toolset.normalize_tools` accepts. Unlike ``redact``, an
    unrecognised value never raises — normalised once here (mirroring how ``redact`` is resolved
    once), and every ``model_call`` span this handler ever opens (``on_llm_start`` /
    ``on_chat_model_start``, across every chain run this one instance records) is stamped with the
    same resolved toolset. There is no per-call override here the way there is for
    ``record_model_call`` — LangChain callbacks carry no user-supplied ``tools`` kwarg, so the
    handler *is* the "session" and its one constructor-time value is authoritative for its whole
    lifetime. Pass ``tools=[]`` if this chain never binds tools — a real, asserted value.

    ``requested_tool_calls`` needs no wiring at all (D-requested): ``on_llm_end`` reads the tool
    calls the model *asked for* straight off the response's ``AIMessage.tool_calls``, which
    LangChain has already normalised across providers. A chat model that asked for nothing records
    ``[]``; a plain text completion, which cannot ask, records nothing. Streaming needs no special
    case — the aggregated message reaches ``on_llm_end`` with its tool calls intact.
    """

    def __init__(
        self,
        *,
        suite: str,
        redact: RedactSetting,
        tools: Any,
        code_version: str = "",
    ) -> None:
        super().__init__()
        config.require_sink_ready()
        self._suite = suite
        self._code_version = code_version
        self._redact = resolve_redactor(redact)
        self._toolset: tuple[list[dict[str, Any]], str] | None = safety.guard(
            "langchain normalize toolset", lambda: api._normalize_toolset(tools)
        )
        self._lock = threading.Lock()
        self._run_to_root: dict[UUID, UUID] = {}
        self._run_to_parent: dict[UUID, UUID | None] = {}
        self._sessions: dict[UUID, _Session] = {}

    # --- internal session bookkeeping (all map mutations hold self._lock) -------------------

    def _register_run_locked(self, run_id: UUID, parent_run_id: UUID | None) -> UUID:
        root = (
            run_id if parent_run_id is None else self._run_to_root.get(parent_run_id, parent_run_id)
        )
        self._run_to_root[run_id] = root
        self._run_to_parent[run_id] = parent_run_id
        return root

    def _begin_session_locked(self, root: UUID, agent_input: Any) -> None:
        enabled = safety.guard(
            "langchain gate check",
            lambda: config.is_capture_enabled() and config.should_capture_now(),
        )
        tree = safety.guard("langchain open session", SpanTree) if enabled else None
        if tree is None:
            self._sessions[root] = _Session(tree=None, capture_id="", agent_input=None, inert=True)
            return
        self._sessions[root] = _Session(
            tree=tree, capture_id=api._new_capture_id(), agent_input=agent_input, inert=False
        )

    def _ensure_session(
        self, run_id: UUID, parent_run_id: UUID | None, agent_input: Any
    ) -> _Session | None:
        with self._lock:
            root = self._register_run_locked(run_id, parent_run_id)
            if parent_run_id is None and root not in self._sessions:
                self._begin_session_locked(root, agent_input)
            return self._sessions.get(root)

    def _parent_call_id_locked(self, session: _Session, parent_run_id: UUID | None) -> str | None:
        rid = parent_run_id
        while rid is not None:
            call_id = session.tool_callid.get(rid)
            if call_id is not None:
                return call_id
            rid = self._run_to_parent.get(rid)
        return None

    def _start_span(
        self,
        session: _Session,
        run_id: UUID,
        parent_run_id: UUID | None,
        kind: Any,
        span_id: str,
        data: dict[str, Any],
        *,
        is_tool: bool = False,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if session.tree is None:
                return
            parent_call_id = self._parent_call_id_locked(session, parent_run_id)
            metadata: dict[str, Any] = {"adapter": _ADAPTER, "lc_run_id": str(run_id)}
            if extra_metadata:
                metadata.update(extra_metadata)
            span = session.tree.open_span(
                kind,
                span_id=span_id,
                start_ts=api._now(),
                parent_call_id=parent_call_id,
                data=data,
                metadata=metadata,
            )
            session.open_spans[run_id] = span
            if is_tool:
                session.tool_callid[run_id] = span_id

    def _close_span(self, run_id: UUID, **data: Any) -> None:
        with self._lock:
            root = self._run_to_root.get(run_id)
            session = self._sessions.get(root) if root is not None else None
            if session is None or session.tree is None:
                return
            span = session.open_spans.pop(run_id, None)
            if span is None:
                return
            session.tree.close_span(span, end_ts=api._now(), **data)

    def _add_error_event(self, run_id: UUID, exc: BaseException) -> None:
        with self._lock:
            root = self._run_to_root.get(run_id)
            session = self._sessions.get(root) if root is not None else None
            if session is None or session.tree is None:
                return
            ts = api._now()
            span = session.tree.open_span(
                "error",
                span_id=f"err_{uuid.uuid4().hex}",
                start_ts=ts,
                parent_call_id=None,
                data={"message": str(exc), "category": type(exc).__name__},
                metadata={"adapter": _ADAPTER, "lc_run_id": str(run_id)},
            )
            session.tree.close_span(span, end_ts=ts)

    def _record_final_output(self, tree: SpanTree, outputs: Any) -> None:
        ts = api._now()
        span = tree.open_span(
            "final_output",
            span_id=f"fo_{uuid.uuid4().hex}",
            start_ts=ts,
            parent_call_id=None,
            data={"text": _coerce_text(outputs)},
            metadata={"adapter": _ADAPTER},
        )
        tree.close_span(span, end_ts=ts)

    def _finish_root(self, run_id: UUID, *, final_output: Any = _UNSET) -> None:
        """Finalize + write the capture iff ``run_id`` is a tracked root run; else a no-op."""
        with self._lock:
            if self._run_to_root.get(run_id) != run_id:
                return  # not the root of its tree -> nothing to finalize yet
            root = run_id
            session = self._sessions.pop(root, None)
            self._run_to_root = {r: rt for r, rt in self._run_to_root.items() if rt != root}
            self._run_to_parent = {
                r: p for r, p in self._run_to_parent.items() if r in self._run_to_root
            }
        if session is None or session.inert or session.tree is None:
            return
        if final_output is not _UNSET:
            self._record_final_output(session.tree, final_output)
        api._finalize(
            session.tree,
            suite=self._suite,
            agent_input=session.agent_input,
            capture_id=session.capture_id,
            code_version=self._code_version,
            redact=self._redact,
        )

    # --- LangChain callback surface (each body is fail-open) --------------------------------

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_chain_start"):
            # Chains are not a CLI event kind: a root chain opens the session; nested chains are
            # registered only so descendants can resolve their nearest-tool parent.
            self._ensure_session(run_id, parent_run_id, jsonable(inputs))

    def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_chain_end"):
            self._finish_root(run_id, final_output=outputs)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_chain_error"):
            self._add_error_event(run_id, error)
            self._finish_root(run_id)

    def on_llm_start(
        self,
        serialized: Any,
        prompts: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_llm_start"):
            payload = jsonable(prompts)
            session = self._ensure_session(run_id, parent_run_id, payload)
            if session is None:
                return
            gc = _generation_config(kwargs)
            data: dict[str, Any] = {"model_id": _model_id(serialized, kwargs), "input": payload}
            api._stamp_toolset(data, self._toolset)
            self._start_span(
                session,
                run_id,
                parent_run_id,
                "model_call",
                f"mc_{uuid.uuid4().hex}",
                data,
                extra_metadata={"generation_config": gc} if gc else None,
            )

    def on_chat_model_start(
        self,
        serialized: Any,
        messages: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_chat_model_start"):
            payload = _messages_to_input(messages)
            session = self._ensure_session(run_id, parent_run_id, payload)
            if session is None:
                return
            gc = _generation_config(kwargs)
            data: dict[str, Any] = {"model_id": _model_id(serialized, kwargs), "input": payload}
            api._stamp_toolset(data, self._toolset)
            self._start_span(
                session,
                run_id,
                parent_run_id,
                "model_call",
                f"mc_{uuid.uuid4().hex}",
                data,
                extra_metadata={"generation_config": gc} if gc else None,
            )

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_llm_end"):
            output, input_tokens, output_tokens = _llm_usage(response)
            data: dict[str, Any] = {
                "output": output,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
            # Guarded separately so an exploding message property costs the requested calls, not
            # the whole span close. ``None`` -> the key is never written, exactly as
            # ``record_model_call`` leaves it off (absent means "not recorded", never ``[]``).
            requested = safety.guard(
                "langchain requested tool calls", lambda: _requested_tool_calls(response)
            )
            if requested is not None:
                data["requested_tool_calls"] = requested
            self._close_span(run_id, **data)
            self._finish_root(run_id, final_output=output)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_llm_error"):
            self._close_span(run_id, output="")
            self._add_error_event(run_id, error)
            self._finish_root(run_id)

    def on_tool_start(
        self,
        serialized: Any,
        input_str: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: Any = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_tool_start"):
            arguments = jsonable(inputs) if inputs is not None else {"input": jsonable(input_str)}
            session = self._ensure_session(run_id, parent_run_id, arguments)
            if session is None:
                return
            self._start_span(
                session,
                run_id,
                parent_run_id,
                "tool",
                api._new_call_id(),
                {"name": _tool_name(serialized), "arguments": arguments},
                is_tool=True,
            )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_tool_end"):
            self._close_span(run_id, result=jsonable(output))
            self._finish_root(run_id)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_tool_error"):
            # The tool_result already carries the error; no separate error event needed.
            self._close_span(run_id, result=None, error=str(error))
            self._finish_root(run_id)

    def on_retriever_start(
        self,
        serialized: Any,
        query: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_retriever_start"):
            session = self._ensure_session(run_id, parent_run_id, jsonable(query))
            if session is None:
                return
            self._start_span(
                session,
                run_id,
                parent_run_id,
                "retrieval",
                f"ret_{uuid.uuid4().hex}",
                {"source": _retriever_source(serialized), "query": str(query)},
            )

    def on_retriever_end(
        self,
        documents: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_retriever_end"):
            self._close_span(run_id, documents=_documents_to_list(documents))
            self._finish_root(run_id)

    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with safety.fail_open("langchain on_retriever_error"):
            self._close_span(run_id)
            self._add_error_event(run_id, error)
            self._finish_root(run_id)


__all__ = ["EvalShiftCallbackHandler"]
