"""obs — drop-in OpenTelemetry instrumentation for agentic workloads.

Adoption is two lines:

    import obs
    obs.init(service_name="my-agent")     # reads OTEL_* from the environment

After that:
  * The Anthropic SDK is auto-instrumented (OpenLLMetry) — every `messages.create`
    emits a gen_ai span with model + token usage. `obs.llm_call` then only records
    metrics (no duplicate span). In mock mode (no instrumentor) it creates the span.
  * FastAPI/httpx are auto-instrumented when present.
  * App logs are shipped as OTLP logs, correlated to the active trace.
  * Providers are flushed on process exit.

Richer agent structure is opt-in via context managers (agent_run / llm_call / tool_call),
with helpers for cross-thread and cross-service trace propagation.
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import Status, StatusCode

_lock = threading.Lock()
_initialized = False
_anthropic_instrumented = False
_tracer: Optional[trace.Tracer] = None
_providers: list = []          # tracer/meter/logger providers, flushed on shutdown

# Metric instruments — created in init().
_token_counter = None
_llm_duration = None
_run_duration = None
_tool_counter = None
_loop_iterations = None

log = logging.getLogger("obs")


class _NoiseFilter(logging.Filter):
    """Keep instrumentation/transport chatter out of the OTLP log pipeline (L2)."""
    NOISY = ("opentelemetry", "urllib3", "httpx", "httpcore", "kafka", "asyncio")

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(self.NOISY)


def init(service_name: Optional[str] = None, **resource_attrs) -> None:
    """Idempotent, thread-safe bootstrap. Call once at process start.

    Endpoint/insecure/headers come from the standard OTEL_* env vars.
    """
    global _initialized, _tracer
    global _token_counter, _llm_duration, _run_duration, _tool_counter, _loop_iterations
    with _lock:
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

        # L3: don't capture prompt/response content by default (PII). Opt in explicitly.
        os.environ.setdefault("TRACELOOP_TRACE_CONTENT", "false")

        attrs = {"service.name": service_name} if service_name else {}
        attrs.update(resource_attrs)
        resource = Resource.create(attrs)  # folds in OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES

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
            "gen_ai.client.operation.duration", unit="s", description="LLM call wall-clock duration.")
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
        handler.addFilter(_NoiseFilter())
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        _providers.extend([tracer_provider, meter_provider, logger_provider])
        atexit.register(_shutdown)               # M5: flush on exit

        _instrument_libraries()
        _initialized = True
        log.info("obs initialized", extra={"service.name": service_name})


def _shutdown() -> None:
    """Flush + shut down providers so nothing is lost on process exit (M5)."""
    for p in _providers:
        try:
            if hasattr(p, "force_flush"):
                p.force_flush()
            p.shutdown()
        except Exception:  # noqa: BLE001
            pass


def _instrument_libraries() -> None:
    global _anthropic_instrumented
    if os.getenv("OBS_DISABLE_AUTOINSTRUMENT") == "1":
        return
    try:
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
        AnthropicInstrumentor().instrument()
        _anthropic_instrumented = True           # H4: instrumentor owns the LLM span
    except Exception as e:  # noqa: BLE001
        log.warning("anthropic auto-instrumentation unavailable: %s", e)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        log.debug("httpx instrumentation unavailable: %s", e)


def instrument_fastapi(app) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:  # noqa: BLE001
        log.warning("fastapi instrumentation unavailable: %s", e)


# --------------------------------------------------------------------------- #
# Context managers                                                             #
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
    start = time.monotonic()
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
            attrs = {"agent.name": name}
            _run_duration.record(time.monotonic() - start, attrs)
            _loop_iterations.record(run._iters, attrs)


class _LLMCall:
    def __init__(self, span, model: str, system: str):
        self._span = span                        # may be None when instrumentor owns the span (H4)
        self.model = model
        self.system = system

    def set_usage(self, input_tokens: int, output_tokens: int) -> None:
        if self._span is not None:
            self._span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
            self._span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        base = {"gen_ai.system": self.system, "gen_ai.request.model": self.model}
        _token_counter.add(input_tokens, {**base, "gen_ai.token.type": "input"})
        _token_counter.add(output_tokens, {**base, "gen_ai.token.type": "output"})


@contextmanager
def llm_call(model: str, system: str = "anthropic") -> Iterator[_LLMCall]:
    """Records token + latency metrics for one LLM call. Creates a span only when
    the Anthropic SDK is NOT auto-instrumented (e.g. mock mode) — otherwise the
    OpenLLMetry instrumentor already provides a richer span (H4: no duplicates)."""
    start = time.monotonic()
    if _anthropic_instrumented:
        call = _LLMCall(None, model, system)
        try:
            yield call
        finally:
            _llm_duration.record(time.monotonic() - start,
                                 {"gen_ai.system": system, "gen_ai.request.model": model})
        return
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
            _llm_duration.record(time.monotonic() - start,
                                 {"gen_ai.system": system, "gen_ai.request.model": model})


@contextmanager
def tool_call(name: str) -> Iterator[None]:
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
    return otel_context.get_current()


@contextmanager
def use(ctx: otel_context.Context) -> Iterator[None]:
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)


def inject_headers(carrier: Optional[dict] = None) -> dict:
    carrier = carrier if carrier is not None else {}
    inject(carrier)
    return carrier


def context_from_headers(carrier: dict) -> otel_context.Context:
    return extract(carrier)
