# otel-agentic-stack

End-to-end, production-shaped OpenTelemetry pipeline for an agentic workload, runnable on a laptop
via **kind**. Implements the two-tier collector design (gateway → Kafka → consumer) with tail
sampling, semantic enforcement, PII redaction, and the three pillars (traces, metrics, logs).

See **[PLAN.md](PLAN.md)** for the full architecture, the production hardening notes, and the
minimal-code integration design.

## What's here

```
obs/         reusable instrumentation package  (2-line adoption: import obs; obs.init())
agent/       demo agent (FastAPI, configurable Claude/mock) — uses obs
k8s/         kind-runnable manifests: Kafka, Tempo, Loki, Prometheus, Grafana, 2 collectors, agent
helm/        same collector configs as upstream-chart values (prod deploy shape)
dashboards/  Grafana "Agentic — OTEL Overview"
scripts/     load generator
Makefile     up / down / build / reload / load / urls / logs
```

## Architecture (one line)

`agent (OTEL SDK + OpenLLMetry)` → **gateway collector** (normalize/redact) → **Kafka** →
**consumer collector** (tail-sample, spanmetrics) → **Tempo / Prometheus / Loki** → **Grafana**.

## Quickstart

```bash
cp .env.example .env          # default LLM_MODE=mock needs no API key
make up                       # create cluster, build+load image, deploy everything
make load                     # send traffic
open http://localhost:30030   # Grafana (anonymous admin) → "Agentic — OTEL Overview"
```

Real Claude calls: set `LLM_MODE=claude` and `ANTHROPIC_API_KEY=...` in `.env`, then `make up`.

Dev loop: edit `agent/` or `obs/`, then `make reload`.

Teardown: `make down`.

## Prerequisites

Docker, `kind`, `kubectl`. (`brew install kind kubectl`.) ~3–4 GiB free for the cluster.

## Notes / known sharp edges

- **Resource-heavy:** Kafka + 2 collectors + 4 backends. If pods are `Pending`, give Docker more RAM.
- **First boot:** collectors retry until Kafka is up — a few `CrashLoopBackOff`/restarts early on are
  expected; they settle within a minute.
- **Metric names:** the Prometheus exporter sanitizes OTLP names (`.`→`_`, `_total`/`_seconds`
  suffixes). Dashboard queries assume the defaults; tweak if your collector version differs.
- **Routing + S3 cold storage** are documented inline in `k8s/31-collector-consumer.yaml` but left
  out of the runnable path (they need AWS creds). That's the one slide element not wired live.
- The minimal-code story is real: the agent's entire instrumentation is `obs.init()` +
  `obs.instrument_fastapi(app)` in `agent/app.py`, plus optional `with obs.*` context managers.
