# OTEL for Agentic Workloads — E2E Plan

Local, end-to-end OpenTelemetry pipeline for a Python agent, running on **kind** (Kubernetes),
exporting to **Tempo** (traces) + **Prometheus** (metrics) + **Grafana** (view), with the
**OTEL SDK instrumentation reactive to the platform** (auto-instruments the Anthropic SDK via
OpenLLMetry, plus manual GenAI-convention spans/metrics so the mock path emits the same shape).

Stack choices (locked): Python · kind · Tempo+Grafana+Prometheus · configurable Claude/mock agent.

---

## 1. End-to-End Components

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  kind cluster  (namespace: otel-demo)                                          │
│                                                                                │
│   ┌────────────────┐   OTLP/gRPC 4317    ┌─────────────────────────┐           │
│   │  agent (pod)   │ ──────────────────► │  otel-collector (pod)   │           │
│   │  FastAPI +     │   traces + metrics  │  receivers: otlp        │           │
│   │  OTEL SDK +    │                     │  processors: batch,     │           │
│   │  OpenLLMetry   │                     │    memory_limiter,      │           │
│   │  (Anthropic    │                     │    resource             │           │
│   │   auto-instr.) │                     │  exporters:             │           │
│   └────────────────┘                     │    otlp→tempo (traces)  │           │
│        │ NodePort 30080                  │    prometheus :8889     │           │
│        ▼ /chat                           └───────────┬─────────────┘           │
│   (load generator)                          traces   │   metrics (scrape)      │
│                                                ▼      │                         │
│                                       ┌────────────┐  │  ┌────────────────┐     │
│                                       │   tempo    │  └─►│  prometheus    │     │
│                                       │  :3200 API │     │  :9090 scrapes │     │
│                                       │  :4317 OTLP│     │  collector:8889│     │
│                                       └─────┬──────┘     └───────┬────────┘     │
│                                             │  query             │ query        │
│                                             ▼                    ▼              │
│                                       ┌──────────────────────────────────┐     │
│                                       │  grafana :3000 (NodePort 30030)  │     │
│                                       │  datasources: Tempo + Prometheus │     │
│                                       │  dashboards: agent overview      │     │
│                                       └──────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────────────┘
```

| # | Component | Image | Role | Port(s) |
|---|-----------|-------|------|---------|
| 1 | **Agent** | built locally (`agent/Dockerfile`) | Instrumented Python agent (FastAPI `/chat`); runs a tool-using loop; calls Claude or mock | 8080 → NodePort 30080 |
| 2 | **OTEL Collector** | `otel/opentelemetry-collector-contrib` | Receive OTLP, batch/limit/enrich, fan out to Tempo + Prometheus | 4317 (gRPC), 4318 (HTTP), 8889 (prom metrics), 13133 (health) |
| 3 | **Tempo** | `grafana/tempo` | Trace storage + query (monolithic, local backend) | 3200 (API), 4317 (OTLP in) |
| 4 | **Prometheus** | `prom/prometheus` | Scrapes the collector's `:8889/metrics`; stores time series | 9090 |
| 5 | **Grafana** | `grafana/grafana` | Single pane: Tempo (traces) + Prometheus (metrics), provisioned dashboards | 3000 → NodePort 30030 |
| 6 | **Load generator** | `scripts/load.sh` (curl loop) | Drives `/chat` so there's live telemetry to look at | — |

### Instrumentation layer (the "reactive SDK" part)
- **`telemetry.py`** — sets up the OTEL SDK: `TracerProvider` + `MeterProvider` with OTLP exporters,
  resource attributes (`service.name`, `service.version`, `deployment.environment`) from env. All
  endpoints come from `OTEL_EXPORTER_OTLP_ENDPOINT` — **zero code change to repoint backends.**
- **OpenLLMetry (`traceloop-sdk`)** — auto-patches the Anthropic SDK so real Claude calls emit
  GenAI spans (model, prompt/completion, token usage) without hand-instrumentation. Guarded import:
  if absent, the manual layer still runs.
- **Manual GenAI conventions** — each LLM step and tool call gets an explicit span
  (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens`) plus
  metrics, so the **mock** path produces the same telemetry shape as the real one.
