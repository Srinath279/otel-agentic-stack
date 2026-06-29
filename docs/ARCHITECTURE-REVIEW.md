# Architecture Review & Critique

An honest, critical review of the current implementation and a re-architecture for a scalable
production system. Findings are severity-ranked; each has **Current → Problem → Fix**.

> TL;DR: the *shape* is right (two-tier, Kafka, env-driven, standard conventions), but two design
> choices are silently wrong at scale (**tail sampling can't scale horizontally**, **RED metrics are
> biased by sampling**), the backends aren't production-grade (single-replica, local disk), and there's
> effectively no security or multi-tenancy. None are hard to fix; the rest of this doc is the plan.

> **Implementation status (this repo):** ✅ **done** — C1 (trace-ID Kafka partitioning), C2 (spanmetrics
> moved to gateway, pre-sampling), H4 (no double-spanning), M3/H5-partial (per-service cost), M5 (flush
> on shutdown), M6 (shared sub-agent pool), L1/L2/L3 (locked init, log-noise filter, prompt-capture off
> by default). 📋 **documented, not deployed on the laptop** (need real infra, would risk the working
> single-node stack) — H1 (HA object-storage backends), H2 (metrics remote-write), H3 (mTLS/auth),
> H5-full (multi-tenancy/quotas), M1 (per-node agent collector), M2 (adaptive sampling), M4 (Operator/
> GitOps), M7 (full bi-directional correlation). These are the prod-cluster steps; configs are below.

---

## Severity-ranked findings

### 🔴 Critical

**C1 — Tail sampling does not scale horizontally.**
*Current:* gateway's Kafka exporter uses default partitioning; the consumer runs a single replica doing
`tail_sampling`.
*Problem:* tail sampling requires **every span of a trace to land on the same consumer instance**. With
default Kafka partitioning, spans of one trace scatter across partitions → across consumer replicas →
each sees a *partial* trace → sampling decisions are wrong. It "works" today only because there's exactly
one consumer. Add a replica and it breaks.
*Fix:* make trace assembly deterministic before the stateful tier. Two standard options:
1. **`loadbalancing` exporter** in the gateway, `routing_key: traceID`, fronting a consumer **StatefulSet**
   — guarantees all spans of a trace hit the same consumer. (Can replace or sit after Kafka.)
2. **Partition Kafka by trace ID** (`partition_traces_by_id: true` on the Kafka exporter) + consumer
   group with partition affinity. Scale = partitions.
Either way: consumer becomes a StatefulSet scaled by partitions/shards, not a naive Deployment.

**C2 — RED metrics are biased by sampling.**
*Current:* `spanmetrics` connector sits in the consumer traces pipeline **after** `tail_sampling`.
*Problem:* request/error/duration metrics are computed only from *sampled* traces, so rate and
error-rate are systematically undercounted and non-deterministic (they track the sample, not reality).
This is a correctness bug, not just a tuning issue.
*Fix:* compute `spanmetrics` on the **full**, unsampled stream — in the **gateway** (before Kafka), or on
a dedicated non-sampled tap in the consumer. Tail sampling should only affect what's *stored in Tempo*,
never what's *counted*.

### 🟠 High

**H1 — Backends are not production-grade.**
*Current:* Tempo/Loki/Prometheus are single-replica with local PVCs.
*Problem:* no HA, no horizontal scale, data capped by one node's disk, lost on node failure.
*Fix:* Tempo distributed mode on **object storage** (S3/GCS); **Mimir/Thanos** for metrics via
remote-write; **Loki** with object storage + microservices mode. All multi-replica behind their query
frontends.

**H2 — Metrics ride Kafka then get pull-scraped from one consumer.**
*Current:* metrics flow app → gateway → Kafka → consumer → `prometheus` exporter (:8889) → Prometheus
scrape.
*Problem:* a consumer restart creates scrape gaps; pull from a single endpoint doesn't scale; Kafka adds
latency to metrics that don't need replay. It also couples metric availability to the trace consumer.
*Fix:* push metrics via **`prometheusremotewrite`** to Mimir/Thanos from the collector directly; keep
Kafka for traces/logs (replay matters there), not metrics. Or run a separate metrics-only collector.

