# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `ObjectStoreSink`: ship captures and toolset sidecars to an object store you
  own — Amazon S3 (and S3-compatible stores via `AWS_ENDPOINT_URL`), Google
  Cloud Storage or Azure Blob Storage — for hosts whose disk does not outlive
  them (Fargate, Lambda, pods). Select it with one env var,
  `EVALSHIFT_SINK=s3://<bucket>/<prefix>` (`gs://…`, `az://<account>/<container>/…`),
  or `configure(sink=ObjectStoreSink(store))`. Keys mirror the local layout
  exactly, so the CLI reads a mirrored bucket unchanged. Uploads run on a
  bounded background queue and never raise into the agent; the first failed
  upload per sink logs a warning, and so does an exit flush that times out with
  uploads still queued (naming the store and how many items it dropped). Extras: `evalshift-sdk[s3]`, `[gcs]`,
  `[azure]`. New top-level `flush_captures(timeout)` for shutdown hooks and
  Lambda. Local disk remains the default; nothing changes for existing users.
- `evalshift.stores`: the `ObjectStore` protocol, `MemoryStore`, the shared
  store URI grammar (`parse_store_uri`, `open_store`) and the three adapters.

### Changed

- An invalid `EVALSHIFT_SINK` value, or one whose extra is not fully
  installed (for `az://`, both `azure-storage-blob` and `azure-identity`),
  logs **one warning** and falls back to local disk — a deliberate
  warning-level exception to the capture path's usual debug-level logging,
  because a silently dropped firehose is the loss this feature exists to
  prevent. The warning names the variable and the accepted forms (or the pip
  extra to install), never the value, so a credential pasted into it stays out
  of logs.
- Toolset sidecars follow the capture sink: with an `ObjectStoreSink` they are
  written to the same store; `FileSink`, `MemorySink` and custom sinks keep
  today's on-disk sidecar.
- Docs: "disk is the only interface" between SDK and CLI becomes "the layout is
  the interface, on disk or in a bucket"; the "Does the SDK send data
  anywhere?" FAQ now answers "not unless you tell it to", and then only to
  your bucket.
- The repository moved from the `babaliauskas` GitHub account to the
  `evalshift` organization: <https://github.com/evalshift/evalshift-sdk>. The
  PyPI project links and the docs point there; GitHub redirects the old URLs.

## [0.4.1] - 2026-10-01

### Changed

- Docs: `examples/support_agent/README.md` calls the CLI's one-command
  pipeline `evalshift compare`, its new name upstream. The former `all`
  stays registered as a permanent hidden alias, so the old spelling in any
  existing script keeps working.
- Docs: DeepSeek is listed among the OpenAI-compatible APIs `wrap_openai`
  captures unchanged, with the exact client construction. Replaying those
  captures needs EvalShift CLI 1.2.0 or later.
- Docs: corrected a batch of facts a docs-currency audit found stale or
  wrong across DOCS.md, llms.txt, llms-full.txt, docs/SCHEMA.md and
  README.md — the documented version string (was still 0.3.0 in two
  spots), the `generation_config` and redactable-field key counts, the
  missing `ObsoleteSchemaVersionError` (the read path's seventh
  `MigrationError` subclass, raised for every pre-2.0 capture, with no
  registered bridge across a major-version boundary), a `SCHEMA.md`
  example that omitted the now-required `tools=` and would raise
  `TypeError`, which log lines a dropped capture actually emits
  (gate-off, sampling and dedup drops are silent; a `require_model_call`
  drop, a raising redactor or a filesystem error logs one debug line),
  which names import from the top-level `evalshift` package versus
  `evalshift.sinks` / `evalshift.trace`, the `EVALSHIFT_DEDUP` values
  that actually disable dedup (`false`/`no` are not recognised and leave
  it on) and the `EVALSHIFT_SAMPLE_RATE=0` vs. `configure(sample_rate=0.0)`
  distinction, and the README's PyPI-broken relative links (now absolute
  GitHub URLs).

## [0.4.0] - 2026-09-10

### Added

- Provider client wrappers: `evalshift.adapters.openai.wrap_openai(client)`,
  `evalshift.adapters.anthropic.wrap_anthropic(client)` and
  `evalshift.adapters.genai.wrap_genai(client)` return a drop-in proxy over a
  client you already built. Inside a capture session every intercepted call
  (`chat.completions.create` / `responses.create`; `messages.create` /
  `messages.stream`; `models.generate_content[_stream]` and the `aio` twins —
  sync, async and streaming) records one `model_call` with `model_id`, the
  tools offered, `requested_tool_calls`, `input` (always a messages-style list,
  system prompts folded in as a leading `system` message), `output`, token
  usage, latency and the allow-listed generation settings. Nothing is
  monkeypatched, the real call is never guarded, and the wrapper is inert
  outside a session. New optional extras `[openai]`, `[anthropic]`,
  `[google-genai]`; the runtime stays stdlib-only. OpenAI-compatible servers
  (Ollama, vLLM, Groq, OpenRouter ...) are covered by `wrap_openai` with a
  `base_url`. `cost_usd` stays 0 — the CLI prices tokens at promote time. See
  `docs/DECISIONS.md` D-wrappers.
