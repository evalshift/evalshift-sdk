# Redaction & the capture data boundary

EvalShift captures the *inside* of an agent run — tool arguments, tool results, model inputs and
outputs. That payload routinely contains PII (emails, names, API keys). This document defines
**where redaction happens, what it guarantees, and what a written capture may still contain.**

See `docs/DECISIONS.md` D-4 / D-4a / D-4c for the locked decisions behind this (D-4b, the
original opt-in policy, is superseded by D-4c) and D-toolset for the redaction-exemption
mechanism behind `model_call.toolset_ref` / `tools_offered` specifically.

## TL;DR

- Redaction runs **in-process, before any byte hits disk or an object store** (D-4).
- `redact=` is **required** at every capture point — masking is an explicit decision, never a
  default (D-4c). There is no way to instrument an agent without answering the question.
- If your redactor **raises, the capture is dropped** — never written half-masked (D-4a). Your
  agent is unaffected.

## Choosing a value

`redact` accepts a bool for the two common answers, or a callable for anything else:

| `redact=` | What happens |
|---|---|
| `True` | Mask with `default_redactor` — emails, `sk-…`, `AKIA…`, `Bearer …`, **and nothing else** |
| `False` | Capture payloads verbatim, on purpose |
| a callable | Your own `(value) -> redacted_value` redactor |

Anything else — including `None` — raises `TypeError` at the capture point, whether or not
`EVALSHIFT_CAPTURE` is set.

```python
from evalshift import capture

@capture.agent(suite="support_agent", redact=True, tools=[])
def handle_ticket(query): ...
```

`False` is the right answer when you need byte-exact goldens and you know the payload is safe —
it is not a lesser choice, but it is a choice, and it shows up in review:

```python
@capture.agent(suite="fixtures", redact=False, tools=[])
def replay_fixture(case): ...
```

Custom redactor (full control — recurse however you like, return a redacted copy). Reach for this
whenever your data has structured secrets; `True` will not find them:

```python
def scrub(value):
    if isinstance(value, dict):
        return {k: ("***" if k == "ssn" else scrub(v)) for k, v in value.items()}
    if isinstance(value, str):
        return value.replace(SECRET, "[REDACTED]")
    return value

@capture.agent(suite="clinical", redact=scrub, tools=[])
def handle_case(record): ...
```

The same required `redact=` applies to `capture.agent_session`, `capture.agent_session_async`, and
`EvalShiftCallbackHandler`. There is no process-wide setter: `configure(redact=...)` was removed in
0.3.0 so that no state set elsewhere can change what a given agent records.

## Where it runs (the boundary)

Redaction is applied in `build_capture` (`src/evalshift/trace/serialize.py`) by `redact_tree`
(`src/evalshift/redaction/base.py`), **before** the span tree is serialized into trace events and
**before** the `Sink` writes anything. The redactor only ever sees in-memory payloads; it never
sees on-disk bytes.

Redactable fields, per recorded span kind:

| Span kind      | Fields passed to the redactor          |
|----------------|----------------------------------------|
| `tool`         | `arguments`, `result`, `error`         |
| `model_call`   | `input`, `output`, `requested_tool_calls` |
| `retrieval`    | `query`, `documents`                   |
| `guardrail`    | `reason`                               |
| `final_output` | `text`                                 |
| `error`        | `message`                              |

Because redaction runs **before** serialization, the tool replay-fixture key
(`(call_id, input_hash)`, D-1) is computed from the **redacted** arguments — the capture stays
internally consistent.

`requested_tool_calls` (what the model *asked* to call — D-requested, schema 2.1.0) is in the
table because it is payload: those arguments are values the model generated from the user's input,
as sensitive as a tool call's own. `default_redactor` walks dicts and lists recursively, so the
one entry masks every nested argument value; the tool `name` is structural and, like every other
name, is left alone.

Two `model_call` fields are deliberately outside this table: `toolset_ref` / `tools_offered`
(the toolset a call was offered — D-toolset). They are config, not payload, the same class as
`metadata["generation_config"]` — but safe from redaction by a different mechanism, worth stating
precisely. `generation_config` lives in `span.metadata`, which `redact_tree` never walks at all.
`toolset_ref` / `tools_offered` are top-level `span.data` fields — the same dict `input` /
`output` / `requested_tool_calls` live in — and are safe only because the table above names
`model_call`'s redactable fields one by one; neither toolset field is among them. If a tool's `description` or `input_schema`
carries something sensitive, it reaches the toolset sidecar (`<base>/toolsets/<hex>.json`)
unmasked. Tool definitions are expected to be static, developer-authored schemas, not
user-provided payload — the same assumption `generation_config` already makes — but if that
assumption doesn't hold for your agent, scrub the toolset before passing it as `tools=`.

## Guarantees

- **Fail-closed (D-4a).** A redactor that raises drops the whole capture (no file) and logs one
  debug line. We never write a possibly-unredacted file. The host agent still returns normally.
- **Host-safe.** Redaction errors can never propagate to your agent (`safety.guard` boundary).
- **No mutation of your data.** `default_redactor` returns redacted copies; it does not mutate the
  values your agent passed around. (Custom redactors should do the same.)

## What a written / promoted artifact MAY still contain

Redaction is a masking pass over payload **values**. It does **not** scrub:

- **Structural metadata** — span/tool `name`s, `model_id`, token counts, costs, timestamps,
  `sequence_index`, `call_id`/`parent_call_id`, and the `metadata["evalshift"]` concurrency block.
- **The capture envelope** — `capture_id`, `suite`, `code_version`, `created_at`, and the
  `input_hash` (a one-way SHA-256 of the agent's bound input; the raw input itself is **never**
  stored, so it is not separately redacted).
- **Anything your redactor misses.** `default_redactor` covers emails and a few key formats only
  (precise patterns, to avoid mangling benign text). It is **not** a comprehensive PII scrubber —
  supply a domain-specific redactor when your data has structured secrets.
- **`toolset_ref` / `tools_offered` on `model_call`, and the toolset sidecar they point at**
  (`<base>/toolsets/<hex>.json`) — config, not payload, like `generation_config`; see above for
  exactly why redaction never reaches them.

A **promoted golden case** (Phase 9) is a *copy* of a capture, so it inherits exactly the boundary
above: redacted payloads, intact structure + envelope. Redact at capture time if you do not want a
value to reach a golden suite or an upload.

## Limitations (this phase)

- `redact=True` is not comprehensive PII coverage. Its patterns are intentionally conservative
  (emails, `sk-…`, AWS `AKIA…`, `Bearer …`); pass your own callable for your domain.
- Requiring `redact=` forces the decision to be made, not to be made *well* — `redact=False` is
  always available and always silent about whether it was the right call.
- Sampling / dedup hygiene (Phase 6) compose around redaction but do not change the boundary.