- **FastAPI instrumentation** — auto HTTP server spans, so the agent run nests under the request.

---

## 2. Setup (prerequisites + one-time)

**Prereqs:** Docker, `kind`, `kubectl`, and (optional) an `ANTHROPIC_API_KEY` for real Claude calls.

```bash
brew install kind kubectl            # if not present
cp .env.example .env                 # set LLM_MODE=mock (default) or claude + key
```

Repo layout:
```
otel-agentic-stack/
├── PLAN.md                  ← this file
├── Makefile                 ← up / down / build / load / urls
├── .env.example
├── kind/cluster.yaml        ← 1 control-plane node + port mappings (30030, 30080)
├── agent/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── telemetry.py         ← OTEL SDK + OpenLLMetry bootstrap
│   ├── llm.py               ← ClaudeClient | MockClient (LLM_MODE switch)
│   ├── tools.py             ← get_weather, calculate
│   ├── agent.py             ← agentic loop + spans/metrics
│   └── app.py               ← FastAPI /chat, /healthz
├── k8s/
│   ├── namespace.yaml
│   ├── otel-collector.yaml  ← ConfigMap + Deployment + Service
│   ├── tempo.yaml
│   ├── prometheus.yaml
│   ├── grafana.yaml         ← datasources + dashboard provisioning
│   └── agent.yaml           ← Deployment + Service(NodePort) + Secret ref
├── dashboards/agent-overview.json
└── scripts/load.sh
```

**Config surface (env, set on the agent Deployment):**

| Env var | Default | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | Where the SDK ships OTLP |
| `OTEL_SERVICE_NAME` | `agentic-demo` | Resource `service.name` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=local` | Extra resource tags |
| `LLM_MODE` | `mock` | `mock` (no key/cost) or `claude` |
| `ANTHROPIC_API_KEY` | — | from a k8s Secret, only when `LLM_MODE=claude` |
| `CLAUDE_MODEL` | `claude-opus-4-8` | model id for real calls |

---

## 3. Deployment (bring-up order matters)

`make up` runs these in sequence (each waits for readiness before the next):

1. **`kind create cluster --config kind/cluster.yaml`** — cluster with NodePorts mapped to localhost.
2. **`kubectl apply -f k8s/namespace.yaml`**.
3. **Backends first** (so the collector has somewhere to push):
   `kubectl apply -f k8s/tempo.yaml -f k8s/prometheus.yaml -f k8s/grafana.yaml`
   then `kubectl rollout status` on each.
4. **Collector** — `kubectl apply -f k8s/otel-collector.yaml`; wait for the `13133` health check.
5. **Build + load the agent image into kind** (kind can't pull from your local Docker daemon
   automatically): `docker build -t agentic-demo:dev agent/ && kind load docker-image agentic-demo:dev`.
6. **Agent** — `kubectl apply -f k8s/agent.yaml`; `rollout status`.
7. **`make load`** — fire requests at `localhost:30080/chat` to generate traces + metrics.

Teardown: `make down` → `kind delete cluster`.

> Mirrors the DoorDash "composable pipeline" idea at small scale: one collector image, config
> driven by a ConfigMap (their `values.yaml` per gateway/consumer), deployed as a K8s workload.
> To grow toward their shape later: split into **agent-sidecar collectors** (per-pod, fast local
> batching) + a **gateway collector** (central, tail-sampling + export) — see §5.

---

## 4. Monitor (what you actually watch)

### Traces (Grafana → Tempo)
- One **root span per `/chat` request**; nested **agent-run** span; child spans per **LLM call**
  and per **tool call**. This is how you see the agent's reasoning tree, loops, and which step is slow.
- Search by `service.name=agentic-demo`, filter `gen_ai.request.model`, sort by duration.
- TraceQL examples to save:
  - Slow runs: `{ name="agent.run" && duration > 2s }`
  - Tool failures: `{ span.tool.name != "" && status = error }`

### Metrics (Grafana → Prometheus), emitted by the agent + surfaced via collector `:8889`
| Metric | Type | Why |
|---|---|---|
| `gen_ai_client_token_usage` (input/output) | counter | token consumption → **cost** |
| `gen_ai_client_operation_duration` | histogram | LLM latency p50/p95/p99 |
| `agent_run_duration` | histogram | end-to-end agent latency |
| `agent_tool_calls_total{tool,outcome}` | counter | tool success/failure & retries |
| `agent_loop_iterations` | histogram | catch runaway/looping agents |
| `http_server_*` (FastAPI auto) | — | request rate / errors |

### Dashboard (`dashboards/agent-overview.json`, auto-provisioned)
Panels: request rate & error %, agent-run p95, LLM latency p95 by model, **tokens/min (in vs out)**,
estimated cost (tokens × price), tool success rate, loop-iteration heatmap, plus a **Tempo trace
panel** linked from any spike (exemplars → click into the trace).

### Alerts (Grafana alert rules, starter set)
- LLM p95 latency > 10s for 5m.
- Tool failure rate > 20% for 5m.
- Agent loop iterations p95 > 8 (runaway guard).
- Token burn rate > N/min (cost guard).

### Health checks
- Collector: `:13133`. Tempo: `:3200/ready`. Prometheus: `:9090/-/healthy`. Agent: `/healthz`.
- `make urls` prints: Grafana `http://localhost:30030` (anon admin), Agent `http://localhost:30080`.