**H3 — No security boundary.**
*Current:* plaintext OTLP, no auth, one NetworkPolicy, API key in a plain `Secret`.
*Problem:* anything in-cluster can inject or read telemetry; secrets are base64, not encrypted.
*Fix:* **mTLS** on all OTLP hops; an auth extension (bearer/OIDC) on the gateway receiver; per-tenant
credentials; NetworkPolicies default-deny; **external-secrets**/sealed-secrets for keys.

**H4 — Double-spanning for real LLM calls.**
*Current:* `obs.llm_call` creates a `chat` span *and* the Anthropic OpenLLMetry instrumentor creates its
own span for the same call.
*Problem:* duplicated/overlapping spans, confused trace trees, double-counted unless reconciled.
*Fix:* pick one source of truth. Either (a) let the instrumentor own the LLM span and have `obs` only
*record metrics* from its attributes, or (b) disable the instrumentor's span and keep the manual one.
Recommended: keep the instrumentor span (richer), make `obs.llm_call` a metrics-only context manager.

**H5 — No multi-tenancy or quotas.**
*Current:* single namespace, no tenant dimension, no rate limiting.
*Problem:* one noisy agent can exhaust the pipeline; no per-team cost attribution or isolation.
*Fix:* a `tenant`/`team` resource attribute enforced at the gateway; per-tenant routing + quotas
(rate-limiting processor); cost rollups grouped by tenant; optionally per-tenant pipelines.

### 🟡 Medium

**M1 — App-side telemetry loss on gateway outage.** `BatchSpanProcessor` drops when its in-memory queue
fills. *Fix:* a **per-node agent collector** (DaemonSet/sidecar) with a `file_storage` persistent queue;
apps export to `localhost`. Decouples app from gateway topology and survives blips.

**M2 — Static sampling.** Fixed 10% probabilistic + fixed thresholds don't adapt to load or error budget.
*Fix:* volume-adaptive sampling; raise/lower base rate by traffic; always-keep policies stay.

**M3 — Cost computed in Prometheus, model-only.** Recording rules can't carry tenant/team cleanly and
don't scale with cardinality. *Fix:* emit cost as a metric **in the collector** (transform/connector
with `tenant`,`team`,`model` dims), or a dedicated usage-metering consumer off Kafka → billing store.

**M4 — Config drift.** Collector config exists twice (k8s ConfigMaps *and* Helm values). *Fix:* one
source of truth — generate manifests from Helm, deploy via **OpenTelemetry Operator** + GitOps
(Argo/Flux). Operator also injects SDK + sidecar via pod annotation (zero app change).

**M5 — No graceful shutdown.** `obs` never flushes providers on exit → spans/metrics lost on pod
termination. *Fix:* register `atexit`/lifespan flush (`force_flush` + `shutdown`) on all three providers.

**M6 — Thread-per-request fan-out.** `subagents.coordinate` spins a new `ThreadPoolExecutor` per request.
*Fix:* a shared bounded pool or async; for true scale, sub-agents as separate services with context
propagation (helpers already exist).

**M7 — Correlation is half-wired.** `tracesToLogsV2` (Tempo→Loki) is set but not the reverse, and
exemplars (metrics→traces) aren't enabled. *Fix:* wire `lokiDerivedFields` (Loki→Tempo) and exemplar
storage end-to-end.

### 🟢 Low / polish