- `generation_config` records three more keys: `tool_choice`,
  `parallel_tool_calls`, and `tool_config` (Gemini's spelling of `tool_choice`),
  so `evalshift capture sync` can replay a case under the same tool-use
  constraint the source ran under instead of silently dropping it.
  `parallel_tool_calls: false` survives intact — the allow-list filters on
  `is not None`, never truthiness. The LangChain adapter picks all three up
  from `invocation_params`, so `bind_tools(tool_choice=...,
  parallel_tool_calls=False)` is recorded with no extra work.
- `evalshift.capture.generation.jsonable` now dumps an object exposing a
  `model_dump` method (a `google.genai.types.ToolConfig`, say — duck-typed via
  `getattr`, never imported) to a dict rather than its `str()` form, so a
  Gemini `tool_config` lands as readable JSON. Anything without a usable
  `model_dump` still degrades to `str()` exactly as before.
- Normalised tools carry an optional `strict: true` — from OpenAI's
  `function.strict` or Anthropic's top-level `strict` — alongside
  `{name, description, input_schema}`. The key is present only when the source
  tool declared it truthy and omitted entirely otherwise, so every toolset
  fingerprint written before this change is byte-for-byte unchanged and no
  `SCHEMA_VERSION` bump is needed. Without it a replay would re-send the schema
  under a weaker constraint than the source ran under. See `docs/DECISIONS.md`
  D-toolset.
- Trace schema `2.1.0`: `model_call` events carry an optional
  `requested_tool_calls` list of `{name, arguments, call_id}` items — what
  the *model asked to call* in its response, as distinct from `tools_offered`
  (what it was allowed to call) and the `tool_call` events (what the app
  actually ran). `null` means "not recorded", `[]` means "the model requested
  no tools"; see `docs/DECISIONS.md` D-requested.
- `record_model_call(..., requested_tool_calls=[...])` and
  `rec.set_requested_tool_calls([...])` on the `capture.model_call` recorder
  record that list. Both are optional and fail-open: a malformed value is
  dropped with a debug log rather than raised, and each item is normalised to
  exactly `{name, arguments, call_id}` so the capture stays CLI-valid. Unlike
  `tools=`, these arguments are redacted — they are model-generated payload,
  so `requested_tool_calls` joins `input` / `output` in `model_call`'s
  redactable fields.
- `evalshift.capture.requested.extract_requested_tool_calls(response)` — a
  stdlib-only helper that reads the tool calls a model *requested* out of an
  OpenAI (Chat Completions or Responses), Anthropic, or Gemini response, as
  `{name, arguments, call_id}` items for `record_model_call`. `[]` means the
  model requested nothing; `None` means the value was not a recognised
  response (or one of its calls was unreadable) — the two are not
  interchangeable. Never raises, and imports no provider SDK.
- The LangChain adapter records `requested_tool_calls` with no extra wiring:
  `on_llm_end` reads `AIMessage.tool_calls` (already provider-normalised by
  LangChain) and maps it through the same normaliser `record_model_call` uses.
  A chat model that asked for nothing records `[]`; a plain text completion,
  which cannot ask, records nothing. `invalid_tool_calls` are excluded — they
  are parse failures, not requests.

### Changed

- **Schema `2.0.0` → `2.1.0`** (MINOR, additive). `SUPPORTED_SCHEMA_VERSIONS`
  is now `("2.0.0", "2.1.0")` and a built-in identity migration upgrades a
  2.0.0 capture on read — `requested_tool_calls` stays absent (reading back as
  `None`) rather than being fabricated as `[]`. 1.x captures are still refused
  with `ObsoleteSchemaVersionError`, unchanged.

- Docs: `docs/DECISIONS.md` §1 and `examples/support_agent/README.md` no
  longer describe recorded tool results as captured-but-unreplayed. The CLI
  now carries them as `tool_result_fixtures` (`capture sync --rounds all`) and
  replays multi-round examples teacher-forced, so the halt-and-flag policy
  drafted there was never needed; the `input_hash`-keyed `build_fixture_table`
  remains unread by the CLI.
- Packaging: the EvalShift CLI (`evalshift` 0.14.0+) now depends on this
  package and imports as `evalshift_cli`, so the two install into one
  environment and `pip install evalshift` brings the SDK with it. The
  "separate virtual environments" rule is gone from the README and DOCS.
  No code change; `import evalshift` is, as before, this SDK.

## [0.3.0] - 2026-08-23

First release from the public repository. Compared to 0.2.0 on PyPI:

### Changed

- **Breaking:** `redact=` is now a required keyword on every capture entry
  point. Pass `True` for the built-in `default_redactor`, `False` to capture
  verbatim, or a custom callable (the new public `RedactSetting` type).
  Masking is an explicit choice, never a default.
- **Breaking:** trace schema is now `2.0.0` (adds `toolset_ref` /
  `tools_offered` on `model_call` spans). No migration from 1.x is registered:
  loading a 1.x capture raises `ObsoleteSchemaVersionError` rather than
  silently asserting it ran with no tools offered. Re-capture to upgrade.
- `generation_config` values are sanitized through a shared allow-list with
  JSON coercion, so a non-serializable config value can no longer drop the
  whole capture at sink-write time, and un-allow-listed keys (e.g.
  `system_instruction`) never reach the capture.

### Added

- Toolset capture: the tools an agent was offered on each model call are
  normalized from Anthropic, OpenAI, or Gemini shapes into one canonical
  form, fingerprinted, and written once as a content-addressed sidecar
  (`.evalshift/toolsets/<hex>.json`) referenced by `toolset_ref`.
- `RedactSetting` exported from the package root.