---

## 5. Dev-flow integration (how this plugs into daily work)

1. **Local loop:** `make up` once; iterate on `agent/` → `make build && make reload` (rebuild image,
   `kind load`, `kubectl rollout restart`). Traces/metrics appear in Grafana within seconds.
2. **Pre-commit:** keep instrumentation honest — a tiny test asserts every `/chat` produces a root
   span with `gen_ai.usage.*` set (catches "we stopped emitting tokens" regressions).
3. **CI:** build the agent image; spin an ephemeral kind cluster (or just run the collector +
   agent with `docker compose` for a smoke test) and assert spans/metrics land.
4. **Prod parity / scale-up path (DoorDash-style):**
   - Promote the single collector to **sidecar (per-pod) + gateway (central)** tiers.
   - Add **tail sampling** at the gateway (keep all errored/slow/expensive agent runs, sample the rest).
   - Swap exporters in the gateway config only (Tempo→managed tracing, Prometheus→remote-write) —
     **no app change**, because the app only knows `OTEL_EXPORTER_OTLP_ENDPOINT`.
   - Gate full prompt/response capture behind an env flag; truncate/redact by default (PII + volume).

---

## 6. Target Architecture (Production-Grade)

The local demo is **tier 0** (one collector). Production splits into the DoorDash-style
**two-tier collector pipeline** with a durable buffer between them, so the app stays dumb and the
hard work (sampling, cost, routing, redaction) lives in config you can change without redeploying agents.

```
                          APPLICATION TIER (agents / harnesses — instrumented once)
  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      every pod points at localhost:4317
  │ agent A pod │  │ agent B pod │  │ harness pod │      (a sidecar/DaemonSet collector),
  │  app + OTEL │  │  app + OTEL │  │  app + OTEL │      NEVER at a backend URL directly.
  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
         │ OTLP           │ OTLP           │ OTLP
         ▼                ▼                ▼
  ┌──────────────────────────────────────────────────┐   GATEWAY TIER (stateless, horizontally scaled)
  │  Gateway Collector  (Deployment + HPA, N replicas)│   - receive OTLP/gRPC+HTTP (mTLS, authn)
  │  resource/attrs · semantic enforcement · batch ·  │   - normalize gen_ai.* (semantic enforcement)
  │  memory_limiter (load-shed) · redaction · groupby │   - redact PII, drop high-cardinality labels
  └───────────────────────────┬──────────────────────┘   - HEAD-decisions only; no stateful sampling
                              │ Kafka exporter (OTLP-over-Kafka)
                              ▼
                    ┌───────────────────┐                 DURABLE BUFFER
                    │   Kafka  (topics: │                 - decouples spikes, survives backend outages
                    │   traces/metrics/ │                 - replay + multi-consumer fan-out
                    │   logs)           │                 - this is the slide's Kafka boundary
                    └─────────┬─────────┘
                              │ Kafka receiver
                              ▼
  ┌──────────────────────────────────────────────────┐   CONSUMER TIER (stateful, ordered work)
  │  Consumer Collector (Deployment, partition-aware) │   - TAIL SAMPLING (needs whole trace in memory)
  │  tail_sampling · usage/cost reporter · routing    │   - usage reporter → per-team/model/tenant cost
  └───┬──────────────┬───────────────┬────────────────┘   - routing by signal/tenant/sensitivity
      │ traces        │ metrics        │ logs + cold
      ▼               ▼                ▼
 ┌─────────┐   ┌──────────────┐   ┌──────────────┐         BACKENDS (swap via exporter config only)
 │ Tempo / │   │ Mimir/Thanos │   │ Loki + S3    │         - traces store (object-backed)
 │ vendor  │   │ (remote-write│   │ (cold replay)│         - durable metrics TSDB
 └────┬────┘   └──────┬───────┘   └──────┬───────┘         - logs + cheap cold storage (slide's S3)
      └───────────────┴──────────────────┘
                      ▼
              ┌────────────────┐
              │    Grafana     │  unified traces+metrics+logs, exemplars, alerting → Alertmanager
              └────────────────┘
```

