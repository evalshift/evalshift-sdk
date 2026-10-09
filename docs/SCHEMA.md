# Schema versioning & migration

A written capture (`cap_<id>.json`) carries a `schema_version` in its **envelope**; the inner
`AgentTrace` is the frozen CLI contract and carries no version of its own (D-5b). This document
defines how that version evolves and how an old capture stays readable — promotable and
replayable — under a newer SDK or CLI.

See `docs/DECISIONS.md` D-5 / D-5b (where the version lives) and D-8 (the read/migration policy).

## TL;DR

- `schema_version` lives in the **envelope only**; the inner `AgentTrace` is always CLI-valid.
- **MAJOR** = breaking envelope change (normally needs a migration; see the 2.0.0 exception below);
  **MINOR** = additive; **PATCH** = no field change.
- Reading is **upgrade-on-read**: `load_capture()` / `load_envelope()` migrate old → current, when
  a registered migration reaches the target.
- A **newer minor/patch** is tolerated (warn + best-effort read); a **newer major** is refused.
- An **older major** with no registered migration is refused too —
  `ObsoleteSchemaVersionError("capture written by an older evalshift-sdk; re-run your agent to
  re-capture.")` — rather than silently fabricating the fields it can't recover. As of 2.0.0 this
  is real, not hypothetical: 1.x captures are refused, not migrated (see below).
- The migrate/load path **raises** typed errors — it does **not** fail open like the capture path.

## The versioned surface

`schema_version` governs the **capture envelope** — the SDK's own wrapper — not the trace inside:

| Envelope key (`schema.ENVELOPE_KEYS`) | Versioned by `schema_version`            |
|---------------------------------------|------------------------------------------|
| `schema_version`                      | the version itself                       |
| `capture_id`, `suite`, `input_hash`, `code_version`, `created_at` | yes — envelope fields |
| `trace`                               | the inner value is the **CLI contract**; the CLI re-validates it independently |
| `conversation_id`, `turn_index`, `parent_capture_id` | yes — added in **1.1.0**, optional |

Because the trace is always emitted to the frozen CLI shape (`extra="forbid"`), schema versioning
never risks the trace's CLI-validity — it only governs the envelope around it.

**"Not the trace inside" describes validity, not whether a version bump is owed.** The inner
`AgentTrace` is independently CLI-valid regardless of `schema_version` — but adding a field to an
event *inside* the trace still requires bumping `schema_version`, even though `schema.ENVELOPE_KEYS`
itself doesn't change. `2.0.0` is the concrete proof: it exists solely because `model_call` gained
`toolset_ref` / `tools_offered`, a trace-internal change, with every envelope key untouched. The
bump matters because it's the only signal a reader has for "was this field even capturable when
this file was written" — see the `2.0.0` section below and the maintainer checklist's step 2.

## 1.1.0: multi-turn conversation identity

Schema `1.1.0` added three optional envelope fields so a multi-turn agent conversation — captured
as one capture file per turn — can later be linked back together for CLI-side promotion and
replay:

| Field               | Type          | Default | Meaning |
|----------------------|---------------|---------|---------|
| `conversation_id`    | `str \| None` | `None`  | Shared identifier for every capture belonging to the same conversation. `None` for a standalone (single-turn) capture. |
| `turn_index`         | `int \| None` | `None`  | This capture's position in the conversation (0-based recommended; the SDK does not enforce an ordering). |
| `parent_capture_id`  | `str \| None` | `None`  | The `capture_id` of the immediately preceding turn's capture, if any — lets a reader reconstruct turn order without relying on file mtimes. |

All three are threaded from the public capture API into `build_capture` (and from there into the
envelope) as keyword-only, default-`None` parameters — a standalone capture that never sets them
behaves exactly as before 1.1.0:

