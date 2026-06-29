# otel-agentic-stack

**Production-shaped, end-to-end OpenTelemetry observability for agentic (LLM) workloads — runnable on a laptop.**

A complete, two-tier OTEL pipeline (gateway → Kafka → consumer) with tail sampling, semantic
enforcement, PII redaction, cost reporting, alerting, and the three pillars (traces, metrics, logs)
into Tempo / Prometheus / Loki, visualized in Grafana. Ships with a reusable `obs` package that
instruments any Python agent in **two lines of code**, and a demo agent that runs against real Claude
or a no-key mock.

---

## Table of contents
- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Quickstart](#quickstart)
- [Usage](#usage)
- [Configuration](#configuration)
- [Dashboards](#dashboards)
- [Testing](#testing)
- [Integrate `obs` into your own agent](#integrate-obs-into-your-own-agent)
- [Troubleshooting](#troubleshooting)
- [Production hardening](#production-hardening)
- [Documentation](#documentation)

---

## Why this exists

Agentic AI is hard to observe: non-deterministic, many nested LLM/tool calls, token cost that adds up,
and silent failure modes (loops, runaway spend, fragmented traces). This repo is a working reference
for instrumenting agents with OpenTelemetry the right way:

- **Minimal code change** — `import obs; obs.init()` and the Anthropic SDK is auto-instrumented.
- **Vendor-neutral** — standard `gen_ai.*` semantic conventions; swap backends via config, not code.
- **Production-shaped** — the same gateway/consumer/Kafka design used at scale (e.g. DoorDash), sized
  for a laptop but structured to grow.

---

## Architecture

```
 agent (FastAPI + obs SDK + OpenLLMetry)
        │ OTLP/gRPC
        ▼
 ┌──────────────────────┐   GATEWAY (stateless, autoscaled)
 │  gateway collector   │   memory_limiter · semantic enforcement · PII redaction · batch
 └──────────┬───────────┘
            │ Kafka exporter
            ▼
 ┌──────────────────────┐   DURABLE BUFFER (spikes, replay, fan-out)
 │   Kafka (3 topics)   │
 └──────────┬───────────┘
            │ Kafka receiver
            ▼
 ┌──────────────────────┐   CONSUMER (stateful)
 │  consumer collector  │   tail_sampling · spanmetrics · routing
 └───┬───────┬───────┬──┘
     ▼       ▼       ▼
  Tempo  Prometheus Loki        ← + Alertmanager (alerts) + recording rules (cost)
     └───────┼───────┘
             ▼
          Grafana   ← 4 dashboards: Overview · Cost · Performance · Pipeline Health
```

The app **only** knows `OTEL_EXPORTER_OTLP_ENDPOINT` — all sampling, redaction, cost, and routing
policy lives in the collectors, changeable without touching the agent.

---

## Repository layout

```
otel-agentic-stack/
├── obs/                      # reusable instrumentation package (2-line adoption)
│   └── obs/__init__.py
├── agent/                    # demo agent (FastAPI, Claude/mock, uses obs)
│   ├── app.py  agent.py  llm.py  tools.py  Dockerfile
├── collector/                # wrapper Dockerfile for the OTEL collector image
├── k8s/                      # kind-runnable manifests (numbered apply order)
│   ├── 00-namespace  10-kafka  20-tempo  21-loki  22-prometheus
│   ├── 23-grafana  24-alertmanager  30-collector-gateway  31-collector-consumer
│   └── 40-agent  50-hpa  60-networkpolicy
├── dashboards/               # 4 Grafana dashboards (auto-provisioned)
├── helm/                     # prod deploy shape (upstream collector chart values)
├── scripts/                  # load.sh (traffic), verify.sh (smoke test)
├── kind/cluster.yaml         # local cluster + NodePort mappings
├── Makefile                  # up / down / reload / load / verify
├── PLAN.md                   # full architecture + production roadmap
└── docs/COMPONENTS.md        # per-component reference
```

---

## Prerequisites

- **Docker** (Docker Desktop on macOS/Windows) — engine running
- **kind** — local Kubernetes in Docker
- **kubectl**
- (optional) an **`ANTHROPIC_API_KEY`** for real Claude calls; the default mock mode needs none
- ~4–6 GiB free RAM for the cluster

```bash
brew install kind kubectl          # macOS
# Docker Desktop: install separately and ensure it's running
```

> **macOS / Apple Silicon:** disable Docker Desktop's containerd image store
> (**Settings → General → uncheck "Use containerd for pulling and storing images"**). It mis-resolves
> multi-arch images and breaks the collector. See [Troubleshooting](#troubleshooting).

---

## Quickstart

```bash
git clone <your-repo-url> otel-agentic-stack
cd otel-agentic-stack

cp .env.example .env               # default LLM_MODE=mock needs no API key
make up                            # create cluster, build+load images, deploy everything (~8–10 min first run)
make load                          # send traffic
make verify                        # automated end-to-end smoke test
open http://localhost:30030        # Grafana (anonymous admin)
```

For real Claude calls: set `LLM_MODE=claude` and `ANTHROPIC_API_KEY=sk-...` in `.env`, then `make up`.

Teardown:
```bash
make down
```

---

## Usage

| Command | What it does |
|---|---|
| `make up` | Full bring-up: cluster → images → backends → collectors → agent |
| `make down` | Delete the kind cluster |
| `make load` | Fire 50 requests at the agent (`make load N=200` for more) |
| `make verify` | End-to-end smoke test: pods, agent, metrics, traces → pass/fail |
| `make reload` | Rebuild the agent image and roll it (dev loop) |
| `make multi` | Add a 2nd agent + sub-agent endpoint + in-cluster load generator |
| `make collectors` | Rebuild + redeploy just the collectors |
| `make dashboard` | Reload all Grafana dashboards |
| `make urls` | Print Grafana + agent URLs |
| `make logs` | Tail the agent logs |

**Hit the agent directly:**
```bash
curl -s localhost:30080/healthz
curl -s -X POST localhost:30080/chat -H 'content-type: application/json' \
  -d '{"message":"what is the weather in Tokyo"}'
```

**Access points (NodePorts):**
- Grafana → http://localhost:30030
- Agent → http://localhost:30080

---

## Configuration

All configuration is environment-driven (12-factor) — set on the agent via [k8s/00-namespace.yaml](k8s/00-namespace.yaml) (`agent-config` ConfigMap) or `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODE` | `mock` | `mock` (no key/cost) or `claude` (real Anthropic calls) |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_MODE=claude` (injected as a k8s Secret) |
| `CLAUDE_MODEL` | `claude-opus-4-8` | Model for real calls |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector-gateway:4317` | Where the SDK ships OTLP |
| `OTEL_SERVICE_NAME` | `agentic-demo` | Resource `service.name` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=local,...` | Extra resource tags |

Swapping backends, sampling, or redaction is a **collector-config change**, never an app/image change.

---

## Dashboards

Auto-provisioned in Grafana (☰ → Dashboards):

| Dashboard | Use it for |
|---|---|
| **Agentic — OTEL Overview** | At-a-glance: token rate, LLM/agent latency, tool success, logs |
| **Agentic — Cost & Token Usage** | $/min by model, tokens in/out, cumulative, token share |
| **Agentic — Performance & Latency** | LLM p50/p95/p99, agent-run p95, request rate, loop heatmap |
| **Agentic — Pipeline Health** | Collector accepted/refused/sent/failed spans, queue, memory, firing alerts |
| **Agentic — Multi-Agent & Sub-Agents** | Per-service and per-role breakdown (multiple agents + coordinator/sub-agents) |

Set the time range to **Last 30 minutes** and run `make load` so panels have data.

### Screenshots

Capture instructions and filenames are in [docs/screenshots/](docs/screenshots/). Once you drop PNGs
there, they render below:

| Overview | Cost & Usage |
|---|---|
| ![Overview](docs/screenshots/overview.png) | ![Cost](docs/screenshots/cost-usage.png) |
| **Performance** | **Multi-Agent** |
| ![Performance](docs/screenshots/performance.png) | ![Multi-Agent](docs/screenshots/multi-agent.png) |

**Alerts** (Alertmanager + Prometheus rules): LLM p95 latency, tool failure rate, runaway loops,
token burn, refused data, exporter failures, target down. Add Slack/PagerDuty receivers in
[k8s/24-alertmanager.yaml](k8s/24-alertmanager.yaml).

---

## Testing

```bash
make verify
```
Checks all deployments are ready → agent answers `/chat` → generates load → asserts token metrics in
Prometheus and traces in Tempo. Exit code = number of failed checks. Manual verification queries are in
[docs/COMPONENTS.md](docs/COMPONENTS.md) and the verify script ([scripts/verify.sh](scripts/verify.sh)).

---

## Integrate `obs` into your own agent

```python
import obs
obs.init(service_name="my-agent")          # reads OTEL_* from the environment

# existing harness code unchanged — Anthropic SDK now auto-instrumented
client = anthropic.Anthropic()
client.messages.create(model="claude-opus-4-8", ...)
```

Opt-in richer traces:
```python
with obs.agent_run("planner") as run:
    with obs.llm_call("claude-opus-4-8") as call:
        resp = client.messages.create(...)
        call.set_usage(resp.usage.input_tokens, resp.usage.output_tokens)
    with obs.tool_call("search"):
        ...
    run.iteration()
```
See [obs/README.md](obs/README.md). Install with `pip install ./obs`.

---

## Multiple agents & sub-agents

The design scales along two axes — and both show up as distinct, queryable telemetry.

### Many agents / use-cases on one pipeline
Every agent is just a workload with its own `OTEL_SERVICE_NAME`, all exporting to the **same gateway**.
No per-agent pipeline wiring — the collectors fan in. The metrics carry a `service_name` label, so the
**Multi-Agent dashboard** breaks everything down per agent.

```bash
make multi      # adds a 2nd agent (support-agent) + an in-cluster load generator
```

To add another agent/use-case: copy the `support-agent` block in
[k8s/41-agents-extra.yaml](k8s/41-agents-extra.yaml), change the name and `OTEL_SERVICE_NAME`. To scale
one agent horizontally: `kubectl -n otel-demo scale deploy/agent --replicas=3` (the HPA pattern). All
replicas/agents share the same `service.name` grouping but distinct pod identity.

### Sub-agents within an agent (fan-out)
A **coordinator** delegates sub-tasks to sub-agents that run concurrently, with trace context propagated
across threads — so one request becomes a single nested trace:

```
agent.run coordinator
  ├── agent.run weather-agent ── chat ── tool get_weather
  └── agent.run math-agent    ── chat ── tool calculate
```

Try it:
```bash
curl -s -X POST localhost:30080/orchestrate -H 'content-type: application/json' \
  -d '{"message":"weather in Paris and 3 * 4"}'
```

Implemented in [agent/subagents.py](agent/subagents.py) — **add a row to `SUBAGENTS` to scale the
fan-out**, no other change. The `agent_name` label (`coordinator`, `weather-agent`, `math-agent`, …)
distinguishes roles in metrics and traces. For sub-agents in **separate services**, `obs` provides
`inject_headers()` / `context_from_headers()` to carry `traceparent` across the network so the child's
tree nests under the parent.

**Why it stays clean at scale:** the app only emits standard `gen_ai.*` + `agent.*` telemetry tagged by
`service.name` and `agent.name`. Tail sampling, cost, and routing are applied centrally in the collectors
— so going from 1 agent to 100 agents with sub-agents is a deploy concern, not a re-instrumentation one.

---

## Troubleshooting

Real issues encountered bringing this up on macOS, and their fixes:

| Symptom | Cause | Fix |
|---|---|---|
| `exec /otelcol-contrib: no such file or directory` | Docker Desktop containerd image store picks the binary-less attestation manifest | Disable it: **Settings → General → uncheck "Use containerd for pulling and storing images"**, Apply & Restart |
| Collector `CrashLoopBackOff` after image runs | contrib **0.116.0** is a broken release artifact | Already pinned to **0.119.0**; verify with `docker run --rm otel/opentelemetry-collector-contrib:0.119.0 --version` |
| Collector `ImagePullBackOff` with a local image | `kind load docker-image` doesn't land images under the containerd store | We use `docker save \| kind load image-archive` + `imagePullPolicy: Never` (in the Makefile) |
| `connection refused` to `127.0.0.1:PORT` from kubectl | Docker Desktop restarted → kind API server moved to a new port | `kind export kubeconfig --name otel-demo` |
| `make load` → `No rule to make target` | Run from the wrong directory | `cd` into the repo root first |
| Grafana panels blank | No traffic yet, or default time window | `make load`, then set time range to **Last 30 minutes** |
| Tempo/Loki/Prometheus crash on PVC | Non-root uid vs volume permissions | `fsGroup` is set; if it persists, paste the log |
| Pods `Pending` | Not enough RAM | Give Docker Desktop 6–8 GiB (Settings → Resources) |

---

## Production hardening

This runs the production *shape* at laptop scale. To go fully production (details in [PLAN.md](PLAN.md) §6):

- **Collector tiers:** gateway as autoscaled Deployment + per-node sidecar/DaemonSet; consumer scaled via Kafka partitions
- **Durability:** multi-broker Kafka (RF≥3); object-storage-backed Tempo/Loki; Prometheus remote-write to Mimir/Thanos
- **Security:** mTLS + authn on OTLP receivers, NetworkPolicies, external-secrets for keys
- **Sampling & cost:** tune tail-sampling policies; move cost rollups into the collector; per-tenant routing + S3 cold storage
- **Deploy:** Helm (see [helm/](helm/)) or the OpenTelemetry Operator for zero-code SDK + sidecar injection

---

## Documentation

- **[PLAN.md](PLAN.md)** — full architecture, production-grade target design, minimal-code integration model
- **[docs/COMPONENTS.md](docs/COMPONENTS.md)** — per-component reference (what each piece is, how it helps, what to use it for, how to extend)
- **[obs/README.md](obs/README.md)** — the instrumentation package
- **[helm/README.md](helm/README.md)** — production deploy via the upstream collector chart

---

## License

MIT — see [LICENSE](LICENSE).
