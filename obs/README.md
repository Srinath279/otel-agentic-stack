# `obs` — drop-in OTEL instrumentation for agent harnesses

Production-grade agent observability for **two lines of code**. Wraps the OpenTelemetry SDK +
OpenLLMetry auto-instrumentation, reads all config from the environment, and exposes opt-in
context managers for agent-level structure.

## Install

```bash
pip install ./obs        # path install (the agent Dockerfile does this)
```

## Use

```python
import obs
obs.init(service_name="research-agent")   # OTEL_* env vars drive endpoint/sampling

# existing harness code unchanged — the Anthropic SDK is now auto-instrumented
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

Async / sub-agent context propagation:

```python
ctx = obs.current_context()
# ...hand to a thread / task...
with obs.use(ctx):
    ...                       # spans here nest under the parent run

headers = obs.inject_headers()           # sub-agent handoff (outbound)
parent = obs.context_from_headers(hdrs)  # sub-agent (inbound)
```

## Config (environment only)

| Var | Meaning |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP target (sidecar/gateway collector) |
| `OTEL_EXPORTER_OTLP_INSECURE` | `true` for plaintext (local) |
| `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` | resource identity/tags |
| `OBS_DISABLE_AUTOINSTRUMENT=1` | skip SDK auto-instrumentation (rare) |

No backend URLs live in code — repoint by changing the env or the collector config.

## Emitted telemetry

* **Traces:** `agent.run` → `chat` (per LLM call) → `tool` (per tool), plus auto HTTP spans.
* **Metrics:** `gen_ai.client.token.usage`, `gen_ai.client.operation.duration`,
  `agent.run.duration`, `agent.tool.calls`, `agent.loop.iterations`.
* **Logs:** app logs shipped as OTLP, correlated by trace/span id.