```python
from evalshift import capture, record_model_call

# One capture per turn — pass fresh conversation_id/turn_index/parent_capture_id each time.
with capture.agent_session(
    suite="support_agent",
    redact=True,
    tools=[],
    agent_input=messages,          # see "the messages-list convention" below
    conversation_id="conv_abc123",
    turn_index=2,
    parent_capture_id="cap_prev_turn_id",
):
    record_model_call(model_id="claude-opus-4-8", tools=[], input=messages, output=reply)
```

The `@capture.agent(...)` decorator also accepts the same three keyword arguments, but they are
**static** — fixed once at decoration time, so every call the decorated function makes writes the
same `turn_index` / `parent_capture_id`. That's fine for a single-turn agent; for a real
multi-turn conversation (a different `turn_index` per call), use `capture.agent_session` /
`capture.agent_session_async` instead and pass fresh values on each `with` / `async with`. There
is no stateful `capture.conversation()` helper — turn identity is the caller's responsibility to
track and pass in.

### The messages-list convention for `model_call.input`

`capture.model_call(input=...)` was already untyped (`input: Any`) before 1.1.0, so no signature
changed. The **convention** for a multi-turn agent is to pass the complete per-turn context as a
list of role-tagged messages:

```python
messages = [
    {"role": "system", "content": "You are a scheduling assistant."},
    {"role": "user", "content": "Can we move my appointment?"},
    {"role": "assistant", "content": "Sure — what time works?"},
    {"role": "user", "content": "1pm"},   # the current turn's user message
]
```

i.e. the system prompt, every prior turn, and the current user message — not just the latest
message in isolation. This is a documentation-only convention (the SDK does not validate the shape
of `input`); it exists so a capture's `trace` carries enough context for the CLI/report side to
render or replay a turn without needing the sibling captures in the conversation.

### Why `input_hash` changes shape for conversation turns

The per-process dedup registry (`evalshift.hygiene.dedup`, wired in via `HygieneSink`) keys on
`(suite, input_hash)`. Before 1.1.0, `input_hash` was always `canonical_hash(agent_input)`. Two
different turns of a conversation frequently share short, repeated user text — "yes", "1pm", "ok"
— which would collapse under that scheme and silently drop the second turn's capture.

`build_capture` (`src/evalshift/trace/serialize.py`) now branches:

- `conversation_id is None` (standalone capture): `input_hash = canonical_hash(agent_input)` —
  **byte-identical** to pre-1.1.0 behavior, so existing dedup keys don't shift.
- `conversation_id is not None`: `input_hash = canonical_hash({"agent_input": agent_input,
  "conversation_id": conversation_id, "turn_index": turn_index})` — folding in the turn identity
  means two turns with identical `agent_input` text hash differently, so dedup no longer collapses
  them.

## 2.0.0: per-call toolset fields, and no migration from 1.x

Schema `2.0.0` added two optional fields to `model_call` events so the CLI can tell "this agent
was offered no tools" apart from "we don't know what it was offered":

| Field           | Type                | Default | Meaning |
|-----------------|---------------------|---------|---------|
| `toolset_ref`   | `str \| None`       | `None`  | Content-addressed pointer (`sha256:<hex>`, see `evalshift.capture.toolset.fingerprint_tools`) to the full toolset sidecar written by `ToolsetSink`. |
| `tools_offered` | `list[str] \| None` | `None`  | Cheap, display-only list of the tool names offered on this call. |

