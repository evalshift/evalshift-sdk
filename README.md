# evalshift-sdk

In-process capture SDK for [EvalShift](https://github.com/evalshift/evalshift-cli).

Install it inside your agent process to record what the agent does — model calls, tool calls,
retrievals — and write CLI-valid traces to `.evalshift/captures/`, or to an object store you
own when the host's disk does not outlive it. The `evalshift` CLI reads those captures from
disk, or mirrors them down from the bucket; the layout is the interface, and the SDK and CLI
never call each other.

- **Distribution:** `evalshift-sdk` · **import name:** `evalshift`
- **Runtime deps:** none (stdlib-only)
- **Python:** >= 3.10
- **Capture is off by default** — set `EVALSHIFT_CAPTURE=1` to record.
- **License:** [MIT](https://github.com/evalshift/evalshift-sdk/blob/main/LICENSE)

## For AI coding agents

Point your coding agent at the dense, single-file reference for the piece it is
working on:

- EvalShift CLI: <https://www.evalshift.dev/cli-llms-full.txt>
- EvalShift SDK: <https://www.evalshift.dev/sdk-llms-full.txt>
  (source of truth: [llms-full.txt](https://github.com/evalshift/evalshift-sdk/blob/main/llms-full.txt) in this repo)
- EvalShift GitHub Action (CI): <https://www.evalshift.dev/ci-llms-full.txt>

## Install

```bash
pip install evalshift-sdk
# or
uv add evalshift-sdk
```

Optional LangChain integration (`EvalShiftCallbackHandler`):

```bash
pip install "evalshift-sdk[langchain]"   # adds langchain-core>=0.2
```

Optional provider client wrappers (`wrap_openai` / `wrap_anthropic` / `wrap_genai`):

```bash
pip install "evalshift-sdk[openai]"        # openai>=1.40
pip install "evalshift-sdk[anthropic]"     # anthropic>=0.40
pip install "evalshift-sdk[google-genai]"  # google-genai>=1.0
```

Optional object-store sinks (`EVALSHIFT_SINK=s3://…` / `gs://…` / `az://…`, see below) use the
provider's own client library — install it like any other package:

```bash
pip install boto3                              # s3:// — Amazon S3 and S3-compatible stores
pip install google-cloud-storage               # gs://
pip install azure-storage-blob azure-identity  # az://
```

Every adapter and store module is import-guarded, so the SDK stays dependency-free at runtime unless you opt in.

> **Co-install note:** the EvalShift CLI (PyPI `evalshift`, import package `evalshift_cli`)
> depends on this SDK, so both live in one environment and `pip install evalshift` brings the
> SDK with it. Production agents that only record captures install `evalshift-sdk` alone.

## Usage

```python
from evalshift import capture

@capture.agent(suite="support_agent", redact=True, tools=[])   # no-op unless EVALSHIFT_CAPTURE=1
def handle_ticket(query): ...
```

Already calling a provider SDK directly? Wrap the client once and every call inside the agent records itself — model, tools offered, tool calls requested, usage, latency:

```python
from openai import OpenAI
from evalshift.adapters.openai import wrap_openai      # also: wrap_anthropic, wrap_genai

client = wrap_openai(OpenAI())                          # OpenAI(base_url=...) covers DeepSeek, Ollama, vLLM, Groq, ...
```

> Full guide: [DOCS.md](https://github.com/evalshift/evalshift-sdk/blob/main/DOCS.md) · dense LLM reference: <https://www.evalshift.dev/sdk-llms-full.txt> ·
> locked design decisions: [docs/DECISIONS.md](https://github.com/evalshift/evalshift-sdk/blob/main/docs/DECISIONS.md)

## Keeping `captures/` bounded

Capture writes one JSON file per sampled invocation, so `.evalshift/captures/` is kept bounded by
default (no configuration needed): identical-input re-runs are de-duplicated, and each suite
directory is capped at the **200 newest** captures (oldest evicted). Tune it with env vars — no code
change required (precedence: an explicit `configure(...)` call > env var > built-in default):

| Env var | Default | Meaning |
| --- | --- | --- |
| `EVALSHIFT_MAX_CAPTURES` | `200` | Max captures kept per suite dir. `0` / `none` / `unlimited` = uncapped. |
| `EVALSHIFT_DEDUP` | `on` | Collapse identical-input captures (per-process). `off` to disable. |
| `EVALSHIFT_CAPTURE_TTL` | off | Evict captures older than N **seconds**. |
| `EVALSHIFT_SAMPLE_RATE` | off | Capture only this fraction of runs, e.g. `0.25`. |
| `EVALSHIFT_DIR` | `.evalshift` | Capture root directory. |

A malformed value falls back to the default (capture never crashes). To restore fully unbounded
capture: `EVALSHIFT_MAX_CAPTURES=0 EVALSHIFT_DEDUP=off`. Disable dedup with `off` (or `0`/`none`);
`false`/`no` are not recognised and leave dedup on. `EVALSHIFT_SAMPLE_RATE=0` means sampling off
(capture every run) — it is not the same as `configure(sample_rate=0.0)`, which captures nothing.
The same knobs are available in code via
`configure(max_captures=..., dedup=..., capture_ttl=..., sample_rate=...)`.

## Hosts whose disk does not outlive them

On Fargate, Lambda or Kubernetes the capture directory disappears with the task. Ship captures
to an object store you own instead — one env var next to the gate, no code change:

```bash
EVALSHIFT_CAPTURE=1 EVALSHIFT_SINK=s3://acme-evals/support-agent python agent.py
```

`s3://<bucket>/<prefix>` (also MinIO, R2, B2 via `AWS_ENDPOINT_URL`), `gs://<bucket>/<prefix>` and
`az://<account>/<container>/<prefix>` are accepted; install the provider's client library (above).
Credentials come from each provider's default chain, never from the URI.

Uploads run on a bounded background thread and never raise into the agent. Configuration is the
one thing that does raise: if `EVALSHIFT_SINK` is set but the library is missing or the value is
malformed, the first `capture.*` decorator, client wrapper or `configure()` call raises
`SinkConfigurationError` — at startup, naming the `pip install` to run — instead of quietly
writing to local disk. With `EVALSHIFT_CAPTURE` off nothing happens at all. The keys mirror the
local layout exactly, so the CLI reads a bucket unchanged once `captures.store` names it in
`evalshift.yaml`. Hosts stopped by `SIGTERM`
should call `evalshift.flush_captures()` from a shutdown hook (Lambda: before the handler
returns). The hygiene knobs above apply to local disk only; bucket retention is a lifecycle rule.
Details: [DOCS.md — ObjectStoreSink and cloud stores](https://github.com/evalshift/evalshift-sdk/blob/main/DOCS.md#objectstoresink-and-cloud-stores).

## Further reading

- [Build a golden eval suite from production traffic](https://www.evalshift.dev/blog/build-a-golden-suite-from-production-traffic) — the capture → promote loop this SDK feeds.
- [Evaluating agent tool calls: what text evals can't see](https://www.evalshift.dev/blog/evaluating-agent-tool-calls) — why the captured tool calls matter more than the output text.
- [Capture SDK docs](https://www.evalshift.dev/docs/sdk)
