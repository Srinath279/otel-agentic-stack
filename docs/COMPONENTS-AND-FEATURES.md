# Components & Features — Code-Grounded Reference

A per-component reference written **against the current code** (`agent/`, `obs/`, `k8s/`, `collector/`).
For each piece: what it is, how it works *here* (with file/line anchors), the features it provides, and
how to push it further. Data flows left→right:

```
agent (obs SDK) ──OTLP/gRPC──▶ gateway collector ──Kafka──▶ consumer collector ──▶ Tempo / Prometheus / Loki ──▶ Grafana
                                  (stateless)                    (stateful)              ▲ MinIO (S3)   ▲ Alertmanager
```

> **Doc drift note:** the older `docs/COMPONENTS.md` predates three changes now in the code. This file
> reflects the code as it actually is:
> 1. **`spanmetrics` runs in the gateway** (pre-sampling), not the consumer — [30-collector-gateway.yaml:63-73](../k8s/30-collector-gateway.yaml#L63-L73).
> 2. **Prometheus ingests via remote-write (push)**, it does *not* scrape the consumer's `:8889` — [31-collector-consumer.yaml:65-68](../k8s/31-collector-consumer.yaml#L65-L68), [22-prometheus.yaml:17](../k8s/22-prometheus.yaml#L17).
> 3. **Tempo + Loki are backed by MinIO/S3 object storage**, a component the older doc omits entirely — [15-minio.yaml](../k8s/15-minio.yaml).
> The tail-sampling "sample-rest" policy is also set to **100%** locally (keep everything for demo visibility), not 10% — [31-collector-consumer.yaml:55-57](../k8s/31-collector-consumer.yaml#L55-L57).

---

## At a glance

| # | Component | Image | Tier / role | Key ports |
|---|---|---|---|---|
| 1 | **`obs`** instrumentation lib | baked into agent | 2-line OTEL bootstrap (traces+metrics+logs) | — |
| 2 | **agent** demo workload | `agentic-demo:dev` | FastAPI tool-loop; Claude or mock | 8080 → NodePort 30080 |
| 3 | **gateway collector** | `otelcol-local:dev` (contrib 0.119.0) | Stateless ingest · enforce · redact · spanmetrics → Kafka | 4317/4318, 8888, 13133 |
| 4 | **Kafka** | `apache/kafka:3.8.0` (KRaft) | Durable buffer, 3 topics | 9092 |
| 5 | **consumer collector** | `otelcol-local:dev` | Stateful tail-sampling · routing → backends | 8888, 13133, (8889 vestigial) |
| 6 | **Tempo** | `grafana/tempo:2.6.1` | Trace store (S3-backed) | 3200 API, 4317 OTLP |
| 7 | **Prometheus** | `prom/prometheus:v2.54.1` | Metrics TSDB (remote-write receiver) | 9090 |
| 8 | **Loki** | `grafana/loki:3.2.1` | Log store (S3-backed) | 3100 |
| 9 | **Grafana** | `grafana/grafana:11.2.0` | Single pane, 5 dashboards | 3000 → NodePort 30030 |
| 10 | **MinIO** | `minio/minio:latest` | S3-compatible object storage for Tempo+Loki | 9000 API, 9001 console |
| 11 | **Alertmanager** | `prom/alertmanager:v0.27.0` | Alert routing (no-op receiver locally) | 9093 |
| — | **HPA / NetworkPolicy** | — | Gateway autoscale + OTLP-only ingress | — |

---

## 1. `obs` — instrumentation library
**File:** [obs/obs/__init__.py](../obs/obs/__init__.py) · installed via the agent Dockerfile ([agent/Dockerfile:6](../agent/Dockerfile#L6))

**What it is.** A thin, reusable Python package that turns on OpenTelemetry for any agent in two lines
(`import obs; obs.init()`). One module, no app-visible exporter wiring.

**How it works here.** [`init()`](../obs/obs/__init__.py#L59) is idempotent and thread-safe (guarded by
`_lock` + `_initialized`). It builds three providers, each with an **OTLP/gRPC exporter** that reads the
standard `OTEL_*` env vars (so the endpoint is never in code):
- **Traces** — `TracerProvider` + `BatchSpanProcessor` ([__init__.py:88-91](../obs/obs/__init__.py#L88-L91)).
- **Metrics** — `MeterProvider` + `PeriodicExportingMetricReader` exporting every **10 s** ([__init__.py:94-95](../obs/obs/__init__.py#L94-L95)).
- **Logs** — `LoggerProvider` + `BatchLogRecordProcessor`, attached to the root logger so app logs become
  trace-correlated OTLP logs; a `_NoiseFilter` ([__init__.py:51-56](../obs/obs/__init__.py#L51-L56)) keeps
  `opentelemetry/urllib3/httpx/kafka/...` chatter out of the pipeline.

**Features.**
- **Auto-instrumentation** ([`_instrument_libraries`](../obs/obs/__init__.py#L138)) of the Anthropic SDK
  (OpenLLMetry) and httpx; `instrument_fastapi()` for HTTP server spans. Disable with
  `OBS_DISABLE_AUTOINSTRUMENT=1`.
- **No-duplicate-span design (H4).** When the Anthropic instrumentor is active it owns the LLM span, so
  [`llm_call`](../obs/obs/__init__.py#L209) records **metrics only**; in mock mode (no instrumentor) it
  creates the `chat <model>` span itself. Same telemetry shape either way.
- **Opt-in agent structure** via context managers: [`agent_run`](../obs/obs/__init__.py#L176) (run span +
  `agent.run.duration` + `agent.loop.iterations`), [`llm_call`](../obs/obs/__init__.py#L209) (tokens +
  `gen_ai.client.operation.duration`), [`tool_call`](../obs/obs/__init__.py#L239) (`agent.tool.calls` +
  ok/error outcome).
- **PII-safe by default (L3):** sets `TRACELOOP_TRACE_CONTENT=false` ([__init__.py:81](../obs/obs/__init__.py#L81))
  so prompt/response bodies are not captured unless opted in.
- **Clean shutdown (M5):** `atexit` flushes + shuts down all providers ([__init__.py:127-135](../obs/obs/__init__.py#L127-L135)).
- **Context propagation helpers:** `current_context()` / `use(ctx)` (cross-thread) and
  `inject_headers()` / `context_from_headers()` (cross-service W3C `traceparent`).

**Metrics emitted** (semantic-convention names): `gen_ai.client.token.usage` (counter, by
`gen_ai.token.type` input/output, model, system), `gen_ai.client.operation.duration` (histogram),
`agent.run.duration` (histogram), `agent.tool.calls` (counter, by `tool`+`outcome`),
`agent.loop.iterations` (histogram).

**Push it further.** Register more auto-instrumentors (LangChain, DB clients) inside `_instrument_libraries`;
flip `TRACELOOP_TRACE_CONTENT=true` locally to capture prompts; wrap async tool calls / sub-agent handoffs
with `use()` / `inject_headers()` (already exercised in `subagents.py`).

---

## 2. agent — the demo workload
**Files:** [agent/app.py](../agent/app.py), [agent/agent.py](../agent/agent.py), [agent/llm.py](../agent/llm.py), [agent/tools.py](../agent/tools.py), [agent/subagents.py](../agent/subagents.py)

**What it is.** A FastAPI service that runs a tool-calling agent loop, with a backend switch between real
Claude and a deterministic mock.

**Endpoints** ([app.py](../agent/app.py)):
- `GET /healthz` → `{status, llm_mode}`.
- `POST /chat {message, agent}` → single-agent loop.
- `POST /orchestrate {message, agent}` → coordinator fan-out to sub-agents.

**How it works here.**
- **Bootstrap is 2 lines** ([app.py:9,18](../agent/app.py#L9-L18)): `obs.init(...)` then
  `obs.instrument_fastapi(app)`. FastAPI is imported *after* `init()` so instrumentation attaches.
- **The loop** ([agent.py:15-53](../agent/agent.py#L15-L53)) runs up to `MAX_ITERS = 8` iterations, each
  wrapped in `obs.agent_run` → `obs.llm_call` → `obs.tool_call`, producing the trace tree
  `agent.run → chat → tool`. It calls `agent.iteration()` per turn (feeds the loop-iteration metric and the
  runaway-loop tail-sampling policy + alert).
- **Backend abstraction** ([llm.py](../agent/llm.py)): `get_client()` returns `ClaudeClient`
  (`anthropic.Anthropic`, model from `CLAUDE_MODEL`, default `claude-opus-4-8`, `max_tokens=1024`) or
  `MockClient`. Both return a normalized `StepResult`, so the loop and the emitted telemetry are identical
  across modes. The mock parses intent (weather/arithmetic) and emits the same Anthropic content-block shapes
  and synthetic token counts.
- **Tools** ([tools.py](../agent/tools.py)): `get_weather` (canned) and `calculate` (a *safe* AST evaluator —
  no `eval`, whitelisted operators). Anthropic-style JSON tool schemas in `TOOLS`.
- **Sub-agents** ([subagents.py](../agent/subagents.py)): a `coordinate()` coordinator submits each entry of
  the `SUBAGENTS` map to a shared bounded `ThreadPoolExecutor` (M6); each worker re-attaches the parent
  context via `obs.use(parent_ctx)` so concurrent sub-agent spans nest under the coordinator's run. Scale the
  fan-out by adding a row to `SUBAGENTS` — nothing else changes.

**Features.** Backend-agnostic telemetry (run with no API key/cost in mock); a faithful agent span tree;
multi-agent (`OTEL_SERVICE_NAME` per deployment) and sub-agent (per-thread context) demonstrations.

**Push it further.** Replace the mock/tools with a real harness; add a streaming `/chat`; add more tools to
enrich the trace tree.

---

## 3. OTEL Collector — Gateway tier (stateless)
**File:** [k8s/30-collector-gateway.yaml](../k8s/30-collector-gateway.yaml) · **Image:** `otelcol-local:dev`

**What it is.** The single front door for all telemetry. Every app pod sends OTLP here (enforced by
NetworkPolicy); it normalizes, protects, derives RED metrics, and ships to Kafka. Stateless → HPA-scalable.

**Pipelines** ([service.pipelines](../k8s/30-collector-gateway.yaml#L79-L91)):
- **traces:** `otlp` → `memory_limiter` → `resource/tier` → `transform/genai_semantics` → `redaction/pii` →
  `batch` → `kafka/traces` **+ `spanmetrics`** connector.
- **metrics:** `otlp` **+ `spanmetrics`** → `memory_limiter` → `resource/tier` → `batch` → `kafka/metrics`.
- **logs:** `otlp` → `memory_limiter` → `resource/tier` → `redaction/pii` → `batch` → `kafka/logs`.

**Features (where cross-cutting policy lives).**
- **`memory_limiter`** (80% limit / 25% spike) — sheds load instead of OOMing under spikes.
- **`transform/genai_semantics`** — semantic enforcement: backfills `gen_ai.system="anthropic"` when a span
  has a model but no system, so every team's spans are uniform and dashboardable ([line 29-34](../k8s/30-collector-gateway.yaml#L29-L34)).
- **`redaction/pii`** — scrubs emails and 13–16-digit card-like numbers from span + log attributes
  *before they leave the cluster* ([line 36-41](../k8s/30-collector-gateway.yaml#L36-L41)).
- **`spanmetrics` connector (C2)** — derives RED metrics from the **full, pre-sampling** span stream
  (namespace `agent`, dimensioned by `gen_ai.request.model` + `agent.name`), so rates/error-rates aren't
  biased by tail sampling downstream. This is why it lives in the gateway, not the consumer.
- **`kafka/traces` `partition_traces_by_id: true` (C1)** — all spans of a trace land on one partition, so the
  consumer can tail-sample correctly at scale.
- Tags every record with `telemetry.pipeline.tier=gateway` (`resource/tier`).

**Push it further.** Add mTLS + authn on the OTLP receiver; `groupbyattrs`/`attributes` to strip
high-cardinality metric keys; tenant tagging; a `filter` processor to drop noise early.

---

## 4. Kafka — durable buffer
**File:** [k8s/10-kafka.yaml](../k8s/10-kafka.yaml) · **Image:** `apache/kafka:3.8.0` (KRaft, single node)

**What it is.** A broker between the two collector tiers, with three topics: `otel-traces`, `otel-metrics`,
`otel-logs` (auto-created; `KAFKA_AUTO_CREATE_TOPICS_ENABLE=true`).

**Features.** Decoupling: absorbs spikes, survives backend outages (telemetry queues rather than dropping),
enables replay and multi-consumer fan-out. The gateway scales on ingest rate; the consumer scales on
partitions. Runs KRaft (no ZooKeeper); single-node here with RF=1.

**Push it further.** Multi-broker with RF ≥ 3; partition topics for parallel tail-sampling consumers;
retention tuning; a dead-letter topic for poison messages.

---

## 5. OTEL Collector — Consumer tier (stateful)
**File:** [k8s/31-collector-consumer.yaml](../k8s/31-collector-consumer.yaml) · **Image:** `otelcol-local:dev`

**What it is.** The stateful processing tier. Reads each topic from Kafka (consumer group `otel-consumer`),
makes whole-trace decisions, and fans out to the storage backends.

**Pipelines** ([service.pipelines](../k8s/31-collector-consumer.yaml#L95-L107)):
- **traces:** `kafka/traces` → `memory_limiter` → `tail_sampling` → `batch` → `otlp/tempo`.
- **metrics:** `kafka/metrics` → `memory_limiter` → `batch` → `prometheusremotewrite`.
- **logs:** `kafka/logs` → `memory_limiter` → `batch` → `otlphttp/loki`.

**Features.**
- **`tail_sampling`** ([line 39-57](../k8s/31-collector-consumer.yaml#L39-L57)) — needs the *complete* trace,
  so it sits after the buffer. Policy keeps every interesting run: `errors`, `latency > 2s`,
  `gen_ai.usage.output_tokens > 1000` (expensive), `agent.loop.iterations ≥ 8` (looping), then a
  probabilistic catch-all **set to 100% locally** (drop to 5–10% in prod).
- **`prometheusremotewrite` (H2)** — *pushes* metrics to Prometheus (`resource_to_telemetry_conversion`
  promotes resource attrs like `service_name` to labels), so there are no scrape gaps. (The Service still
  exposes `:8889`, but it is **vestigial** — no `prometheus` *exporter* is configured; metrics leave via
  remote-write.)
- **Exporters:** Tempo (OTLP/gRPC), Prometheus (remote-write), Loki (OTLP/HTTP). All `tls.insecure` locally.
- **Routing + cold storage** are documented inline as a commented PROD extension (`routing` connector +
  `awss3` exporter) — left out of the runnable path to avoid requiring AWS creds.

**Push it further.** Lower the catch-all sampling %; uncomment the routing/cold-storage block; add per-team
cost rollups; scale via **Kafka partitions**, not naive replicas (tail sampling is stateful per trace).

---

## 6. Tempo — trace store
**File:** [k8s/20-tempo.yaml](../k8s/20-tempo.yaml) · **Image:** `grafana/tempo:2.6.1`

**What it is.** Distributed tracing backend; receives sampled traces via OTLP at `:4317`, query API on
`:3200`. **Blocks are stored in object storage** (`backend: s3` → MinIO bucket `tempo`); only the WAL is on
the local PVC ([line 21-32](../k8s/20-tempo.yaml#L21-L32)). `block_retention: 24h`.

**Features.** See the agent "think": each `/chat` is a tree `agent.run → chat → tool`. Query by trace id or
TraceQL, e.g. `{ name="agent.run" && duration > 2s }`, `{ span.tool.name != "" && status = error }`. Linked
from Grafana for metric→trace→log navigation.

**Push it further.** Point `s3` at managed object storage; enable the metrics-generator for service graphs;
wire exemplars so Prometheus panels link to representative traces.

---

## 7. Prometheus — metrics store
**File:** [k8s/22-prometheus.yaml](../k8s/22-prometheus.yaml) · **Image:** `prom/prometheus:v2.54.1`

**What it is.** Time-series DB. Runs with `--web.enable-remote-write-receiver` (H2) and
`--enable-feature=exemplar-storage`. **Agent + spanmetrics metrics arrive via remote-write**; Prometheus only
*scrapes* the two collectors' `:8888` self-telemetry and itself ([scrape_configs](../k8s/22-prometheus.yaml#L16-L23)).
3Gi PVC, `fsGroup: 65534` for non-root writes.

**Features.**
- **Recording rules — usage/cost reporter** ([line 33-46](../k8s/22-prometheus.yaml#L33-L46)): precompute
  `agent:tokens_per_min:*` and **cost** `agent:cost_usd_per_min:{input,output,total}`, priced at Opus 4.8
  **$5/1M in, $25/1M out**, grouped by `service_name` + model so cost is attributed per agent.
- **Alert rules** ([line 48-95](../k8s/22-prometheus.yaml#L48-L95)): `HighLLMLatencyP95` (>10s),
  `HighToolFailureRate` (>20%), `AgentRunawayLoops` (p95 ≥ 8), `HighTokenBurnRate` (>100k/min), plus
  pipeline-health alerts `CollectorRefusingData`, `ExporterSendFailures`, `TargetDown`.

**Push it further.** Remote-write to Mimir/Thanos for long retention; add per-team cost rules; watch
cardinality (keep high-cardinality keys on spans, not metric labels).

---

## 8. Loki — log store
**File:** [k8s/21-loki.yaml](../k8s/21-loki.yaml) · **Image:** `grafana/loki:3.2.1`

**What it is.** Log backend; ingests OTLP logs at `/otlp/v1/logs`. **Chunks + TSDB index in object storage**
(MinIO bucket `loki`, schema v13) ([line 11-34](../k8s/21-loki.yaml#L11-L34)). `allow_structured_metadata`
and `volume_enabled` on.

**Features.** The third pillar that closes the loop: from a slow/errored span, jump to the actual log line
(trace-correlated by `trace_id`). Query by label, e.g. `{service_name="agentic-demo"}`.

**Push it further.** Add the routing pattern to send sensitive logs to a separate store; richer structured
metadata for filtering.

---

## 9. Grafana — single pane
**File:** [k8s/23-grafana.yaml](../k8s/23-grafana.yaml) · **Image:** `grafana/grafana:11.2.0` · Dashboards: [dashboards/](../dashboards/)

**What it is.** Unified visualization. Anonymous-admin (login disabled) for local use. **Four datasources
auto-provisioned**: Prometheus (default), Tempo, Loki, **Alertmanager**.

**Features.**
- **Trace↔log linking (M7):** the Loki datasource defines a `derivedFields` rule mapping `trace_id` →
  Tempo, and Tempo defines `tracesToLogsV2` → Loki ([line 22-35](../k8s/23-grafana.yaml#L22-L35)) — click
  across all three pillars.
- **Five auto-provisioned dashboards** (loaded from `dashboards/` by `make dashboard` into a ConfigMap):

| Dashboard | uid | Use it for |
|---|---|---|
| Agentic — OTEL Overview | `agent-overview` | Tokens, LLM/agent latency, tool success, logs |
| Agentic — Cost & Token Usage | `agent-cost-usage` | $/min by model, tokens in/out, cumulative, share |
| Agentic — Performance & Latency | `agent-performance` | LLM p50/p95/p99, agent-run p95, req rate, loop heatmap |
| Agentic — Pipeline Health | `agent-pipeline-health` | Accepted/refused/sent/failed spans, queue, memory, alerts |
| Agentic — Multi-Agent & Sub-Agents | (multi-agent.json) | Per-service + per-role (coordinator/sub-agents) |

**Push it further.** Add Grafana-managed alert rules + contact points; exemplar links; SSO for shared use.

---

## 10. MinIO — object storage (S3 stand-in)
**File:** [k8s/15-minio.yaml](../k8s/15-minio.yaml) · **Image:** `minio/minio:latest`

**What it is.** S3-compatible object storage (H1) backing **Tempo and Loki** — the production HA-backend
pattern at laptop scale. A one-shot `minio-mkbuckets` Job creates the `tempo` and `loki` buckets on startup.
Deployed first in `make backends` so the trace/log stores have a bucket to write to. 5Gi PVC; console on
`:9001`.

**Features.** Decouples trace/log durability from pod-local disk; makes the "blocks/chunks in object storage,
cheap cold replay" story real locally. Credentials are demo defaults (`minioadmin`/`minioadmin`).

**Push it further.** On a real cluster, delete MinIO and repoint Tempo/Loki at managed object storage
(S3/GCS) — a config change only.

---

## 11. Alertmanager — alert routing
**File:** [k8s/24-alertmanager.yaml](../k8s/24-alertmanager.yaml) · **Image:** `prom/alertmanager:v0.27.0`

**What it is.** Receives firing alerts from Prometheus (configured target `alertmanager:9093` in the
Prometheus `alerting` block). Locally it routes to a **no-op `default` receiver** (group by alertname,
10s wait, 1m group interval, 1h repeat).

**Features.** Turns the dashboards into something that can page you. Also surfaced as a Grafana datasource so
firing alerts show up in the Pipeline-Health dashboard.

**Push it further.** Add `slack_configs` / `webhook_configs` / `pagerduty_configs` receivers + routing /
inhibition rules in `alertmanager.yml`.

---

## 12. Platform, scaling & deploy

| Piece | File | Role |
|---|---|---|
| **kind cluster** | [kind/cluster.yaml](../kind/cluster.yaml) | 1 control-plane node; NodePorts 30030 (Grafana) + 30080 (agent) → localhost |
| **Makefile** | [Makefile](../Makefile) | `up` (cluster→images→backends→collectors→agent), `down`, `reload`, `load`, `verify`, `multi`, `collectors`, `dashboard`, `urls`, `logs` |
| **Agent config** | [k8s/00-namespace.yaml](../k8s/00-namespace.yaml) | `agent-config` ConfigMap: `LLM_MODE`, `CLAUDE_MODEL`, `OTEL_*`. Agent reads it via `envFrom` |
| **Agent Deployment** | [k8s/40-agent.yaml](../k8s/40-agent.yaml) | `imagePullPolicy: Never`; optional `anthropic` Secret; `/healthz` readiness; NodePort 30080 |
| **Multi-agent** | [k8s/41-agents-extra.yaml](../k8s/41-agents-extra.yaml) | `support-agent` (2nd `OTEL_SERVICE_NAME`) + in-cluster `loadgen` driving `/chat` + `/orchestrate` |
| **HPA** | [k8s/50-hpa.yaml](../k8s/50-hpa.yaml) | Gateway 1→5 replicas at 70% CPU (stateless tier only) |
| **NetworkPolicy** | [k8s/60-networkpolicy.yaml](../k8s/60-networkpolicy.yaml) | Only the gateway accepts OTLP (4317/4318) from app pods; agents can't reach Kafka/backends directly |
| **Collector image** | [collector/Dockerfile](../collector/Dockerfile) | Pins contrib **0.119.0** (0.116.0 ships a broken binary); built + `kind load`-ed |

**macOS/kind build note.** Both images are built single-arch and loaded via `docker save | kind load
image-archive` with `imagePullPolicy: Never`, sidestepping Docker Desktop's containerd-store
attestation-manifest selection bug. (Details in the Makefile `image` / `collector-image` targets.)

---

## 13. Cross-cutting features — where "production-grade" lives

| Feature | Where (code) | What it buys |
|---|---|---|
| GenAI semantic conventions | `obs` + gateway `transform/genai_semantics` | Vendor-neutral, uniform `gen_ai.*` → portable dashboards |
| Tail sampling | consumer `tail_sampling` | Keep every interesting run, sample the rest → cost vs. coverage |
| PII redaction | gateway `redaction/pii` | Scrub emails/cards before storage; content capture off by default |
| Spanmetrics (RED) | gateway `spanmetrics` (pre-sampling) | Unbiased rate/error/duration from spans, free |
| Two-tier + Kafka | gateway / Kafka / consumer | Resilience, independent scaling, replay |
| Object-storage backends | MinIO ← Tempo + Loki | HA-shaped durability; swap to managed S3 via config |
| Push metrics (remote-write) | consumer → Prometheus | No scrape gaps for app metrics |
| Cost as a metric | Prometheus recording rules | Per-service/model $/min, queryable + cheap to chart |
| Alerting | Prometheus rules → Alertmanager | Latency/error/cost/runaway/pipeline pages |
| Trace↔log correlation | Grafana derived fields + Tempo↔Loki | One click across all three pillars |
| Env-only config (12-factor) | everywhere | Same image local↔prod; repoint backends via config, never code |
| Pipeline self-observability | Prometheus scrape of collectors `:8888` | Watch the observability pipeline itself |
| Autoscale + isolation | HPA + NetworkPolicy | Stateless gateway scales; OTLP-only ingress |
