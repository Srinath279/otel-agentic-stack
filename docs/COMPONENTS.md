# Component Reference

Every component in the stack: what it is, how it works *here*, how it helps, what to use it for, and
how to push it to full capability. Data flows left→right:

```
agent (obs SDK) ──OTLP──▶ gateway collector ──▶ Kafka ──▶ consumer collector ──▶ Tempo / Prometheus / Loki ──▶ Grafana
```

---

## 1. `obs` — instrumentation library
**Files:** [obs/obs/__init__.py](../obs/obs/__init__.py) · **Image:** baked into the agent

**What it is.** A thin, reusable Python package that turns on OpenTelemetry for any agent harness in
two lines (`import obs; obs.init()`). It wires the OTEL SDK (traces + metrics + logs), auto-instruments
the Anthropic SDK via OpenLLMetry, and exposes opt-in context managers for agent structure.

**How it works here.** `init()` builds a `TracerProvider`, `MeterProvider`, and `LoggerProvider`, each
with an OTLP gRPC exporter pointed at `OTEL_EXPORTER_OTLP_ENDPOINT` (the gateway collector). It then
calls `AnthropicInstrumentor().instrument()` so real LLM calls emit GenAI spans automatically. The
context managers (`agent_run`, `llm_call`, `tool_call`) create the span tree and record the custom
metrics. App logs are attached to the OTLP log pipeline, correlated by trace/span id.

**How it helps.** It's the "minimal code change" promise — teams get production-grade telemetry without
touching exporter wiring or backend URLs. Because everything reads from env, the *same image* runs
locally and in prod; you repoint backends by changing config, never code.

**Use it for.** Instrumenting any Python agent/harness. Drop the package in, call `init()`, optionally
decorate agent steps. For non-Python, the same pattern applies via that language's OTEL SDK + OpenLLMetry.

**Full capability.** Add `obs.use(ctx)` / `inject_headers()` around async tool calls and sub-agent
handoffs (already provided) to keep distributed traces intact. Extend `init()` to register more
auto-instrumentors (LangChain, httpx, DB clients). Gate prompt/response capture behind a flag
(`TRACELOOP_TRACE_CONTENT`) for PII control.

---

## 2. agent — the demo workload
**Files:** [agent/app.py](../agent/app.py), [agent/agent.py](../agent/agent.py), [agent/llm.py](../agent/llm.py), [agent/tools.py](../agent/tools.py)

**What it is.** A FastAPI service exposing `POST /chat`. It runs an 8-iteration tool-calling loop with
two tools (`get_weather`, `calculate`) and a configurable backend: real Claude (`claude-opus-4-8`) or a
deterministic mock (no API key/cost).

**How it works here.** `app.py` calls `obs.init()` + `obs.instrument_fastapi(app)` (the only
instrumentation code). `agent.py` wraps each run in `obs.agent_run`, each model call in `obs.llm_call`,
each tool in `obs.tool_call`. `llm.py` normalizes Claude and mock into one `StepResult`, so the loop is
backend-agnostic and both paths emit identical telemetry.

**How it helps.** It's a faithful but minimal stand-in for a real agent — it produces the exact span
tree and GenAI metrics a production agent would, so the pipeline is exercised end-to-end without
needing your real workload or an API key.

**Use it for.** Demoing/testing the observability pipeline; as a template for instrumenting your own
agent (copy the `obs` usage pattern). Flip `LLM_MODE=claude` to test real-LLM telemetry (tokens, model
attributes, OpenLLMetry spans).

**Full capability.** Swap the mock/tools for your real agent. Add more tools to see richer trace trees.
Add a `/chat` streaming variant. Add sub-agents to exercise multi-thread trace propagation.

---

## 3. OTEL Collector — Gateway tier
**Files:** [k8s/30-collector-gateway.yaml](../k8s/30-collector-gateway.yaml) · **Image:** `otelcol-local:dev` (built from contrib 0.119.0)

**What it is.** The stateless front door for all telemetry. Every app pod sends OTLP here (never to a
backend directly). It normalizes, protects, redacts, and forwards to Kafka.

