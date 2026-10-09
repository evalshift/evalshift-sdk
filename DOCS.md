# EvalShift SDK Documentation

The EvalShift SDK is an **in-process capture SDK** for AI agents. You install it inside your agent process, wrap the boundaries you care about — the agent invocation, its model calls, its tool calls — and the SDK records each run as a structured trace and writes it as a JSON capture file to local disk, or — on hosts whose disk does not outlive them — to an object store you own.

- **Distribution name:** `evalshift-sdk` · **import name:** `evalshift` · **version:** 0.5.0
- **Python:** >= 3.10 · **runtime dependencies:** none (stdlib only) · **fully typed** (`py.typed` ships)
- **License:** [MIT](LICENSE)
- **No network by default.** The SDK writes to the local filesystem (or an in-memory buffer) unless you configure an [`ObjectStoreSink`](#objectstoresink-and-cloud-stores), which ships captures to a bucket *you* own — never to EvalShift. Captures are consumed by the separate [evalshift CLI](https://github.com/evalshift/evalshift-cli); the layout is the interface between the two, on disk or in a bucket.
- **Off by default.** Nothing is recorded unless the `EVALSHIFT_CAPTURE` environment variable is set to a truthy value. Instrumentation is safe to leave in production code paths permanently.

---

## Table of contents

1. [Installation](#installation)
2. [Quickstart](#quickstart)
3. [How it works](#how-it-works)
4. [Instrumenting your agent](#instrumenting-your-agent)
5. [Configuration](#configuration)
6. [Sinks](#sinks)
7. [Redaction](#redaction)
8. [Async and concurrency](#async-and-concurrency)
9. [LangChain integration](#langchain-integration)
10. [Provider client wrappers](#provider-client-wrappers)
11. [Reading captures programmatically](#reading-captures-programmatically)
12. [API reference](#api-reference)
13. [Troubleshooting / FAQ](#troubleshooting--faq)

---

## Installation

```bash
pip install evalshift-sdk
# or
uv add evalshift-sdk
```

Optional LangChain integration:

```bash
pip install "evalshift-sdk[langchain]"   # adds langchain-core>=0.2
```

Optional provider client wrappers (see [Provider client wrappers](#provider-client-wrappers)):

```bash
pip install "evalshift-sdk[openai]"        # openai>=1.40
pip install "evalshift-sdk[anthropic]"     # anthropic>=0.40
pip install "evalshift-sdk[google-genai]"  # google-genai>=1.0
```

Every adapter module is import-guarded: importing `evalshift.adapters.langchain` (or `.openai`, `.anthropic`, `.genai`) without the matching extra installed does not fail — the SDK stays dependency-free at runtime.

Optional object-store sinks (see [Sinks → `ObjectStoreSink`](#objectstoresink-and-cloud-stores)) use the provider's own client library — install it like any other package:

```bash
pip install boto3                              # s3:// — Amazon S3 and S3-compatible stores
pip install google-cloud-storage               # gs://
pip install azure-storage-blob azure-identity  # az://
```

> **Co-install note:** the EvalShift CLI (PyPI `evalshift`, import package `evalshift_cli`) depends on this SDK, so both live in one environment and `pip install evalshift` brings the SDK with it. Production agents that only record captures install `evalshift-sdk` alone.

---

## Quickstart

Instrument a minimal agent with three primitives: `@capture.agent` marks the agent boundary, `@capture.tool` records tool calls, and `record_model_call` records a completed model call.

```python
# agent.py
from evalshift import capture, record_model_call


@capture.tool(name="search_orders")
def search_orders(customer_id: str) -> dict:
    return {"orders": [{"id": "12345", "status": "delivered"}]}


@capture.tool(name="issue_refund")
def issue_refund(order_id: str) -> dict:
    return {"status": "refunded", "order_id": order_id}


@capture.agent(suite="support_demo", redact=True, tools=[])  # this agent never switches toolsets
def handle_ticket(query: str) -> str:
    # tools= is required at every model call too -- it has no default at any entry point. The
    # decorator's tools=[] above only becomes this session's *inherited* toolset for a call that
    # explicitly passes tools=None; a call's own value always wins.
    record_model_call(
        model_id="demo/router", tools=[], input={"query": query}, output="On it."
    )
    search_orders(customer_id="customer_42")
    if "refund" in query.lower():
        issue_refund(order_id="12345")
        return "Refund issued for order #12345."
    return "Your order is on its way."


if __name__ == "__main__":
    handle_ticket("I need a refund for order #12345, the item arrived damaged.")
```

Run it with capture enabled:

```bash
EVALSHIFT_CAPTURE=1 python agent.py
```

One capture file appears at `.evalshift/captures/support_demo/cap_<hex>.json` (relative to the current working directory). Trimmed real output:

```json
{
  "schema_version": "2.1.0",
  "capture_id": "cap_203ce5041fe042298d0e3b5b9910e178",
  "suite": "support_demo",
  "input_hash": "7af43bb32e9f7fc4cbb43d99126edba4c1f4e575d20997f297ce9f02f8395c4b",
  "code_version": "",
  "created_at": "2026-07-20T22:22:31.082267+00:00",
  "trace": {
    "run_id": "cap_203ce5041fe042298d0e3b5b9910e178",
    "prompt_id": "support_demo",
    "example_id": "cap_203ce5041fe042298d0e3b5b9910e178",
    "role": "source",
    "events": [
      {
        "type": "model_call",
        "sequence_index": 0,
        "model_id": "demo/router",
        "input": {"query": "I need a refund for order #12345, the item arrived damaged."},
        "output": "On it.",
        "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latency_ms": 0,
        "toolset_ref": "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "tools_offered": [],
        "requested_tool_calls": null,
        "timestamp": "2026-07-20T22:22:31.081939+00:00",
        "metadata": {"evalshift": {"span_id": "mc_…", "start_ts": 1784586151.081939, "end_ts": 1784586151.081939}}
      },
      {
        "type": "tool_call",
        "sequence_index": 1,
        "name": "search_orders",
        "arguments": {"customer_id": "customer_42"},
        "call_id": "call_26a525ed688a4ba0af7c226f380e1a50",
        "parent_call_id": null,
        "timestamp": "2026-07-20T22:22:31.081975+00:00",
        "metadata": {"evalshift": {"…": "…"}}
      },
      {
        "type": "tool_result",
        "sequence_index": 2,
        "name": "search_orders",
        "call_id": "call_26a525ed688a4ba0af7c226f380e1a50",
        "result": {"orders": [{"id": "12345", "status": "delivered"}]},
        "error": null,
        "timestamp": "2026-07-20T22:22:31.081983+00:00",
        "metadata": {"evalshift": {"…": "…", "input_hash": "d1db9fc0…"}}
      }
    ]
  },
  "conversation_id": null,
  "turn_index": null,
  "parent_capture_id": null
}
```

> **Nothing is recorded unless `EVALSHIFT_CAPTURE` is set** (to `1`, `true`, `yes`, or `on`). With the gate off, every wrapper is a pure pass-through with no measurable work. This is what makes it safe to ship instrumented code to production and flip capture on only when you want telemetry.

---

## How it works

### The capture pipeline

```
call to @capture.agent-wrapped function
        │
        ▼
  gate check ──── EVALSHIFT_CAPTURE not truthy? → run function untouched, record nothing
        │
        ▼
  sampling ────── sample_rate set and this run not drawn? → run function untouched
        │
        ▼
  open session ── a SpanTree is created and bound via a contextvar;
        │          nested @capture.tool / record_model_call / capture.model_call
        │          calls attach to it (works across await and asyncio.gather)
        ▼
  record spans ── each tool/model call opens and closes a timed Span
        │          (monotonic op-counter keeps concurrent spans deterministically ordered)
        ▼
  finalize ────── 1. require_model_call gate (opt-in): drop if no model_call span
        │          2. redaction (required `redact=`): mask payloads in memory, pre-serialization
        │          3. serialize: span tree → ordered trace events → capture envelope
        ▼
  sink write ──── dedup check → base sink write (disk, memory, or object store) → GC (disk only)
```

Span-to-event mapping: a **tool span expands into two events** — a `tool_call` at its start and a `tool_result` at its close. A `model_call` or `error` span becomes one event. Events get a dense `sequence_index` ordered by `(timestamp, monotonic op-order)`, so concurrent operations serialize deterministically. Span timing and parentage are stashed under each event's `metadata["evalshift"]` block.

### Fail-open is the core promise

A capture bug must never break or slow your agent:

- **Your function call is the only statement the SDK does not wrap in a guard.** Its return value and its exceptions always propagate exactly as if the SDK were not there.
- Every piece of SDK bookkeeping — opening spans, serializing, writing — is individually guarded. A fault degrades to a dropped capture plus one `debug`-level log line, never an exception into your code. (A few failures deliberately log at `WARNING` instead, because they would otherwise lose every capture silently: the first failed object-store upload — see [`ObjectStoreSink`](#objectstoresink-and-cloud-stores) — and the once-per-process backstop when `EVALSHIFT_CAPTURE` turns on after an unusable `EVALSHIFT_SINK` was already recorded, below.)
- **Failed runs are still captured.** When your agent raises, the SDK records an `error` event, writes the (partial) capture, and then re-raises your original exception. Failed runs are the highest-value telemetry.

Three deliberate exceptions to fail-open:

| Path | Behavior | Why |
|---|---|---|
| Redaction | **Fail-closed** — a raising redactor drops the whole capture (your agent still runs normally) | Never write a possibly-unredacted file |
| Read side (`load_capture` / `load_envelope`) | **Raises** typed `MigrationError` subclasses | Reading is tooling, not the hot path; silent bad reads are worse than loud ones |
| Sink configuration (`EVALSHIFT_SINK`) | **Raises** `SinkConfigurationError` at the first explicit touch while capture is on — never on bare import, never with capture off | A bucket you asked for and did not get is the loss this feature exists to prevent; a crash in the deploy log beats captures quietly written to a disk that dies with the task |

### Anatomy of a capture file

Each capture is one JSON file: an **envelope** wrapping a trace.

| Envelope key | Meaning |
|---|---|
| `schema_version` | Envelope schema version (currently `"2.1.0"`) |
| `capture_id` | Unique id, `cap_<hex>`; also the file name |
| `suite` | The suite you passed to `@capture.agent` / `agent_session` — the grouping unit on disk |
| `input_hash` | SHA-256 of the agent's bound input (the raw input itself is not stored at the envelope level); dedup key |
| `code_version` | Whatever you passed as `code_version` (e.g. a git SHA); `""` by default |
| `created_at` | ISO-8601 UTC timestamp |
| `trace` | The event list: `model_call`, `tool_call`, `tool_result`, `retrieval`, `guardrail`, `final_output`, `error` |
| `conversation_id`, `turn_index`, `parent_capture_id` | Optional multi-turn identity (schema 1.1.0), `null` for standalone captures |

Full schema-versioning and migration policy: [docs/SCHEMA.md](docs/SCHEMA.md).

---

## Instrumenting your agent

There are four ways to wrap agent calls. They share the same pipeline and configuration; pick per call site.

1. **Decorators** — `@capture.agent` + `@capture.tool` + `record_model_call`: least intrusive, best for a stable agent entry point.
2. **Context managers** — `capture.agent_session` / `agent_session_async`: for inline instrumentation and multi-turn conversations where per-call values change.
3. **LangChain callback handler** — zero decorators on your code; see [LangChain integration](#langchain-integration).
4. **Provider client wrappers** — keep `@capture.agent` on the boundary and let a wrapped `openai` / `anthropic` / `google-genai` client record every model call for you; see [Provider client wrappers](#provider-client-wrappers).

### The agent boundary

`@capture.agent` marks one agent invocation = one capture file. It auto-detects `async def`:

```python
import asyncio
from evalshift import capture


@capture.tool
async def fetch_weather(city: str) -> dict:
    return {"city": city, "temp_c": 21}


@capture.tool
async def fetch_news(city: str) -> list:
    return [f"{city} headline"]


@capture.agent(suite="trip_planner", redact=True, tools=[])
async def plan_trip(city: str) -> str:
    # concurrent tools each get correct parentage — contextvars propagate into gather
    weather, news = await asyncio.gather(fetch_weather(city), fetch_news(city))
    return f"{city}: {weather['temp_c']}°C, {len(news)} headlines"


asyncio.run(plan_trip("Vilnius"))
```

The agent's **input** is derived automatically by binding the call arguments against the function signature (`{param: value}`), and hashed into the envelope's `input_hash`.

When there is no function boundary to decorate, use the context-manager form:

```python
from evalshift import capture, record_model_call

user_query = "What is my order status?"

with capture.agent_session(
    suite="support", agent_input={"query": user_query}, redact=True, tools=[]
):
    record_model_call(model_id="claude-sonnet-5", tools=[],
                      input=[{"role": "user", "content": user_query}],
                      output="Your order shipped yesterday.")
```

> **Always pass `agent_input=` to `agent_session`.** It defaults to `None`, and the dedup registry keys captures on `(suite, hash(agent_input))`. With `agent_input` left as `None` every session in the process hashes identically, and dedup (on by default) silently drops every capture after the first. Pass the real per-run input (or set `conversation_id`, which folds turn identity into the hash).

`agent_session` yields the live span tree (or `None` when capture is off / the run wasn't sampled); you normally ignore the yielded value. `capture.agent_session_async(...)` is the identical `async with` form.

### Recording model calls

The SDK never calls a model provider itself — you make the call with any client you like and record what happened.

**Completed (atomic) calls** — you already have the response:

```python
from evalshift import record_model_call

response = client.messages.create(model="claude-sonnet-5", messages=messages)  # any client

record_model_call(
    model_id="claude-sonnet-5",
    tools=tools,           # the toolset actually offered on this call -- required, see below
    input=messages,
    output=response.content[0].text,
    input_tokens=response.usage.input_tokens,
    output_tokens=response.usage.output_tokens,
    cost_usd=0.0042,
    latency_ms=850,        # pass it yourself; an atomic record has zero duration otherwise
)
```

`record_model_call` is a no-op outside an active agent session. `tools` is the only other required
field (besides `model_id`); everything else is optional. `tools` is not config you set once — it is
what the model call was actually offered, right there at the call, because a real agent can switch
toolsets between calls (one process, one suite, two toolsets, chosen by an `if`). Pass:

- the real toolset (any of the shapes below) — recorded as `tools_offered` (names) and
  `toolset_ref` (a pointer to the full schema, written once per distinct toolset);
- `[]` to assert this call genuinely had no tools — a real value, not a default;
- `None` to inherit the enclosing session's own `tools=` (`capture.agent` / `agent_session` /
  `agent_session_async`) instead of asserting one for this call — the call's own value always wins
  when it gives one.

`tools` accepts Anthropic (`{name, description, input_schema}`), OpenAI
(`{type: "function", function: {name, description, parameters}}`), or Gemini `types.Tool` shape, or
a bare list mixing any of those. A value matching none of them is left unstamped (both fields stay
`None`) rather than guessed at — logged at `debug`, never raised. Details: [docs/DECISIONS.md](docs/DECISIONS.md) D-toolset.

**Offered vs. requested vs. executed.** A `model_call` event records three different facts about
tools, and they are not interchangeable:

| what | how you record it | field |
| --- | --- | --- |
| **offered** — what the model *could* call | `tools=` on this call (above) | `tools_offered` / `toolset_ref` |
| **requested** — what the model *asked* to call, in its response | `requested_tool_calls=` (below) | `requested_tool_calls` |
| **executed** — what your app *actually ran* | `@capture.tool` on the function | the `tool_call` / `tool_result` events |

They diverge routinely — a guard rejects a requested call, a router drops it, your app pre-fetches
a tool the model never asked for, or the process dies before dispatch — and each divergence is
exactly the thing an eval wants to see, which is why all three are recorded rather than one
inferred from the others:

```python
from evalshift import record_model_call
from evalshift.capture.requested import extract_requested_tool_calls

response = client.messages.create(model="claude-sonnet-5", messages=messages, tools=tools)

record_model_call(
    model_id="claude-sonnet-5",
    tools=tools,                                              # offered
    input=messages,
    output=response.content[0].text,
    requested_tool_calls=extract_requested_tool_calls(response.model_dump()),   # requested
)
```

`extract_requested_tool_calls` is a stdlib helper that pulls the list out of a raw Anthropic /
OpenAI / Gemini response; you can also build it by hand as
`[{"name": ..., "arguments": {...}, "call_id": ...}]`. Each item is normalised to exactly those
three keys (`arguments` defaults to `{}`, `call_id` to `None`, extra provider keys are dropped).
Omitting the argument records nothing — `null`, meaning "not recorded", which is *not* the same as
`[]`, meaning "the model asked for no tools". A malformed value is dropped fail-open (logged at
`debug`), never raised. Unlike `tools=`, these arguments **are** redacted: they are payload the
model generated from user input, so they go through the same redactor as a tool call's arguments.
Details: [docs/DECISIONS.md](docs/DECISIONS.md) D-requested.

**Streaming calls** — use the recorder context manager and accumulate as chunks arrive:

```python
from evalshift import capture

with capture.model_call(model_id="claude-sonnet-5", tools=tools, input=messages) as rec:
    for chunk in stream:               # your provider's stream
        rec.add_text(chunk.text)
    rec.set_usage(input_tokens=812, output_tokens=204, cost_usd=0.0031)
```

Async variant:

```python
async with capture.model_call(model_id="claude-sonnet-5", tools=tools, input=messages) as rec:
    async for chunk in stream:
        rec.add_text(chunk.text)
```

Recorder behavior:

- Exactly one `model_call` event is recorded on exit, carrying the joined `add_text` output.
- `rec.set_requested_tool_calls([...])` records what the model asked to call (same contract as
  `record_model_call`'s argument above); call it before or during the block, last write wins.
- `latency_ms` is derived automatically from the `with`-block duration unless you pass it to `set_usage`.
- Without an active session the recorder is inert — no span, no write.
- Recorder faults never break your streaming loop (fail-open).

**The messages-list convention.** For multi-turn agents, pass the *complete per-turn context* as `input` — system prompt, prior turns, and the current user message — as role-tagged dicts:

```python
messages = [
    {"role": "system", "content": "You are a scheduling assistant."},
    {"role": "user", "content": "Can we move my appointment?"},
    {"role": "assistant", "content": "Sure — what time works?"},
    {"role": "user", "content": "1pm"},
]
```

The SDK does not validate this shape (`input` is `Any`), but following the convention makes each capture self-contained for downstream rendering and replay. Details: [docs/SCHEMA.md](docs/SCHEMA.md).

### Extracting requested tool calls from a response

Three different things get called "tools" around a model call: the ones the call was **offered**
(`tools=`), the ones the model **requested** in its response, and the ones your app actually
**executed** (`@capture.tool`). A stdlib-only helper derives the middle one from a provider
response, so you never have to reshape it by hand:

```python
from evalshift.capture.requested import extract_requested_tool_calls

response = client.messages.create(...)   # any provider

record_model_call(
    model_id="claude-sonnet-5",
    tools=tools,
    input=messages,
    requested_tool_calls=extract_requested_tool_calls(response),
)
```

It is not exported from the package root — import the full path above, like its sibling helpers in
`evalshift.capture.toolset` and `evalshift.capture.generation`.

It accepts an already-serialised response dict, an object exposing `model_dump()` / `to_dict()`, or
the provider response object itself (walked by attribute — the SDK imports no provider SDK, not
even guarded). Recognised shapes: OpenAI Chat Completions (`choices[0].message.tool_calls`, plus
the deprecated `function_call` form), OpenAI Responses (`output[*]` items of
`type: "function_call"`), Anthropic Messages (`content[*]` blocks of `type: "tool_use"`), and
Gemini (`candidates[0].content.parts[*].functionCall`, or `function_call` from the python SDK's
`to_dict()`). Only the first choice/candidate is read.

Every item is exactly `{"name": str, "arguments": dict, "call_id": str | None}`, in response order
(`call_id` is `None` where the provider has none — Gemini REST, legacy `function_call`).

**`[]` and `None` are not interchangeable.**

- `[]` — a recognised response in which the model requested no tools. A real, deliberate value:
  "the model asked for nothing."
- `None` — the value did not look like a provider response at all, or one of its tool calls had no
  usable name, in which case the whole response is refused rather than reported one call short
  (same rule as `normalize_tools`: a list that reads as complete but isn't is worse than no list).
  Nothing is asserted about what the model requested — pass it straight through rather than
  substituting `[]`.

The helper never raises. Unparseable JSON `arguments`, JSON that parses to something other than an
object, and an already-parsed `input`/`args` that is not an object each degrade to `{}` for that
one call, logged at `debug`.

### Recording tool calls

```python
from evalshift import capture


@capture.tool                      # name defaults to the function name
def lookup_customer(email: str) -> dict: ...


@capture.tool(name="search_orders")  # explicit name
def _impl(customer_id: str) -> dict: ...
```

- Works on `def` and `async def` (auto-detected). No-op when no agent session is active.
- **Arguments** are recorded by binding the call against the function signature with defaults applied; if binding fails, the fallback shape `{"args": [...], "kwargs": {...}}` is recorded instead.
- **Results** are recorded on the `tool_result` event; a raising tool records `error: str(exc)` with `result: null`, and the exception propagates to your code unchanged.
- **Nesting**: a tool called from inside another tool records the outer tool's `call_id` as its `parent_call_id`:

```python
@capture.tool
def geocode(address: str) -> tuple: ...

@capture.tool
def find_stores(address: str) -> list:
    lat, lng = geocode(address)      # recorded as a child of find_stores
    return stores_near(lat, lng)
```

### Multi-turn conversations

Schema 1.1.0 adds three optional envelope fields — `conversation_id`, `turn_index`, `parent_capture_id` — so one-capture-per-turn conversations can be linked back together downstream.

The `@capture.agent` decorator accepts all three, but decorator kwargs are **fixed at decoration time** — every call would stamp the same `turn_index`. For real conversations, open one `agent_session` per turn with fresh values:

```python
import uuid
from evalshift import capture, record_model_call

conversation_id = f"conv_{uuid.uuid4().hex}"
messages = [{"role": "system", "content": "You are a scheduling assistant."}]

for turn_index, user_text in enumerate(user_turns):
    messages.append({"role": "user", "content": user_text})
    with capture.agent_session(
        suite="scheduler",
        redact=True,
        tools=[],
        agent_input=messages,
        conversation_id=conversation_id,
        turn_index=turn_index,
    ):
        reply = run_model(messages)          # your model call
        record_model_call(model_id="claude-sonnet-5", tools=[], input=messages, output=reply)
    messages.append({"role": "assistant", "content": reply})
```

Notes:

- The SDK does not return the written `capture_id` to the caller, so `parent_capture_id` can only be set if you manage capture ids yourself (or omit it — `conversation_id` + `turn_index` are enough to reconstruct order).
- When `conversation_id` is set, turn identity is folded into `input_hash`, so short repeated turns ("yes", "1pm") don't collapse under dedup. Standalone captures (`conversation_id=None`) hash exactly as before 1.1.0.

### Error capture semantics

When the wrapped function (or session body) raises:

1. An `error` event is recorded — `message` is `str(exc)` (falling back to the exception type name when empty, e.g. `CancelledError`), `category` is the exception class name.
2. The partial capture — everything recorded up to the failure — is still written.
3. Your original exception re-raises unchanged.

---

## Configuration

### `configure()`

```python
from evalshift import configure, MemorySink

configure(
    sink=MemorySink(),            # where captures go (default: FileSink)
    sample_rate=0.25,             # capture ~25% of runs (default: capture all)
    dedup=True,                   # collapse identical-input captures (default: True)
    max_captures=200,             # newest-N cap per suite dir (default: 200)
    capture_ttl=7 * 86400,        # evict captures older than N seconds (default: off)
    require_model_call=True,      # drop captures with no model_call span (default: False)
)
```

`configure` has **merge semantics**: only the keyword arguments you pass are changed; everything else keeps its current value. Call it any time; it takes effect for subsequent captures.

`evalshift.config.reset_config()` (not a top-level export) resets everything to defaults and re-reads the environment — intended for test isolation.

### Environment variables

| Variable | Default | Meaning | When read |
|---|---|---|---|
| `EVALSHIFT_CAPTURE` | unset (off) | Master gate. Truthy values: `1`, `true`, `yes`, `on` (case-insensitive). Anything else is off. | **Live, on every call** — can be toggled mid-process |
| `EVALSHIFT_DIR` | `.evalshift` | Capture root directory. Relative paths resolve against the **current working directory** (no repo-root walk). | **Live, at each write** |
| `EVALSHIFT_MAX_CAPTURES` | `200` | Keep only the newest N capture files per suite directory. | At config construction (import or `reset_config()`) |
| `EVALSHIFT_CAPTURE_TTL` | off | Evict capture files older than N seconds. | At config construction |
| `EVALSHIFT_DEDUP` | `on` | Collapse captures with identical `(suite, input_hash)` within one process. | At config construction |
| `EVALSHIFT_SAMPLE_RATE` | off (capture all) | Capture only this fraction of runs (0.0–1.0), decided once per agent invocation. | At config construction |
| `EVALSHIFT_SINK` | unset (local disk) | Ship captures to an object store you own: `s3://<bucket>/<prefix>`, `gs://<bucket>/<prefix>` or `az://<account>/<container>/<prefix>`. Needs the provider's client library (`pip install boto3` / `google-cloud-storage` / `azure-storage-blob azure-identity`). An unusable value — library missing, malformed URI — is recorded here and raised as `SinkConfigurationError` by the first `capture.*` decorator, client wrapper, LangChain handler or `configure()` call while `EVALSHIFT_CAPTURE` is on; the message names the variable, the scheme and the fix, never the value. With capture off nothing happens. | At config construction |

- For the numeric knobs, `0`, `none`, `unlimited`, or `off` means "no cap / disabled". Disable
  `EVALSHIFT_DEDUP` with `off` (or `0`/`none`); `false`/`no` are not recognised and leave dedup on.
- `EVALSHIFT_SAMPLE_RATE=0` means sampling off (capture every run), whereas
  `configure(sample_rate=0.0)` captures nothing; to capture nothing via the env var, unset
  `EVALSHIFT_CAPTURE` instead.
- **Precedence:** explicit `configure(...)` > environment variable > built-in default.
- Malformed values for the hygiene knobs fail open to the default; `EVALSHIFT_SINK` is the
  exception — an unusable value raises `SinkConfigurationError` at startup while capture is on
  (row above).

### Keeping `captures/` bounded

By default a long-running host cannot grow `captures/` without limit; three mechanisms compose:

- **Dedup** (on by default): a per-process registry keyed on `(suite, input_hash)` drops repeat captures of the same input. The registry resets on process restart.
- **GC** (200 files per suite by default): after each successful disk write, the suite directory is capped to the newest `max_captures` files (ordered by filesystem mtime) and files older than `capture_ttl` seconds are evicted. GC never recurses into subdirectories and never raises.
- **Sampling** (off by default): with `sample_rate=0.1`, ~10% of agent invocations are captured; unsampled runs skip all bookkeeping entirely. A fault in the sampling draw fails open to *capturing*.

To restore unbounded write-everything behavior:

```bash
EVALSHIFT_MAX_CAPTURES=0 EVALSHIFT_DEDUP=off
```

**With an object store** (`EVALSHIFT_SINK`), dedup still applies but `max_captures` and `capture_ttl` do not — GC only ever runs after a real disk write. Bound the bucket with a lifecycle rule instead (e.g. expire objects under `<prefix>/captures/` after 30 days). Dedup is per process, so several hosts writing the same input each store a capture; `evalshift capture sync` drops content duplicates when it promotes, so the bucket may be noisy while the suite is not.

**`require_model_call`** (off by default) is a persistence gate for eval-grade capture: a capture with no `model_call` event carries no scoreable ground truth, so hosts that only want promotable captures enable `configure(require_model_call=True)` and content-free captures are silently dropped (one debug log line).

---

## Sinks

A sink decides where a finished capture envelope goes. The protocol is a single method:

```python
class Sink(Protocol):
    def write(self, envelope: CaptureEnvelope) -> Path | None: ...
```

### `FileSink` (default)

Writes `<base>/captures/<suite>/<capture_id>.json` as UTF-8 JSON.

- **Base resolution, at write time:** constructor argument > `EVALSHIFT_DIR` env var > `.evalshift` relative to the current working directory. There is deliberately no repo-root walk — the SDK must not assume it runs inside a checkout (containers, Lambda).
- Returns the **absolute** `Path` written, or `None` when the write degrades on a filesystem `OSError` (read-only mount, disk full, permission denied) — the capture is dropped, the host never crashes.
- The `suite` name is sanitized into a single safe path segment (separators and `..` replaced), so a suite name can never escape `<base>/captures/`.

```python
from evalshift import FileSink, configure

configure(sink=FileSink(base="/var/log/evalshift"))
```

### `MemorySink`

Buffers envelopes in process memory; nothing touches disk. The right choice for read-only filesystems (AWS Lambda) and tests. Thread-safe.

```python
from evalshift import MemorySink, configure

sink = MemorySink()
configure(sink=sink)

run_agent()

for env in sink.flush():        # drains and clears the buffer
    upload_somewhere(env)

sink.captures                   # non-draining snapshot (tuple), for inspection
```

**Toolset sidecars are the one exception to "nothing touches disk."** A `model_call`'s
`toolset_ref` points at a content-addressed sidecar (`<base>/toolsets/<hex>.json`) written by
`ToolsetSink`, which is unconditionally file-based — there is no in-memory toolset store. With
`MemorySink` configured, the capture envelope itself stays safely in memory, but the sidecar
write still goes to disk, resolved the same way `FileSink`'s own default is (`EVALSHIFT_DIR`,
else `.evalshift` relative to the CWD). On a genuinely read-only filesystem with no writable
mount at all, every sidecar write fails — silently, fail-open — and every capture keeps its
`toolset_ref` unstamped, which the CLI refuses to promote. Point `EVALSHIFT_DIR` at a writable
mount (e.g. `/tmp`) even when you use `MemorySink` for the envelopes themselves; see
[Troubleshooting](#no-capture-file-was-written) and the read-only-filesystem FAQ entry below.
(An [`ObjectStoreSink`](#objectstoresink-and-cloud-stores) sends the sidecar to its store instead and touches no disk at all.)

### `ObjectStoreSink` and cloud stores

For hosts whose disk does not outlive them — Fargate tasks, Lambda, Kubernetes pods — ship captures to an object store you own. Set one env var next to `EVALSHIFT_CAPTURE=1`:

```bash
EVALSHIFT_CAPTURE=1 EVALSHIFT_SINK=s3://acme-evals/support-agent python agent.py
```

| URI form | Store | Install | Credentials (default chain) |
|---|---|---|---|
| `s3://<bucket>/<prefix>` | Amazon S3; also MinIO, Cloudflare R2, Backblaze B2 via boto3's `AWS_ENDPOINT_URL` | `pip install boto3` | IAM task role / instance profile; `aws sso login` locally |
| `gs://<bucket>/<prefix>` | Google Cloud Storage | `pip install google-cloud-storage` | Workload Identity / Cloud Run service account; `gcloud auth application-default login` locally |
| `az://<account>/<container>/<prefix>` | Azure Blob Storage (`https://<account>.blob.core.windows.net`) | `pip install azure-storage-blob azure-identity` | Managed Identity; `az login` locally |

`<prefix>` is optional. Credentials never go in the URI — a URI containing `@` or `?` is rejected, and the error does not echo it back. If `EVALSHIFT_SINK` is invalid, its library is missing, or the store cannot be opened for any other reason, the SDK records the problem at import and raises it as `SinkConfigurationError` the first time your code asks it to capture — applying a `capture.*` decorator, wrapping a client, building the LangChain handler or calling `configure()` without a `sink` — while `EVALSHIFT_CAPTURE` is on. In a real agent that is process start. Bare `import evalshift` never raises, and with capture off nothing happens. Should the gate turn on only after those ran, captures are dropped with one `WARNING` rather than written to local disk: a store you asked for is never silently replaced. The message names the variable, the scheme and the package to install (`Run: pip install boto3`), never the value.

**The keys are the local layout.** Under your prefix the sink writes exactly what `FileSink` and `ToolsetSink` write to disk — `captures/<suite>/cap_<hex>.json` and `toolsets/<hex>.json` — so the CLI can mirror the bucket into `.evalshift/` and read it unchanged (`evalshift capture sync` does this automatically once `captures.store` is set in `evalshift.yaml`; see the CLI docs). Until your CLI version supports `captures.store`, `aws s3 sync` / `gcloud storage rsync` / `azcopy sync` into a local `.evalshift/` followed by `evalshift capture sync` works the same way.

**Toolset sidecars go to the same store**, once per process per toolset. If a background sidecar upload fails, the sink forgets that toolset so the next model call offering it uploads the sidecar again; captures stamped in between point at a missing sidecar, and the CLI refuses to promote them.

**Background, bounded, fail-open.** `write` enqueues and returns; one daemon thread uploads. The queue holds 1000 items by default and drops the newest when full (one debug line). A failing upload never raises into your agent: the first failure per sink logs a `WARNING` with the store URI, later ones `debug`. There is no retry layer of its own — boto3, the GCS client and the Azure client already retry.

**Shutdown.** The sink registers a flush with `atexit` the first time it uploads in the background. It waits up to `flush_timeout` (10 s by default); if uploads are still queued when it gives up, it logs one `WARNING` naming the store and how many items were dropped. Python's default `SIGTERM` handling does *not* run `atexit`. On ECS/Fargate, Kubernetes or anything that stops you by signal, do one of:

```python
import signal, sys
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # SystemExit → atexit → flush
```

or call `evalshift.flush_captures(timeout=10)` from your own shutdown hook — not from inside the signal handler itself, where it can deadlock on the sink's lock. On **Lambda**, background threads freeze between invocations: call `flush_captures()` before the handler returns, or configure the sink inline:

```python
from evalshift import ObjectStoreSink, configure
from evalshift.stores.s3 import S3Store

configure(sink=ObjectStoreSink(S3Store("acme-evals", "support-agent"), background=False))
```

Programmatic construction accepts any `ObjectStore` — an object with `put(key: str, data: bytes) -> None` — so a store the SDK does not ship is a few lines. `evalshift.stores.MemoryStore` is the in-memory one the tests use.

### Custom sinks

Anything with a `write(envelope) -> Path | None` method works:

```python
import queue
from evalshift import configure

class QueueSink:
    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()

    def write(self, envelope):
        self.q.put(envelope)
        return None

configure(sink=QueueSink())
```

### Hygiene wrapping

Whenever any hygiene knob is active (dedup / max_captures / capture_ttl — and dedup + max_captures are on by default), the active sink is transparently wrapped in an internal `HygieneSink` that applies dedup before the base write and GC after it. Dedup works with any sink (including `MemorySink`); GC runs only when the base write returns a real `Path`, so it is effectively a `FileSink`-only concern (an `ObjectStoreSink` returns `None` and is bounded by bucket lifecycle rules instead). With all three knobs disabled, your sink is used bare.

A dedup-suppressed write returns `None` silently (no log line); a degraded `OSError` write logs one `debug` line.

---

## Redaction

Captures record the *inside* of an agent run — tool arguments, results, model inputs and outputs — which routinely contains PII. Redaction masks payload values **in process, before any byte hits disk**. Full boundary spec: [docs/REDACTION.md](docs/REDACTION.md).

- **Required, not optional.** `redact=` is a mandatory keyword on `@capture.agent`, `capture.agent_session`, `capture.agent_session_async`, and `EvalShiftCallbackHandler`. There is no default and no process-wide setter — masking is decided, and reviewable, at each capture point.
- **Three answers:** `True` masks with `default_redactor`; `False` captures verbatim on purpose; a `(value) -> redacted_value` callable is your own redactor. Anything else, `None` included, raises `TypeError` at the capture point whether or not the gate is on.
- **Fail-closed:** if your redactor raises, the whole capture is dropped — never written half-masked. Your agent is unaffected.
- Because redaction runs before serialization, masked values flow into both the trace events *and* the derived tool `input_hash` — the capture stays internally consistent.

```python
from evalshift import capture

@capture.agent(suite="support", redact=True, tools=[])      # mask emails + API keys
def handle_ticket(query: str) -> str: ...

@capture.agent(suite="fixtures", redact=False, tools=[])    # verbatim, on purpose
def replay_fixture(case: dict) -> str: ...
```

`default_redactor` recursively walks strings, dicts, lists, and tuples (returning copies, never mutating) and masks:

| Pattern | Replacement |
|---|---|
| Email addresses | `[REDACTED_EMAIL]` |
| OpenAI-style keys (`sk-` + 16+ chars) | `[REDACTED_KEY]` |
| AWS access keys (`AKIA` + 16 chars) | `[REDACTED_KEY]` |
| `Bearer <token>` headers | `Bearer [REDACTED_KEY]` |

Those four patterns are all `redact=True` covers. Structured secrets — SSNs, account numbers, internal id formats — need a callable of your own:

```python
def scrub(value):
    if isinstance(value, dict):
        return {k: ("***" if k == "ssn" else scrub(v)) for k, v in value.items()}
    if isinstance(value, str):
        return value.replace(SECRET, "[REDACTED]")
    return value

@capture.agent(suite="clinical", redact=scrub, tools=[])
def handle_case(record: dict) -> str: ...
```

Fields passed to the redactor, per span kind: `tool` → `arguments`, `result`, `error`; `model_call` → `input`, `output`, `requested_tool_calls`; `retrieval` → `query`, `documents`; `guardrail` → `reason`; `final_output` → `text`; `error` → `message`. `requested_tool_calls` is in that list because a requested call's arguments are payload the model generated from user input, as sensitive as a tool call's own (D-requested). `model_call`'s `toolset_ref` / `tools_offered` are deliberately **not** in that list — they are config, not payload, exactly like `generation_config` (which lives outside `span.data` entirely); see [docs/DECISIONS.md](docs/DECISIONS.md) D-toolset for why the two fields are safe from redaction by construction rather than by an explicit skip.

**What a written capture may still contain.** Redaction masks payload values only. It does not scrub structural metadata (tool names, `model_id`, token counts, timestamps, call ids, the `metadata["evalshift"]` block), the envelope (`capture_id`, `suite`, `code_version`, `input_hash` — a one-way SHA-256; the raw input is never stored at the envelope level), or anything your redactor's patterns miss. `default_redactor` is deliberately conservative and is **not** a comprehensive PII scrubber — supply a domain-specific redactor when your data has structured secrets.

---

## Async and concurrency

- `@capture.agent` and `@capture.tool` auto-detect `async def` and wrap accordingly.
- Session state lives in `contextvars`, which propagate across `await` and are copied into `asyncio.gather` child tasks — concurrent tool calls each record correct parentage.
- Threaded tools (`asyncio.to_thread`, `run_in_executor`) are safe: the span tree and `MemorySink` are lock-guarded.
- Event ordering stays deterministic under concurrency: events sort by `(timestamp, monotonic op-order)` into a dense `sequence_index`.

---

## LangChain integration

Requires the extra: `pip install "evalshift-sdk[langchain]"`.

`EvalShiftCallbackHandler` records an entire chain/agent run — model calls, tool calls, retriever calls, final output — with zero decorators on your code. Drop it into any `callbacks=[...]` list:

```python
from evalshift.adapters.langchain import EvalShiftCallbackHandler

handler = EvalShiftCallbackHandler(suite="rag_agent", redact=True, code_version="abc1234", tools=bound_tools)

chain.invoke({"question": "What is our refund policy?"}, config={"callbacks": [handler]})
```

Behavior:

- The gate (`EVALSHIFT_CAPTURE`), sampling, dedup, GC, and `configure(...)` all apply exactly as with manual instrumentation. Each root run makes its own gate + sampling decision.
- One handler instance is reusable across many invocations and across threads (per-root-run state, lock-guarded).
- Constructor is keyword-only: `EvalShiftCallbackHandler(*, suite, redact, tools, code_version="")`. `redact` is **required**, with the same `True` / `False` / callable contract as `@capture.agent`. `tools` is also **required** — the toolset this chain was offered (`[]` if it never binds tools) — but unlike `redact` it has no per-call override surface: LangChain callbacks carry no user-supplied `tools` kwarg, so the handler resolves its one value once at construction and stamps it onto every `model_call` span it ever opens, for the handler's whole lifetime. It does **not** accept conversation-identity kwargs.
- A raising redactor drops the capture (fail-closed), chain unaffected.
- Framework payloads are coerced to JSON-able primitives before recording, so a non-serializable LangChain object can't silently break the capture write.
- Retriever calls are recorded as `retrieval` events and the chain's final output as a `final_output` event — event kinds the manual API does not emit.
- `requested_tool_calls` is captured automatically, with no extra wiring: `on_llm_end` reads the response's `AIMessage.tool_calls` — LangChain has already normalised it across providers — and maps `args` → `arguments`, `id` → `call_id` through the same normaliser [`record_model_call`](#record_model_call) uses. A chat model that asked for nothing records `[]`; a plain text (non-chat) completion, which has no message and so cannot ask, records nothing (`null`). `invalid_tool_calls` are deliberately excluded: those are calls whose arguments failed to parse, not requests your app could have dispatched. Streaming needs no special case — the aggregated message reaches `on_llm_end` with its tool calls intact. See [Offered vs. requested vs. executed](#recording-model-calls).
- **Do not mix** the handler with `@capture.tool`-decorated code on the same call path. The handler deliberately keeps its own run-id-based span bookkeeping (LangChain callbacks fire flat with `run_id`/`parent_run_id`, not nested on the stack) and does not bind the contextvar session — mixing risks double-recording. Use one or the other.

---

## Provider client wrappers

Requires the matching extra: `pip install "evalshift-sdk[openai]"`, `"[anthropic]"`, or `"[google-genai]"`.

If your agent calls a provider SDK directly, you do not have to write `record_model_call` by hand. Wrap the client instance once; every intercepted call made inside an active capture session (`@capture.agent`, `agent_session`, ...) records one `model_call` with `model_id`, the tools offered, the tool calls the model requested, `input`, `output`, token usage, latency and the allow-listed generation settings. Outside a session the wrapper is inert.

```python
from openai import OpenAI
from evalshift import capture
from evalshift.adapters.openai import wrap_openai

client = wrap_openai(OpenAI())          # or AsyncOpenAI(); use the proxy exactly like the client

@capture.agent(suite="support_agent", redact=True, tools=[])
def handle_ticket(query: str) -> str:
    r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": query}])
    return r.choices[0].message.content or ""
```

```python
from anthropic import Anthropic
from evalshift.adapters.anthropic import wrap_anthropic

client = wrap_anthropic(Anthropic())    # or AsyncAnthropic()
```

```python
from google import genai
from evalshift.adapters.genai import wrap_genai

client = wrap_genai(genai.Client())     # sync and client.aio both covered
```

What is intercepted (everything else is forwarded untouched and not recorded):

| Wrapper | Intercepted | Not recorded (still works) |
| --- | --- | --- |
| `wrap_openai` | `chat.completions.create`, `responses.create` — sync, async, `stream=True` | `parse` / `beta`, `with_raw_response`, the `.stream()` helper context managers, embeddings, audio, images, files |
| `wrap_anthropic` | `messages.create` (sync, async, `stream=True`), `messages.stream` (sync and async managers) | `beta`, `messages.batches`, `messages.count_tokens`, `with_raw_response` |
| `wrap_genai` | `models.generate_content`, `models.generate_content_stream`, and both under `client.aio` | `chats`, `embed_content`, `count_tokens`, `files`, `caches`, `batches`, `tunings`, `live` |

Behaviour, shared by all three (design: [docs/DECISIONS.md](docs/DECISIONS.md) D-wrappers):

- **Wraps the instance, never the module.** Nothing is monkeypatched; a client you did not wrap is untouched. The proxy is not an `isinstance` of the client's class — `evalshift.adapters._wrap.unwrap(proxy)` returns the real client if you need one.
- **Record-only.** The wrapper opens no session and takes no `suite` / `redact`; the boundary and the masking choice stay on `@capture.agent`. A wrapped client can be shared between captured and uncaptured code paths.
- **`tools` is asserted per call.** The call's `tools` kwarg (Gemini: `config.tools`) is recorded, or `[]` when the request carried none — the session's `tools=` is never inherited, because a wrapper knows exactly what the provider was sent. Callables passed to Gemini for automatic function calling are declared through the SDK's own converter; a built-in-only tool such as `google_search` normalises to nothing and leaves the toolset unstamped.
- **Fail-open, and the real call is never guarded.** Provider exceptions propagate exactly as before; a wrapper fault means "this call was not recorded", never a broken client. A request that raised records nothing.
- **Streaming: the returned stream is a proxy** that forwards every attribute and records once when the stream is exhausted, closed, or fails — with whatever output had arrived. Usage comes from the final chunk when the provider sends one (OpenAI chat streams need `stream_options={"include_usage": True}`, otherwise tokens stay 0). Anthropic's `messages.stream` manager records on `__exit__` from `get_final_message()`. A stream simply abandoned records nothing.
- **`input` is always a messages-style list**, so the CLI recovers system prompt, history and current turn the same way for every provider: Anthropic's `system`, the Responses API's `instructions` and Gemini's `system_instruction` become a leading `{"role": "system"}` message; Gemini `Content`/`Part` values are folded into role-tagged messages, function calls into `tool_calls` and function responses into `tool` messages. Server-held state (OpenAI `previous_response_id` / `conversation`) is not expanded.
- **`cost_usd` stays 0.** The CLI prices tokens from litellm's table at promote time (`cost_source: "estimated"`); a model with no price entry (local / self-hosted) legitimately stays at 0.
- **Open-source models need no wrapper of their own.** DeepSeek, Ollama, vLLM, llama.cpp server, LM Studio, TGI, Together, Groq, Fireworks and OpenRouter serve OpenAI-compatible endpoints: `wrap_openai(OpenAI(base_url=...))` covers them unchanged; `model_id` is whatever string you passed, and a server that omits `usage` records zero tokens. Native non-OpenAI clients (the `ollama` package, in-process transformers) keep using [`record_model_call`](#record_model_call) / [`capture.model_call`](#capturemodel_call). For DeepSeek, `wrap_openai(OpenAI(base_url="https://api.deepseek.com", api_key=os.environ["DEEPSEEK_API_KEY"]))` records `model_id` as the bare id you pass (`deepseek-flash`); the CLI (1.2.0+) resolves it to `deepseek/deepseek-flash` on replay.
- **Mixing with the manual API is fine** — a wrapped client inside `@capture.agent` alongside `@capture.tool` is the intended pairing. Do not also call `record_model_call` for the same request, or it is recorded twice.

---

## Reading captures programmatically

The read side parses a capture file and **upgrades it on read** to the current schema version, so older captures of the **same major** stay readable under newer SDKs.

```python
from pathlib import Path
from evalshift import load_capture, load_envelope

raw = Path(".evalshift/captures/support_demo/cap_abc.json").read_text()

data = load_capture(raw)         # dict, migrated to the current schema version
env = load_envelope(raw)         # typed CaptureEnvelope dataclass

for event in env.trace.events:
    print(type(event).__name__, event.sequence_index)
```

Unlike the capture path, the read path **raises** — it does not fail open:

| Error (all subclass `MigrationError`) | Raised when |
|---|---|
| `UnreadableCaptureError` | Not valid UTF-8/JSON, not a JSON object, or an unparseable timestamp |
| `MissingSchemaVersionError` | No `schema_version` key and no `default_version=` supplied |
| `InvalidSchemaVersionError` | `schema_version` is not a `MAJOR.MINOR.PATCH` string |
| `UnsupportedSchemaVersionError` | The capture's major version is newer than this SDK supports |
| `NoMigrationPathError` | No registered migration chain reaches the target version |
| `ObsoleteSchemaVersionError` | The capture's major version is older than this SDK supports and no registered migration bridges it (every 1.x capture). Re-run the agent to re-capture. |
| `UnknownEventTypeError` | An event's `type` is not a known discriminator |

Catch broadly with `except MigrationError`.

Forward compatibility: an **older, same-major** capture migrates up the registered chain (e.g. 2.0.0 → 2.1.0); an **older major** with no registered bridge (every 1.x capture) is refused with `ObsoleteSchemaVersionError`; the **same** version reads as-is; a **newer minor/patch** (same major) reads best-effort with a warning, unknown fields dropped; a **newer major** is refused with `UnsupportedSchemaVersionError`.

Extenders can plug in a future upgrade step:

```python
from evalshift import register_migration

register_migration("2.1.0", "2.2.0", my_upgrade_fn, description="add foo field")
```

`SCHEMA_VERSION` (currently `"2.1.0"`) is the version this SDK writes. Policy details: [docs/SCHEMA.md](docs/SCHEMA.md).

---

## API reference

Everything below except `reset_config` (`evalshift.config`), the `Sink` protocol (`evalshift.sinks`), `CaptureEnvelope` (`evalshift.trace`), the object stores (`evalshift.stores`), `EvalShiftCallbackHandler` and the `wrap_*` wrappers (import lines shown) imports from the top level: `from evalshift import ...`.

Top-level exports: `capture`, `record_model_call`, `configure`, `flush_captures`, `Redactor`, `RedactSetting`, `default_redactor`, `FileSink`, `MemorySink`, `ObjectStoreSink`, `SinkConfigurationError`, `load_capture`, `load_envelope`, `register_migration`, `MigrationError`, `SCHEMA_VERSION`, `__version__`.

`evalshift.trace` / `evalshift.capture` also export lower-level building blocks used by the SDK's own tests; not a stable public API.

### `capture.agent`

```python
@capture.agent(
    *,
    suite: str,
    redact: RedactSetting,          # required — True | False | Redactor
    tools: Any,                     # required — this session's toolset; see record_model_call
    code_version: str = "",
    conversation_id: str | None = None,
    turn_index: int | None = None,
    parent_capture_id: str | None = None,
)
```

Decorator capturing one agent invocation per call. Works on `def` and `async def` (auto-detected). No-op unless the `EVALSHIFT_CAPTURE` gate is on. Agent input is derived by binding call arguments to the signature. Conversation kwargs are static per decoration — use `agent_session` for per-turn values. `tools` sets this session's toolset — inherited by any `record_model_call` / `capture.model_call` inside the decorated function that passes its own `tools=None`; a call's own value always overrides it (D-toolset).

### `capture.agent_session` / `capture.agent_session_async`

```python
with capture.agent_session(
    *,
    suite: str,
    redact: RedactSetting,          # required — True | False | Redactor
    tools: Any,                     # required — this session's toolset; see record_model_call
    code_version: str = "",
    agent_input: Any = None,
    conversation_id: str | None = None,
    turn_index: int | None = None,
    parent_capture_id: str | None = None,
) as tree:  # SpanTree | None
    ...
```

Context-manager form of `capture.agent` for inline instrumentation; `agent_session_async` is the identical `async with` form. Yields the live `SpanTree`, or `None` when capture is off or the run wasn't sampled. **Always pass `agent_input`** — it feeds `input_hash`, the dedup key. The recommended primitive for multi-turn conversations (fresh `turn_index` per `with`). `tools` behaves exactly as on `capture.agent`.

### `capture.model_call`

```python
rec = capture.model_call(*, model_id: str, tools: Any, input: Any = None,
                         generation_config: dict[str, Any] | None = None)
# usable as `with rec:` or `async with rec:`

rec.add_text(text: str) -> None
rec.set_usage(*, input_tokens: int = 0, output_tokens: int = 0,
              cost_usd: float = 0.0, latency_ms: int | None = None) -> None
rec.set_generation_config(config: dict[str, Any]) -> None
rec.set_requested_tool_calls(calls: Any) -> None
```

Streaming model-call recorder. Records exactly one `model_call` event on exit with the joined `add_text` chunks as output. `latency_ms` auto-derives from the block duration unless set via `set_usage`. Inert without an active session — no toolset resolution or sidecar write happens for a no-op either; recorder faults never break the stream loop. `tools` is required, with the same contract as [`record_model_call`](#record_model_call)'s. `set_requested_tool_calls` records what the model asked to call, also with the same contract as `record_model_call`'s argument — usable before or during the block (a streamed tool call is only complete once its argument deltas have arrived), last write wins. `generation_config` is allow-listed and JSON-coerced exactly as in `record_model_call`; use `set_generation_config` (last write wins, filtered on the same ten keys) when the effective settings only become known mid-stream.

### `capture.tool`

```python
@capture.tool
def my_tool(...): ...

@capture.tool(name="explicit_name")
def my_tool(...): ...
```

Records a tool span (a `tool_call` + `tool_result` event pair). Name defaults to `__name__`. Works on `def` and `async def`. No-op when no agent session is active. Arguments recorded via signature binding (fallback `{"args": [...], "kwargs": {...}}`); a raising tool records `error=str(exc)` and re-raises.

### `record_model_call`

```python
record_model_call(
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
) -> None
```

Records an already-complete model call into the active session. No-op outside one. Latency is `0` unless passed explicitly (the event has no duration).

`tools` is **required** (D-toolset) — the toolset this call was actually offered, in Anthropic (`{name, description, input_schema}`), OpenAI (`{type: "function", function: {name, description, parameters}}`), or Gemini `types.Tool` shape (`evalshift.capture.toolset.normalize_tools`), or a bare list mixing any of those. Forgetting it is a `TypeError` at the call site — the parameter has no default, on purpose, so an un-instrumented call site can never silently record "no tools" by omission. Pass:

- the real toolset — recorded as `tools_offered` (a name-only list, always stamped once the effective value normalises) and `toolset_ref` (a content-addressed pointer to the full schema, written once per distinct toolset by `ToolsetSink` and stamped only if that write succeeds);
- `tools=[]` to assert this call genuinely had no tools — a real, first-class value, not a default;
- `tools=None` to defer to the enclosing session's own `tools=` (`capture.agent` / `agent_session` / `agent_session_async`) instead of asserting one for this call. A call's own non-`None` value always wins over the session's, even across repeated calls in one session that each choose differently — the reason this is per-call at all is that a real agent can switch toolsets mid-run.

A value matching no recognised shape — the call's own, or (when `tools=None`) the session's — normalises to `None`: neither field is stamped (logged at `debug`), leaving the capture structurally invalid for that event rather than guessing. Unlike `generation_config`, toolsets are **not** allow-listed — an `input_schema` is arbitrary user JSON needed in full to dispatch, so normalisation only recognises or rejects tool *shapes*, never prunes keys within a schema. The canonical shape a toolset is normalised *to* is `{name, description, input_schema}` plus one optional key: `strict: true`, carried through from OpenAI's `function.strict` or Anthropic's top-level `strict` when it is truthy and omitted entirely otherwise (so pre-existing fingerprints are unchanged). It is the one function-envelope key kept, because a replay that drops it runs the target under a weaker schema constraint than the source did. Like `generation_config`, toolsets are config, not payload, and are never redacted — but by a different mechanism: `generation_config` lives outside `span.data` entirely, while the toolset fields are top-level `span.data` fields kept safe only because `model_call`'s redactable-field list names its fields one by one (`input`, `output`, `requested_tool_calls`) and neither toolset field is among them. See [docs/DECISIONS.md](docs/DECISIONS.md) D-toolset for the full reasoning.

`requested_tool_calls` (schema 2.1.0, D-requested) records what the **model asked for** in this response — a third fact alongside `tools` (what it was *offered*) and the `tool_call` events (what your app *executed*); see [Offered vs. requested vs. executed](#recording-model-calls) above. Pass a list of `{"name": str, "arguments": dict, "call_id": str | None}` items, typically from `evalshift.capture.requested.extract_requested_tool_calls(response_dict)`. Each item is normalised to exactly those three keys (extra provider keys dropped, `arguments` → `{}`, `call_id` → `None`) because the CLI's `RequestedToolCall` model is `extra="forbid"`. Unlike `tools`, it is **optional**: omitted or `None` records nothing (`null` = "not recorded", distinct from `[]` = "the model requested no tools"). A malformed value — not a list, or a list with no item carrying a usable `name` — is dropped fail-open and logged at `debug`; the event is still recorded. Unlike `tools`, these arguments **are** redacted.

`generation_config` records the call's generation settings under the event's `metadata["generation_config"]`, so `evalshift capture sync` can replay promoted cases with the same settings (structured-output schemas no longer need hand-mirroring into the CLI config). Exactly ten keys are recorded — `temperature`, `top_p`, `response_mime_type`, `response_schema`, `response_format`, `max_output_tokens`, `max_tokens`, `tool_choice`, `parallel_tool_calls`, and `tool_config` (Gemini's spelling of `tool_choice`) — and every other key is dropped, silently apart from a debug log. The allow-list is not tidiness: metadata is config, not payload, so the redactor never walks it, and an unlisted key — `system_instruction` above all, or `safety_settings` — would land in the capture unmasked. The three tool-use keys are the ones a replay needs in order to run the target under the same constraint the source ran under; `parallel_tool_calls: false` survives intact (the filter is `is not None`, never truthiness). Values are coerced to JSON on the way in: primitives, dicts and lists pass through, an object with a `model_dump` method (a `google.genai.types.ToolConfig`, say — duck-typed, never imported) is dumped to a dict, and anything else (a Pydantic `response_schema` class, say) is stored as its `str()` form, so a non-serialisable setting can no longer take the whole capture down at write time. A non-dict `generation_config` is dropped fail-open, as is one the allow-list empties — neither writes the key at all. The LangChain adapter applies the same allow-list to `invocation_params`.

### `configure`

```python
configure(
    *,
    sink: Sink | None = ...,
    sample_rate: float | None = ...,
    dedup: bool = ...,
    max_captures: int | None = ...,
    capture_ttl: float | None = ...,
    require_model_call: bool = ...,
) -> None
```

Sets process-wide options with merge semantics — only the arguments you pass change. See [Configuration](#configuration) for each knob. `None` means "disabled/unset" for `sink`, `sample_rate`, `max_captures`, `capture_ttl`. There is no `redact` knob — masking is required per capture point (see [Redaction](#redaction)).

`sink` defaults from `EVALSHIFT_SINK` when that is set.

### `evalshift.config.reset_config`

```python
from evalshift.config import reset_config
reset_config()
```

Resets all configuration to defaults, re-reading hygiene env vars, and clears the dedup registry. Test-isolation utility; not a top-level export.

### `Redactor` (protocol)

```python
@runtime_checkable
class Redactor(Protocol):
    def __call__(self, value: Any) -> Any: ...
```

### `RedactSetting`

```python
RedactSetting = Redactor | bool
```

The type of the required `redact=` keyword. `True` → `default_redactor`; `False` → verbatim; a callable → itself. Any other value raises `TypeError` at the capture point.

### `default_redactor`

```python
default_redactor(value: Any) -> Any
```

Recursively masks emails (`[REDACTED_EMAIL]`), `sk-…` keys, `AKIA…` keys, and `Bearer` tokens (`[REDACTED_KEY]`) in strings; walks dict/list/tuple element-wise; returns copies, never mutates; passes other types through unchanged.

### `Sink` (protocol)

```python
from evalshift.sinks import Sink
from evalshift.trace import CaptureEnvelope

@runtime_checkable
class Sink(Protocol):
    def write(self, envelope: CaptureEnvelope) -> Path | None: ...
```

### `FileSink`

```python
FileSink(base: str | os.PathLike[str] | None = None)
FileSink.write(envelope) -> Path | None
```

Writes `<base>/captures/<suite>/<capture_id>.json`. Base resolution at write time: constructor arg > `EVALSHIFT_DIR` > CWD-relative `.evalshift`. Returns the absolute path, or `None` on `OSError` (dropped, logged at debug). Suite segment sanitized against path traversal.

### `MemorySink`

```python
MemorySink()
MemorySink.write(envelope) -> None
MemorySink.flush() -> list[CaptureEnvelope]   # drains and clears
MemorySink.captures -> tuple[CaptureEnvelope, ...]  # non-draining snapshot
```

In-memory buffer for read-only filesystems and tests. Thread-safe.

### `ObjectStoreSink`

```python
from evalshift import ObjectStoreSink
from evalshift.stores import ObjectStore, MemoryStore, parse_store_uri, open_store

ObjectStoreSink(store: ObjectStore, *, background: bool = True, queue_size: int = 1000, flush_timeout: float = 10.0)
ObjectStoreSink.write(envelope) -> None
ObjectStoreSink.write_toolset(normalized, fingerprint) -> str | None
ObjectStoreSink.flush(timeout: float | None = None) -> bool
ObjectStoreSink.store -> ObjectStore
```

Stores: `evalshift.stores.s3.S3Store(bucket, prefix="", *, client=None)`, `evalshift.stores.gcs.GCSStore(bucket, prefix="", *, client=None)`, `evalshift.stores.azure.AzureBlobStore(account, container, prefix="", *, client=None)`; each has `put` and a `uri` attribute and builds its client on first use (and, when constructed without a `client`, raises `MissingStoreDependencyError` at construction if its library is absent). `open_store(uri) -> ObjectStore` dispatches on the scheme and raises `MissingStoreDependencyError` (an `ImportError` naming the package to install) when a module the scheme needs is absent (for `az://`, `azure-storage-blob` or `azure-identity`); `parse_store_uri(uri) -> StoreURI` validates the grammar alone.

`evalshift.SinkConfigurationError` (`RuntimeError`) — raised by `evalshift.config.require_sink_ready()`, which every touch point calls; `evalshift.stores.uri.MissingStoreDependencyError` (`ImportError`; `.scheme`, `.module`, `.package`, `.packages`, `.extra`) — raised by `open_store()`, `require_store_modules(scheme)` and a store constructed without a `client` when its library is absent.

### `flush_captures`

```python
flush_captures(timeout: float | None = None) -> bool
```

Waits for a background `ObjectStoreSink` to finish uploading; returns `True` immediately for any other sink. For shutdown hooks and Lambda. Do not call it from inside a signal handler — it can deadlock on the sink's lock; for `SIGTERM`, have the handler call `sys.exit(0)` so the `atexit` flush runs outside it.

### `load_capture`

```python
load_capture(raw: str | bytes, *, target: str | None = None,
             default_version: str | None = None) -> dict[str, Any]
```

Parses capture JSON and migrates it to the current (or `target`) schema version. Raises `MigrationError` subclasses. `default_version` opts into a version for captures missing `schema_version`.

### `load_envelope`

```python
load_envelope(raw: str | bytes, *, target: str | None = None,
              default_version: str | None = None) -> CaptureEnvelope
```

Full typed read path: parse → upgrade → reconstruct dataclasses. Unknown event `type` is a hard error (`UnknownEventTypeError`); unknown *fields* from newer-minor captures are dropped tolerantly.

### `register_migration`

```python
register_migration(from_version: str, to_version: str,
                   apply: Callable[[dict], dict], *, description: str = "") -> None
```

Registers a single-step, forward-only schema upgrade. `apply` must be pure (`dict -> dict`, no mutation, no I/O). Raises `ValueError` on a backward/same-version step or a duplicate outgoing edge.

### `MigrationError`

Base class of the seven typed read errors (see [Reading captures](#reading-captures-programmatically)). Subclasses import from `evalshift.trace.migrate`.

### `SCHEMA_VERSION` / `__version__`

`SCHEMA_VERSION` — the envelope schema version this SDK writes (`"2.1.0"`). `__version__` — the package version (`"0.5.0"`).

### `EvalShiftCallbackHandler`

```python
from evalshift.adapters.langchain import EvalShiftCallbackHandler

EvalShiftCallbackHandler(*, suite: str, redact: RedactSetting, tools: Any,
                         code_version: str = "")
```

LangChain `BaseCallbackHandler` that records a chain/agent run as one capture per root run. See [LangChain integration](#langchain-integration).

### `wrap_openai` / `wrap_anthropic` / `wrap_genai`

```python
from evalshift.adapters.openai import wrap_openai        # [openai] extra
from evalshift.adapters.anthropic import wrap_anthropic  # [anthropic] extra
from evalshift.adapters.genai import wrap_genai          # [google-genai] extra

wrap_openai(client: C) -> C
wrap_anthropic(client: C) -> C
wrap_genai(client: C) -> C
```

Return a drop-in proxy over a provider client instance that records one `model_call` per intercepted request made inside an active capture session, and is inert outside one. Typed as returning the client's own type for editor ergonomics; at runtime the value is an `evalshift.adapters._wrap.ClientProxy` (`unwrap()` gives the real client back). See [Provider client wrappers](#provider-client-wrappers).

---

## Troubleshooting / FAQ

### No capture file was written

Work down this checklist:

1. **Gate off** — is `EVALSHIFT_CAPTURE` set to `1`/`true`/`yes`/`on`? Anything else (including unset) means off.
2. **Wrong directory** — the default `.evalshift` is relative to the process's *current working directory*, not the repo root. Set `EVALSHIFT_DIR` for a stable location.
3. **Dedup collapsed it** — same `(suite, input_hash)` already written this process. The classic trigger: `agent_session` without `agent_input=` (every session hashes identically). Disable with `EVALSHIFT_DEDUP=off` to test.
4. **Sampling skipped it** — is `sample_rate` / `EVALSHIFT_SAMPLE_RATE` set?
5. **`require_model_call` dropped it** — the gate is on and the run recorded no `model_call` event.
6. **Redactor raised** — a raising redactor drops the capture fail-closed.
7. **Filesystem error** — read-only mount / disk full; the write degrades silently. Use `MemorySink` on read-only filesystems.
8. **Object store upload dropped it** — with `EVALSHIFT_SINK` / an `ObjectStoreSink` nothing is written locally; look in the bucket and in the logs. The first failed upload per sink logs a `WARNING` with the store URI and the error (wrong credentials, missing bucket); later failures are `debug`. An exit flush that times out logs a `WARNING` naming the store and how many items were dropped. A full upload queue drops the newest item with a `debug` line. A process stopped by `SIGTERM` without `sys.exit(0)` skips the exit flush entirely, with no log at all — see [Shutdown](#objectstoresink-and-cloud-stores).

Drops from `require_model_call`, a raising redactor, a filesystem error or a full upload queue each log one `debug` line; a failed upload or a timed-out exit flush logs a `WARNING`. Gate-off, sampling and dedup drops are silent — rule them out with the checklist above. Turn on the logger to see which branch fired:

```python
import logging
logging.basicConfig()
logging.getLogger("evalshift").setLevel(logging.DEBUG)
```

### `SinkConfigurationError` at startup

Your agent raises `evalshift.SinkConfigurationError` the instant it applies a `capture.*` decorator, wraps a client (`wrap_openai` / `wrap_anthropic` / `wrap_genai`), builds `EvalShiftCallbackHandler`, or calls `configure()` without a `sink` — never on bare `import evalshift`, and only while `EVALSHIFT_CAPTURE` is on. The cause is always an `EVALSHIFT_SINK` the SDK cannot build, and the message says why: `EVALSHIFT_SINK points at an object store (s3://), but boto3 is not installed. Run: pip install boto3, or unset EVALSHIFT_SINK to capture to local disk.` for a missing client library; `EVALSHIFT_SINK is not a valid store URI. Accepted forms: s3://<bucket>/<prefix>, gs://<bucket>/<prefix>, az://<account>/<container>/<prefix>. Credentials never go in the URI.` for a malformed value; or `EVALSHIFT_SINK could not be opened (<ExceptionType>). Unset it to capture to local disk.` for anything else. Fix it by installing the package the message names, fixing the URI, or unsetting `EVALSHIFT_SINK` so captures go to local disk — or unset `EVALSHIFT_CAPTURE` if you want capture off entirely.

### Does the SDK send data anywhere?

Not unless you tell it to. By default captures go to the local filesystem (`FileSink`) or process memory (`MemorySink`). If you set `EVALSHIFT_SINK` or configure an `ObjectStoreSink`, they go to the bucket *you* named, using *your* cloud credentials — never to EvalShift. The SDK has no telemetry and no endpoint of its own.

### Can I leave instrumentation in production?

Yes — that's the design. With the gate off, wrappers are pure pass-throughs. With it on, all SDK bookkeeping is fail-open: a capture bug degrades to a dropped file and a debug log line, never an exception or a slowdown surfaced to your agent (configuration is the one exception: an unusable `EVALSHIFT_SINK` raises at startup — see [Troubleshooting](#sinkconfigurationerror-at-startup)).

### How do I capture in AWS Lambda / read-only containers?

Prefer `EVALSHIFT_SINK=s3://...` (or `gs://` / `az://`): the capture and its toolset sidecar both go to the bucket, nothing touches disk, and the data survives the environment. On Lambda call `evalshift.flush_captures()` before the handler returns (background threads freeze between invocations) or use `ObjectStoreSink(..., background=False)`. The older pattern still works too: `configure(sink=MemorySink())` and drain with `sink.flush()`, or point `EVALSHIFT_DIR` at a writable mount (e.g. `/tmp`).

If you stay on `MemorySink` or `FileSink`, point `EVALSHIFT_DIR` at a writable mount whichever of the two holds the envelope itself: toolset sidecars (`ToolsetSink`, behind every `model_call`'s `toolset_ref`) are file-based for both, so on a filesystem with no writable mount anywhere, sidecar writes fail on every call even with `MemorySink()` configured, and every capture's `toolset_ref` stays unstamped — the CLI refuses to promote those. Pointing `EVALSHIFT_DIR` at `/tmp` (or another writable mount) fixes both cases at once.

---

## Further reading

- [docs/SCHEMA.md](docs/SCHEMA.md) — schema versioning, migration policy, the messages-list convention, `input_hash` semantics
- [docs/REDACTION.md](docs/REDACTION.md) — the redaction boundary, guarantees, and limits
- [docs/DECISIONS.md](docs/DECISIONS.md) — locked design decisions and their rationale
- [CHANGELOG.md](CHANGELOG.md) — release history
- [examples/support_agent/](examples/support_agent/) — runnable end-to-end demo agent
- [llms-full.txt](llms-full.txt) — dense single-file reference for AI coding tools, hosted at <https://www.evalshift.dev/sdk-llms-full.txt>
- [LICENSE](LICENSE) — MIT
