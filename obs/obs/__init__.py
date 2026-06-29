"""obs — drop-in OpenTelemetry instrumentation for agentic workloads.

Adoption is two lines:

    import obs
    obs.init(service_name="my-agent")     # reads OTEL_* from the environment

After that:
  * The Anthropic SDK is auto-instrumented (OpenLLMetry) — every `messages.create`
    emits a gen_ai span with model + token usage.
  * FastAPI/httpx are auto-instrumented when present.
  * App logs are shipped as OTLP logs, correlated to the active trace.

Richer agent structure is opt-in via context managers:

    with obs.agent_run("planner") as run:
        with obs.llm_call(model) as call:
            resp = client.messages.create(...)
            call.set_usage(resp.usage.input_tokens, resp.usage.output_tokens)
        with obs.tool_call("search"):
            ...
        run.iteration()

Everything (endpoint, sampling, redaction) is environment + collector config.
There are no backend URLs in application code.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import Status, StatusCode

_initialized = False
_tracer: Optional[trace.Tracer] = None

# Metric instruments — created in init(), used by the context managers below.
_token_counter = None
_llm_duration = None
_run_duration = None
_tool_counter = None
_loop_iterations = None

log = logging.getLogger("obs")


def init(service_name: Optional[str] = None, **resource_attrs) -> None:
    """Idempotent bootstrap. Safe to call once at process start.

    Endpoint/insecure/headers are read from the standard OTEL_* env vars by the
    OTLP exporters — do not hardcode them here.
    """
    global _initialized, _tracer
    global _token_counter, _llm_duration, _run_duration, _tool_counter, _loop_iterations
    if _initialized:
        return

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

    attrs = {"service.name": service_name} if service_name else {}
    attrs.update(resource_attrs)
    # Resource.create() also folds in OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES.
    resource = Resource.create(attrs)

    # ---- Traces -------------------------------------------------------------
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)
    _tracer = trace.get_tracer("obs")

    # ---- Metrics ------------------------------------------------------------
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10_000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)
    meter = metrics.get_meter("obs")

    _token_counter = meter.create_counter(
        "gen_ai.client.token.usage", unit="1",
        description="LLM tokens consumed (input/output), by model and token type.")
    _llm_duration = meter.create_histogram(
        "gen_ai.client.operation.duration", unit="s",
        description="LLM call wall-clock duration.")
    _run_duration = meter.create_histogram(
        "agent.run.duration", unit="s", description="End-to-end agent run duration.")
    _tool_counter = meter.create_counter(
        "agent.tool.calls", unit="1", description="Tool invocations, by tool and outcome.")
    _loop_iterations = meter.create_histogram(
        "agent.loop.iterations", unit="1", description="Agent loop iterations per run.")

    # ---- Logs (OTLP, trace-correlated) -------------------------------------
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    _instrument_libraries()
    _initialized = True
    log.info("obs initialized", extra={"service.name": service_name})


def _instrument_libraries() -> None:
    """Attach auto-instrumentation to whatever the harness already imports.

    This is the 'reactive to the platform' layer — guarded so a missing optional
    package never breaks startup.
    """
    if os.getenv("OBS_DISABLE_AUTOINSTRUMENT") == "1":
        return
    try:
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
        AnthropicInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        log.warning("anthropic auto-instrumentation unavailable: %s", e)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        log.debug("httpx instrumentation unavailable: %s", e)


def instrument_fastapi(app) -> None:
    """Auto-instrument a FastAPI app (HTTP server spans). Call after init()."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:  # noqa: BLE001
        log.warning("fastapi instrumentation unavailable: %s", e)


# --------------------------------------------------------------------------- #
# Context managers — the opt-in "Layer 2" structure                            #
# --------------------------------------------------------------------------- #
class _Run:
    def __init__(self, span):
        self._span = span
        self._iters = 0

    def iteration(self) -> None:
        self._iters += 1
        self._span.set_attribute("agent.loop.iterations", self._iters)


@contextmanager
def agent_run(name: str) -> Iterator[_Run]:
    """Root span for one agent task. Records duration + loop iterations."""
    start = time.monotonic()
    run = None
    with _tracer.start_as_current_span(f"agent.run {name}") as span:
        span.set_attribute("agent.name", name)
        run = _Run(span)
        try:
            yield run
        except Exception as exc:  # noqa: BLE001
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        finally:
            dur = time.monotonic() - start
            attrs = {"agent.name": name}
            _run_duration.record(dur, attrs)
            _loop_iterations.record(run._iters, attrs)


class _LLMCall:
    def __init__(self, span, model: str, system: str):
        self._span = span
        self.model = model
        self.system = system

    def set_usage(self, input_tokens: int, output_tokens: int) -> None:
        self._span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        self._span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        base = {"gen_ai.system": self.system, "gen_ai.request.model": self.model}
        _token_counter.add(input_tokens, {**base, "gen_ai.token.type": "input"})
        _token_counter.add(output_tokens, {**base, "gen_ai.token.type": "output"})


@contextmanager
def llm_call(model: str, system: str = "anthropic") -> Iterator[_LLMCall]:
    """Wrap a single LLM request. Source of truth for token + latency metrics
    (works even in mock mode where the SDK instrumentor never fires)."""
    start = time.monotonic()
    with _tracer.start_as_current_span(f"chat {model}") as span:
        span.set_attribute("gen_ai.system", system)
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.operation.name", "chat")
        call = _LLMCall(span, model, system)
        try:
            yield call
        except Exception as exc:  # noqa: BLE001
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        finally:
            _llm_duration.record(
                time.monotonic() - start,
                {"gen_ai.system": system, "gen_ai.request.model": model})


@contextmanager
def tool_call(name: str) -> Iterator[None]:
    """Wrap a tool execution. Records tool success/failure + retries."""
    start = time.monotonic()
    outcome = "ok"
    with _tracer.start_as_current_span(f"tool {name}") as span:
        span.set_attribute("tool.name", name)
        try:
            yield
        except Exception as exc:  # noqa: BLE001
            outcome = "error"
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        finally:
            _tool_counter.add(1, {"tool": name, "outcome": outcome})
            span.set_attribute("tool.outcome", outcome)
            span.set_attribute("tool.duration_s", time.monotonic() - start)


# --------------------------------------------------------------------------- #
# Context-propagation helpers (async tool calls / sub-agent handoffs)          #
# --------------------------------------------------------------------------- #
def current_context() -> otel_context.Context:
    """Capture the active context to re-attach across a thread/await boundary."""
    return otel_context.get_current()


@contextmanager
def use(ctx: otel_context.Context) -> Iterator[None]:
    """Re-attach a captured context so spans created inside nest correctly."""
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)


def inject_headers(carrier: Optional[dict] = None) -> dict:
    """Inject W3C traceparent into an outbound payload (sub-agent handoff)."""
    carrier = carrier if carrier is not None else {}
    inject(carrier)
    return carrier


def context_from_headers(carrier: dict) -> otel_context.Context:
    """Extract context from an inbound sub-agent payload."""
    return extract(carrier)