**Pipeline (per signal):** `otlp` receiver → `memory_limiter` → `resource/tier` → `transform`
(semantic enforcement) → `redaction` (PII) → `batch` → `kafka` exporter.

**How it helps.** This is where cross-cutting policy lives — so app teams stay dumb and the platform
owns consistency. Key processors:
- **`memory_limiter`** — sheds load instead of OOMing under spikes (the slide's load-shed).
- **`transform` (semantic enforcement)** — backfills `gen_ai.system` so every team's spans are uniform;
  you can't dashboard what isn't normalized.
- **`redaction`** — scrubs emails / card-like numbers from attributes before they leave the cluster.
- **`batch`** — amortizes network/CPU.

**Use it for.** A stateless, horizontally-scalable ingestion + enrichment + protection layer. Run it as
a Deployment (autoscaled, see [50-hpa.yaml](../k8s/50-hpa.yaml)) or as a per-node DaemonSet/sidecar so
apps always export to `localhost`.

**Full capability.** Add mTLS + authn on the OTLP receiver. Add `attributes`/`groupbyattrs` to strip
high-cardinality keys off metrics. Add tenant tagging. Add a `filter` processor to drop noise early.

---

## 4. Kafka — durable buffer
**Files:** [k8s/10-kafka.yaml](../k8s/10-kafka.yaml) · **Image:** `apache/kafka:3.8.0` (KRaft, single node)

**What it is.** A message broker sitting between the gateway and consumer tiers, with three topics
(`otel-traces`, `otel-metrics`, `otel-logs`).

**How it helps.** Decoupling. It absorbs traffic spikes, survives backend outages (telemetry queues
instead of dropping), enables replay, and lets multiple consumers fan out. This is the boundary that
turns a fragile single pipeline into a resilient one — the core of the DoorDash design.

**Use it for.** Any pipeline where you can't afford to lose telemetry during a backend blip, or where
ingestion and processing need to scale independently. The gateway can scale on ingest rate while the
consumer scales on Kafka partitions.

**Full capability.** Multi-broker cluster with replication factor ≥3 for durability. Partition the
topics for parallel tail-sampling consumers. Add retention tuning. Add a dead-letter topic for poison
messages. Front it with schema/size limits.

---

## 5. OTEL Collector — Consumer tier
**Files:** [k8s/31-collector-consumer.yaml](../k8s/31-collector-consumer.yaml) · **Image:** `otelcol-local:dev`

**What it is.** The stateful processing tier. It reads from Kafka, makes whole-trace decisions
(tail sampling), derives metrics from spans, and fans out to the storage backends.

**Pipelines:**
- traces: `kafka` → `memory_limiter` → `tail_sampling` → `batch` → `otlp/tempo` + `spanmetrics`
- metrics: `kafka` + `spanmetrics` → `batch` → `prometheus`
- logs: `kafka` → `batch` → `otlphttp/loki`

**How it helps.** This is where the expensive, stateful intelligence lives:
- **`tail_sampling`** — needs the *complete* trace in memory, so it must be after the buffer. Policy:
  always keep `error`, `latency > 2s`, `output_tokens > 1000`, `agent.loop.iterations ≥ 8`; sample the
  rest at 10%. This is the #1 cost-vs-coverage lever for agent traces (which are span-heavy).
- **`spanmetrics` connector** — derives RED metrics (rate/errors/duration) from spans, dimensioned by
  model and agent — free latency/throughput metrics without extra instrumentation.
- **exporters** — Tempo (traces), Prometheus (metrics), Loki (logs).

**Use it for.** Centralized sampling, cost/usage aggregation, and routing. Keep it as a small, ordered
deployment; scale via Kafka partitions, not naive replicas (tail sampling is stateful per-trace).

**Full capability.** Add a **usage/cost reporter** (`groupbyattrs` + token metrics → per-team/model
rollups). Add the **`routing` connector** to split by tenant/sensitivity (recipe is inlined in the
file). Add an **`awss3` exporter** for cheap cold-storage replay. Tune the tail-sampling policy set.

---

## 6. Tempo — trace store
**Files:** [k8s/20-tempo.yaml](../k8s/20-tempo.yaml) · **Image:** `grafana/tempo:2.6.1`

**What it is.** A distributed tracing backend. Receives sampled traces via OTLP, stores and indexes them
for query by trace id or TraceQL.

**How it helps.** This is where you *see the agent think* — each `/chat` becomes a tree: `agent.run` →
`chat` (per LLM call) → `tool` (per tool). You pinpoint the slow step, spot loops, and read tool
failures. TraceQL examples: `{ name="agent.run" && duration > 2s }`, `{ span.tool.name != "" && status = error }`.

**Use it for.** Debugging agent behavior, latency analysis, and seeing exactly what an agent did on a
given request. Linked from Grafana so you click a metric spike straight into the trace.

**Full capability.** Back it with object storage (S3/GCS) instead of local disk for retention. Enable
the metrics-generator for service graphs. Wire exemplars so Prometheus panels link to representative
traces.

---

## 7. Prometheus — metrics store
**Files:** [k8s/22-prometheus.yaml](../k8s/22-prometheus.yaml) · **Image:** `prom/prometheus:v2.54.1`

**What it is.** A time-series database that scrapes the consumer collector's `:8889/metrics` (your
agent metrics + spanmetrics) and the collectors' own `:8888` self-telemetry.