**What lives where (and why the app never changes):**

| Concern | Tier | Collector processor / mechanism |
|---|---|---|
| Receive, batch, protect from overload | Gateway | `otlp` receiver, `batch`, `memory_limiter` (load-shed) |
| Enforce GenAI conventions across teams | Gateway | `transform` / schema processor ("semantic enforcement") |
| PII redaction, prompt truncation | Gateway | `redaction` / `transform` (gated by attr/flag) |
| Strip high-cardinality keys off metrics | Gateway | `attributes` / `groupbyattrs` |
| Durable buffer, spike absorption, replay | Buffer | Kafka exporter → Kafka receiver |
| Tail sampling (keep errored/slow/expensive/looping runs) | Consumer | `tail_sampling` with policy set |
| Per-team / model / tenant cost rollups | Consumer | usage/`spanmetrics` + `groupbyattrs` → metrics |
| Route by signal / tenant / sensitivity | Consumer | `routing` connector → N exporters |
| Persist + visualize | Backends | `otlp`/`prometheusremotewrite`/`loki`/`awss3` exporters |

**Tail-sampling policy (agent-specific starter):** always keep traces that are `error`,
`duration > p95`, `gen_ai.usage.output_tokens > threshold`, or `agent.loop.iterations > N`;
probabilistically sample the rest at e.g. 5%. This is the single biggest lever for cost vs. coverage.

**Deployment shape:** packaged as **one custom collector image** + **one Helm chart**, rendered into
**per-tier `values.yaml`** (gateway values, consumer values) — exactly the slide's model. App
workloads ship a **DaemonSet/sidecar collector** so they always export to `localhost`, decoupling
them from gateway DNS/topology. HA: gateway behind HPA, consumer partition-aware, backends
object-storage-backed. Security: mTLS + authn on OTLP receivers, NetworkPolicies, secrets via
external-secrets.

---

## 7. Integration: OTEL with Minimal Code Changes

Design goal: **a team adopts full agent observability by adding one dependency and one import —
or zero code at all.** Everything else (endpoints, sampling, redaction, routing) is env + collector
config, owned by the platform, not the app.

### 7.1 The instrumentation contract — three layers, decreasing code touch

```
Layer 0 — ZERO code:   `opentelemetry-instrument python app.py`   (runtime auto-instrument)
Layer 1 — ONE import:  `import obs; obs.init()`                    (bootstrap + LLM auto-patch)
Layer 2 — DECORATORS:  `@agent` / `@tool` / `with obs.span(...)`   (only for richer agent structure)
```

- **Layer 0 (no app change).** OTEL's Python auto-instrumentation hooks library entrypoints at
  startup. `opentelemetry-instrument` (or the `opentelemetry-operator` auto-injection in k8s, via a
  pod annotation) instruments FastAPI/Flask, `requests`/`httpx`, gRPC, DB clients — **without editing
  the app**. In k8s this becomes a single annotation:
  `instrumentation.opentelemetry.io/inject-python: "true"`.