- **L1** `obs.init()` idempotency isn't lock-guarded (race on concurrent first call). Add a lock.
- **L2** Root-logger `LoggingHandler` at INFO captures third-party logs as OTLP. Scope to app loggers.
- **L3** Prompt/response capture not gated by default — PII risk in `claude` mode. Default off; redact at the SDK boundary too, not only the gateway.
- **L4** HPA on CPU only; pipelines are often throughput/memory-bound. Add memory + custom (queue-depth) metrics.
- **L5** `obs` is env-only — hard to unit-test. Accept an optional config object.

---

## Target production architecture

```
                          per-node                         trace-ID aware          stateful, sharded
 app pods (obs SDK) ─OTLP▶ agent collector ─OTLP▶ gateway ─loadbalancing─▶ consumer (StatefulSet) ─┐
   localhost:4317         (DaemonSet,                (stateless, HPA)      tail_sampling, routing   │
                          file_storage queue)         spanmetrics(FULL),                            │
                                                       redaction, schema     ┌───────────────────────┘
                                                       enforcement           │
                                                          │ metrics (remote_write, full)             │
                                                          ▼                   ▼ traces        ▼ logs
                                                    Mimir / Thanos        Tempo (S3)      Loki (S3)
                                                    (HA, remote-write)    distributed     microservices
                                                          └─────────────────┼───────────────┘
                                                                            ▼
                                                                 Grafana (HA) + Alertmanager (HA)
                                                                 SSO, exemplars, bi-dir correlation
   Kafka (multi-broker, RF3) buffers traces+logs (replay); metrics bypass Kafka (remote_write).
   Control plane: OpenTelemetry Operator + GitOps (Argo/Flux); single Helm source of truth.
   Cross-cutting: mTLS, OTLP auth, per-tenant quotas + cost chargeback, external-secrets.
```

Key deltas from today: **(1)** spanmetrics moves pre-sampling; **(2)** trace-ID-aware routing makes the
consumer horizontally scalable; **(3)** metrics bypass Kafka via remote-write; **(4)** object-backed HA
backends; **(5)** per-node agent collector for app durability; **(6)** security + multi-tenancy +
GitOps/Operator.

---

## Migration path (incremental, low-risk order)

1. **Correctness first (no infra change):** move `spanmetrics` to the gateway (fixes C2); make
   `obs.llm_call` metrics-only (fixes H4); add provider flush on shutdown (M5).
2. **Scale the consumer (C1):** add the `loadbalancing` exporter + consumer StatefulSet; verify tail
   sampling on >1 replica.
3. **Backends (H1/H2):** point Tempo/Loki at object storage; stand up Mimir; switch metrics to
   remote-write.
4. **Durability (M1):** add the per-node agent collector with persistent queue.
5. **Security + tenancy (H3/H5):** mTLS, OTLP auth, tenant attribute + quotas, external-secrets.
6. **Operate (M4):** OpenTelemetry Operator + GitOps; collapse config to one Helm source.
7. **Refine (M2/M3/M7):** adaptive sampling, collector-side cost metering, full correlation + exemplars.

---

## Scorecard

| Dimension | Today | Target |
|---|---|---|
| Correctness (sampling/metrics) | ⚠️ biased RED, single-consumer tail | ✅ full-stream metrics, sharded tail |
| Scalability | ⚠️ vertical only | ✅ horizontal at every tier |
| Availability | ❌ single-replica, local disk | ✅ HA, object storage |
| Durability | ⚠️ in-memory queues | ✅ node queue + Kafka RF3 |
| Security | ❌ plaintext, no auth | ✅ mTLS, auth, secrets mgmt |
| Multi-tenancy | ❌ none | ✅ tenant attr, quotas, chargeback |
| Operability | ⚠️ kubectl + drift | ✅ Operator + GitOps, single source |
| Cost visibility | 🟡 model-only, Prom rules | ✅ tenant/team, collector-metered |
| Instrumentation DX | ✅ 2-line `obs` | ✅ (keep) + Operator auto-inject |

The instrumentation library and the overall pillar design are the strong parts — keep them. The work is
in the pipeline's scaling correctness, the backends, and the production concerns (security, tenancy, ops).