**How it helps.** Aggregate, queryable metrics over time: token consumption (→cost), LLM/agent latency
percentiles, tool success rates, loop iterations, plus collector health (dropped spans, queue depth).
The numbers you alert and dashboard on.

**Key metrics emitted by `obs`:** `gen_ai_client_token_usage_total`, `gen_ai_client_operation_duration_seconds`,
`agent_run_duration_seconds`, `agent_tool_calls_total`, `agent_loop_iterations`.

**Use it for.** Dashboards, alerting, cost tracking, capacity planning, and SLOs.

**Full capability.** Remote-write to a durable, horizontally-scalable backend (Mimir/Thanos) for long
retention. Add recording rules for cost rollups. Add Alertmanager (rules listed in PLAN.md §4) for
latency/error/cost/runaway alerts. Watch cardinality — keep high-cardinality keys on spans, not metric labels.

---

## 8. Loki — log store
**Files:** [k8s/21-loki.yaml](../k8s/21-loki.yaml) · **Image:** `grafana/loki:3.2.1`

**What it is.** A log aggregation backend that ingests OTLP logs (the agent's structured logs) at
`/otlp/v1/logs`, labeled by resource attributes (e.g. `service_name`).

**How it helps.** The third pillar — and the one that closes the loop. When a span is slow or errored,
you jump from the trace to the actual log line (trace-correlated via trace/span id). Without logs you
see *that* something failed but not the *message*.

**Use it for.** Reading what the agent logged during a run, correlating logs↔traces, and querying by
label (`{service_name="agentic-demo"}`).

**Full capability.** Back chunks with S3 for retention/cold replay (the slide's S3 path). Add structured
metadata for richer filtering. Add the routing pattern to send sensitive logs to a separate store.

---

## 9. Grafana — single pane
**Files:** [k8s/23-grafana.yaml](../k8s/23-grafana.yaml), [dashboards/agent-overview.json](../dashboards/agent-overview.json) · **Image:** `grafana/grafana:11.2.0`

**What it is.** The unified visualization layer. Pre-provisioned with three datasources (Prometheus,
Tempo, Loki) and the "Agentic — OTEL Overview" dashboard.

**How it helps.** One place to see all three pillars: metrics panels, trace search, and logs — with
cross-links (metric spike → trace → logs). Anonymous-admin for local use.

**Use it for.** Day-to-day monitoring and debugging. The dashboard shows token rate, LLM/agent p95,
tool success rate, and a live logs panel. Explore → Tempo for trace search.

**Full capability.** Add alert rules + contact points. Add cost/usage and per-tenant dashboards. Add
exemplar links from metrics to traces. Lock down auth (SSO) for shared/prod use.

---

## 10. Cross-cutting concepts (where the "production-grade" lives)

| Concept | Where | What it buys you |
|---|---|---|
| **GenAI semantic conventions** | `obs` + gateway `transform` | Vendor-neutral, uniform `gen_ai.*` attributes → portable dashboards |
| **Tail sampling** | consumer | Keep every interesting agent run, sample the boring ones → cost control without losing signal |
| **PII redaction** | gateway `redaction` | Scrub sensitive data before storage |
| **Spanmetrics** | consumer connector | RED metrics from spans for free |
| **Two-tier + Kafka** | gateway/consumer/kafka | Resilience, independent scaling, replay |
| **Env-only config** | everywhere | Same image local↔prod; repoint backends via config, never code |
| **Collector self-telemetry** | Prometheus scrape of `:8888` | Observe the observability pipeline (dropped/refused data) |

---

## 11b. Alerting & cost reporting
**Files:** [k8s/24-alertmanager.yaml](../k8s/24-alertmanager.yaml), rules in [k8s/22-prometheus.yaml](../k8s/22-prometheus.yaml)

**What it is.** Prometheus **recording rules** precompute token rates and **cost** (`agent:cost_usd_per_min:*`,
priced at Opus 4.8 $5/$25 per 1M in/out) — this is the "usage/cost reporter". Prometheus **alert rules**
fire on LLM p95 latency, tool failure rate, runaway loops, token burn, and pipeline health (refused data,
send failures, target down). **Alertmanager** receives firing alerts (local: no-op receiver; prod: add
Slack/PagerDuty/webhook).

**How it helps.** Cost becomes a first-class, queryable metric (per model, in/out split). Alerts turn the
dashboards into something that pages you instead of something you have to watch. Recording rules also make
cost panels cheap (no heavy query at view time).

**Full capability.** Add Slack/webhook receivers + routing/inhibition in `alertmanager.yml`. Add per-team
cost rules (group by a `team` resource attribute). Move cost rollups into the collector for multi-backend portability.

## 11c. Dashboards (one per concern)
**Files:** [dashboards/](../dashboards/)

| Dashboard | uid | Use it for |
|---|---|---|
| **Agentic — OTEL Overview** | `agent-overview` | At-a-glance: tokens, latency, tool success, logs |
| **Agentic — Cost & Token Usage** | `agent-cost-usage` | $/min by model, tokens in/out, cumulative, token share |
| **Agentic — Performance & Latency** | `agent-performance` | LLM p50/p95/p99, agent-run p95, req rate, calls/run, loop heatmap |
| **Agentic — Pipeline Health** | `agent-pipeline-health` | Collector accepted/refused/sent/failed spans, queue, memory, firing alerts |

All auto-provisioned (`make dashboard` loads the whole `dashboards/` dir). In Grafana: ☰ → Dashboards.

---

## 11. Platform & deploy

| Piece | Files | Role |
|---|---|---|
| **kind** | [kind/cluster.yaml](../kind/cluster.yaml) | Local k8s with NodePorts (Grafana 30030, agent 30080) |
| **Makefile** | [Makefile](../Makefile) | `up` / `down` / `reload` / `load` / `verify` orchestration; builds + archive-loads images |
| **Helm values** | [helm/](../helm/) | Prod deploy shape — same collector configs via the upstream chart, one release per tier |
| **NetworkPolicy / HPA** | [k8s/50](../k8s/50-hpa.yaml), [k8s/60](../k8s/60-networkpolicy.yaml) | Gateway autoscaling + OTLP-only ingress |

**Local-on-Mac note:** the collector image is built into a single-arch wrapper and loaded via
`docker save | kind load image-archive`, with `imagePullPolicy: Never` — this sidesteps Docker
Desktop's containerd-store manifest-selection bug. Pinned to contrib **0.119.0** (0.116.0 ships a
broken binary). See the Makefile `collector-image` / `image` targets.