- **Layer 1 (one line).** A shared internal package — call it `obs` — wraps OTEL SDK + OpenLLMetry:
  `Traceloop.init()` auto-patches the **Anthropic SDK** (and OpenAI, LangChain, LlamaIndex, vector
  DBs) so every LLM call, tool call, and chain emits GenAI spans with model, prompt/completion, and
  token usage. The app calls `obs.init()` once at process start. This is the "reactive to the
  platform" piece — instrumentation attaches to whatever LLM client the harness already uses.
- **Layer 2 (opt-in).** Decorators/context managers to name agent-level structure
  (`@agent("planner")`, `@tool("search")`) when you want the trace tree to mirror the agent's logic.
  Skippable — Layer 1 already produces useful traces.

### 7.2 What actually changes in an existing agent harness

The entire diff for a harness already using the Anthropic SDK:

```python
# requirements: add `obs` (internal) which pins opentelemetry-sdk + traceloop-sdk

# main.py — add 2 lines at the top, change nothing else
import obs
obs.init(service_name="research-agent")     # reads OTEL_* / TRACELOOP_* from env

# ... existing harness code unchanged ...
client = anthropic.Anthropic()              # auto-patched — emits gen_ai spans + token metrics
resp = client.messages.create(model="claude-opus-4-8", ...)
```

That's it. No exporter wiring, no manual spans, no backend URL in code. The `obs` package internally:
sets up `TracerProvider`/`MeterProvider` with OTLP exporters, resource attrs from env, calls
`Traceloop.init()`, and registers W3C trace-context propagation.

### 7.3 Config is 100% environment (12-factor) — same image everywhere

| Env var | Local (kind) | Production |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | `http://localhost:4317` (sidecar/DaemonSet) |
| `OTEL_SERVICE_NAME` | `research-agent` | `research-agent` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=local` | `deployment.environment=prod,team=ml` |
| `OTEL_TRACES_SAMPLER` | `always_on` | `parentbased_always_on` (tail-sample at consumer) |
| `TRACELOOP_TRACE_CONTENT` | `true` (see prompts locally) | `false` (redact; capture gated centrally) |

**Repointing backends, changing sampling, turning on redaction = collector config change, never a
code or image change.** Promote local → prod by swapping env + which collector you point at.

### 7.4 Context propagation (the one thing agents get wrong)

Auto-instrumentation propagates W3C `traceparent` on outgoing HTTP automatically, so cross-service
spans stitch together for free. Two agent-specific cases need a 1-line helper from `obs`:

- **Async tool calls / thread pools:** capture and re-attach context across the await/thread boundary
  (`ctx = obs.current_context()` → `with obs.use(ctx):`), or spans become orphans.
- **Sub-agent handoffs:** pass `traceparent` in the sub-agent invocation payload so the child agent's
  tree nests under the parent run.

`obs` ships these as thin wrappers so harness authors don't touch the raw OTEL context API.

### 7.5 Dev-flow integration (per environment, same instrumentation)

| Stage | How OTEL plugs in | Code change |
|---|---|---|
| **Local dev** | `make up` runs the collector in kind; app points at it via env. Or `docker compose up` with a collector + Grafana for a laptop loop. | none |
| **Pre-commit / unit** | Test asserts each agent run emits a root span with `gen_ai.usage.*` set — catches "we stopped emitting telemetry" regressions | none |
| **CI smoke** | Ephemeral collector (compose) + run a canned `/chat`; assert spans/metrics land | none |
| **Staging/Prod** | `opentelemetry-operator` injects the SDK + sidecar collector via pod annotation; gateway/consumer tiers handle the rest | none (annotation only) |

### 7.6 Adoption checklist for a new agent harness

1. Add the `obs` dependency; call `obs.init(service_name=...)` once. *(or skip even this with Layer 0
   `opentelemetry-instrument` / operator annotation.)*
2. Set the five env vars above (platform provides defaults via a ConfigMap).
3. Confirm in Grafana: a root span per run, nested LLM + tool spans, `gen_ai.usage.*` populated.
4. (Optional) add `@agent` / `@tool` decorators where you want the trace tree to mirror agent logic.
5. (Optional) add the `obs.use(ctx)` helper around async tool calls / sub-agent handoffs.

No exporter code, no backend URLs, no manual metrics. The platform owns sampling, cost, redaction,
and routing centrally — so agent teams get production-grade observability for ~2 lines of code.

```