Both are optional on the *trace* model too (mirroring the CLI's `ModelCallEvent`) so a reader
still accepts a capture written before per-call toolset capture existed; the CLI enforces their
presence at promotion time, not at parse time, where it can name the capture in the error instead
of failing a generic parse.

The sidecar those `toolset_ref`s point at holds `{"tools": [{name, description, input_schema}, …]}`,
each tool optionally carrying `strict: true` (see `docs/DECISIONS.md` D-toolset). The sidecar is
content-addressed rather than versioned, so adding that optional key needs no `SCHEMA_VERSION` bump:
a strict toolset simply hashes to a different sidecar than the same toolset without it, and a
toolset that declares no strict flag hashes exactly as it always did.

**This is a MAJOR bump with no migration.** Every prior bump registered at least an identity
migration (see the footnote below) so older captures kept upgrading on read. 2.0.0 breaks that
pattern deliberately: there is no honest way to derive `tools_offered` for a capture written
before this field existed. Defaulting it to `[]` would *assert* the agent was offered no tools —
indistinguishable from a real empty-toolset run, and false for every legacy capture that actually
had tools. Leaving it `None`-but-stamped-2.0.0 would be equally dishonest — the field would claim
schema 2.0.0 provenance for data schema 2.0.0 never captured. Neither is safe to fabricate, so
`trace/migrate.py` registers **no edge** out of the 1.x major, and reading a 1.x envelope raises
`ObsoleteSchemaVersionError` instead:

```
capture written by an older evalshift-sdk; re-run your agent to re-capture.
```

Captures are gitignored, regenerated state — re-running the instrumented agent produces a fresh,
honest 2.0.0 capture, which is the only fix this SDK offers. See the versioning policy and the
forward-compatibility table below for exactly when this refusal (as opposed to a normal migration)
fires, and `docs/DECISIONS.md` D-8 for the design rationale.

## 2.1.0: model-requested tool calls

Schema `2.1.0` added one optional field to `model_call` events so a reader can tell what the model
**asked for** apart from what the application actually **ran**:

| Field                  | Type                              | Default | Meaning |
|------------------------|-----------------------------------|---------|---------|
| `requested_tool_calls` | `list[RequestedToolCall] \| None` | `None`  | The tool calls present in the model's own response. `None` = not recorded (a capture written before 2.1.0, or a caller that passed nothing); `[]` = the model requested no tools. |

Each item is exactly `{"name": str, "arguments": dict, "call_id": str | None}` — a strict
`RequestedToolCall` model on the CLI side (`extra="forbid"`, `arguments` defaulting to `{}` and
`call_id` to `None`), a plain `dict` in the SDK's stdlib dataclass, which needs no nested type
(D-deps). The SDK normalises every item to those three keys and drops anything else before
stamping it, so the emitted JSON always validates against the CLI model.

Three distinct facts now live on one `model_call` event, and they are not interchangeable:

| Field                  | Question it answers            | Source |
|------------------------|--------------------------------|--------|
| `tools_offered` / `toolset_ref` | what *could* be called | the `tools=` you passed at the call |
| `requested_tool_calls` | what the model *asked* to call | the provider response |
| the `tool_call` events | what the app *actually ran*    | `@capture.tool` |

They diverge routinely — an app can ignore a requested call, run something the model never asked
for, or fail before dispatch — which is exactly why both are recorded rather than one inferred
from the other. See `docs/DECISIONS.md` D-requested.

**This is a MINOR bump with an identity migration.** The field is additive with an honest default
(absent → `None`, "not recorded"), so `_register_builtins()` registers `2.0.0 -> 2.1.0` as a no-op
step (`_migrate_2_0_0_to_2_1_0`). The edge is not decorative: `_build_chain` walks by exact
`from_version`, so without it every 2.0.0 capture would become unreadable. It deliberately does
**not** fabricate `[]` — that would assert the model requested no tools on every pre-2.1.0 call,
the same dishonesty 2.0.0 refused for `tools_offered`.

**Field ordering matters here.** The CLI's trace models are `extra="forbid"`, so the CLI must
learn the field before the SDK writes it; it did, and the SDK emits it from 2.1.0 on.
`requested_tool_calls` sits immediately after `tools_offered` in both the SDK dataclass
(`trace/models.py`) and `schema.EVENT_FIELDS["model_call"]`, mirroring the CLI class body so the
conformance parity drift guard keeps the two field sets aligned.

## Semantic-versioning policy

The version is `MAJOR.MINOR.PATCH`.

| Bump  | Meaning                                                            | Migration step? |
|-------|-------------------------------------------------------------------|-----------------|
| MAJOR | a backward-incompatible envelope change — a field removed/renamed/retyped, or a reshape an old reader can't safely interpret | **normally required** (mechanically transformable) — *unless a safe default genuinely doesn't exist, see below[^major-refusal]* |
| MINOR | a backward-compatible **additive** change — a new optional envelope field with a default | **required** (identity, at minimum)[^minor-edge] |
| PATCH | no field-set change — docs, a relaxed constraint, a derived-value fix | none |

[^minor-edge]: Every schema bump — MAJOR *or* MINOR — must register at least one edge in
    `_register_builtins()` (`src/evalshift/trace/migrate.py`) *if* older captures should keep
    upgrading on read. `_build_chain` walks the registry by exact `from_version`; a source version
    with no registered outgoing edge has no path to the target. A MINOR bump can almost always
    register a trivial identity migration rather than a reshaping one, because additive fields
    have an honest default — `_migrate_2_0_0_to_2_1_0` (schema 2.1.0's `requested_tool_calls`) is
    the shipped example, and 1.1.0's since-removed `_migrate_1_0_0_to_1_1_0` was the earlier one.
    Skipping the edge anyway makes every older capture unreadable by this SDK, so treat it as
    required for MINOR.

[^major-refusal]: A MAJOR bump's migration is only "mechanically transformable" when there's a
    value an old capture's absence of a field can honestly become. `2.0.0` is the first bump where
    that's not true — `tools_offered` has no honest default for a capture written before per-call
    toolset capture existed, so it registers no migration at all (see the `2.0.0` section above).
    When a MAJOR bump registers no edge from an older version, that version simply isn't reachable:
    `upgrade_envelope_dict` raises `ObsoleteSchemaVersionError` for a gap spanning a major-version
    boundary (`NoMigrationPathError` is reserved for a missing edge *within* a major — a registry
    bug, not a deliberate refusal). This is a real, supported outcome, not an error state to avoid
    — decide it deliberately per MAJOR bump rather than always reaching for `register_migration`.

## Reading a capture (upgrade-on-read)

```python
from evalshift import load_capture, load_envelope

upgraded = load_capture(text)        # parse JSON + migrate to current -> dict
envelope = load_envelope(text)       # + reconstruct a typed CaptureEnvelope
```

The chain inside `upgrade_envelope_dict`:

1. **detect** the source version (`detect_version`).
2. **apply the forward-compat policy** (below) — may return as-is with a warning, or raise
   `UnsupportedSchemaVersionError` for a newer major.
3. **run the migration chain** step by step (each step is a pure `dict -> dict`). If no registered
   chain reaches the target: a gap spanning a major-version boundary raises
   `ObsoleteSchemaVersionError` (the older-major refusal); a gap *within* the same major raises the
   lower-level `NoMigrationPathError` (a registry bug — every MINOR bump must register an edge).
4. On success, **re-stamp** `schema_version` to the target.

`load_envelope` then reconstructs typed dataclasses (the inverse of `to_jsonable`): unknown event
`type` is a hard error; unknown *fields* are dropped tolerantly (forward-minor compatibility).

## Adding a new schema version (for SDK maintainers)

1. Bump `SCHEMA_VERSION` in `src/evalshift/trace/schema.py`. Decide `SUPPORTED_SCHEMA_VERSIONS`
   deliberately — it is not simply appended to: it should list every `schema_version` this SDK can
   actually produce via `upgrade_envelope_dict` (the new version, plus any older one a registered
   migration still bridges to it). A version dropped from the tuple should also lose its bridge
   (step 3) — see 2.0.0, which dropped `1.0.0` and `1.1.0` together with their migration. It is
   currently `("2.0.0", "2.1.0")`: `2.0.0` stays listed because the identity edge added in 2.1.0
   still bridges it.
2. If the **trace** contract changed, re-sync the vendored CLI model
   (`tests/conformance/cli_models_vendored.py`) and the `schema.py` field constants — the drift
   guard in `tests/conformance/test_parity.py` enforces parity.
3. For a **MAJOR** bump, `register_migration("<old>", "<new>", fn)` in `_register_builtins()`
   (`trace/migrate.py`) so old captures upgrade on read — *unless* there is no honest value an old
   capture's absent field can become, in which case register nothing and let
   `ObsoleteSchemaVersionError` refuse it (see the `2.0.0` section above; don't invent a migration
   just to avoid the refusal). A MINOR bump needs at least an identity step (see the versioning
   policy above); PATCH needs none.
4. Update this doc (including `SUPPORTED_SCHEMA_VERSIONS`'s new contents and, if the migration
   story changed, the forward-compatibility table below) and add a `docs/DECISIONS.md` D-8 note.

## Version-compatibility (reading an older or newer capture)

| Source version vs. supported      | Behavior                                               |
|------------------------------------|--------------------------------------------------------|
| older, **same major**              | migrate up the registered chain — e.g. `2.0.0` → `2.1.0` via the built-in identity step (`NoMigrationPathError` if a step is missing — a registry bug) |
| older, **different (lower) major** | refuse — raise `ObsoleteSchemaVersionError`, *unless* a registered chain happens to bridge the gap (tried first; see the 2.0.0 section above) |
| same                               | read as-is                                             |
| newer **minor/patch**, same major  | **warn** (`logging.getLogger("evalshift")`) and read best-effort; unknown fields dropped |
| newer **major**                    | refuse — raise `UnsupportedSchemaVersionError`         |

**Why tolerating newer-minor is safe:** minor bumps are additive-only within a major, and the
version governs only the envelope — the inner trace is an independently CLI-valid `AgentTrace`. An
older reader can therefore read a newer-minor file by ignoring fields it doesn't know. A newer
**major** may have removed or reshaped envelope fields this SDK depends on, so reading it would be
unsound — we refuse loudly.

**Why an unbridged older major is also refused:** the symmetric case. A major bump may have added
fields with no honest value for older data (2.0.0's `tools_offered` is the concrete example) —
fabricating one would silently assert something the original capture never recorded. Refusing with
`ObsoleteSchemaVersionError` is deliberately louder and more specific than the internal
`NoMigrationPathError` a bare registry gap would otherwise surface: it tells the caller what
happened (an older SDK wrote this) and what to do about it (re-run the agent), rather than leaking
a "no migration registered from X toward Y" implementation detail. It only fires when the
registered chain genuinely can't reach the target — a maintainer who *does* register a bridge
across the major boundary is honored, not overridden.

## Guarantees & limits

- **Raise, don't fail open.** Reading is read-side tooling for tests and your own scripts (the CLI
  has its own reader; the capture layout, on disk or in a bucket, is the only SDK↔CLI
  interface), not the capture hot path. It raises typed `MigrationError` subclasses
  (`UnreadableCaptureError`,
  `MissingSchemaVersionError`, `InvalidSchemaVersionError`, `UnsupportedSchemaVersionError`,
  `NoMigrationPathError`, `ObsoleteSchemaVersionError`, `UnknownEventTypeError`) — contrast the
  capture path's fail-open `safety.py` boundary, which must never break the host agent.
- **Pure, forward-only migrations.** Steps are `dict -> dict`, do no I/O, and must not mutate their
  input (`upgrade_envelope_dict` migrates a deep copy). The chain is **linear** — one outgoing step
  per version — and there is **no** downgrade path.
- **No missing-version guessing.** A capture without `schema_version` raises unless a caller opts
  into `default_version=` (there are no real pre-version captures).
- **The CLI re-validates the trace.** This SDK read path stays validation-light (stdlib-only,
  pydantic is dev/test only); CLI-validity is enforced by the conformance tests here and by the
  CLI's own loader downstream.
