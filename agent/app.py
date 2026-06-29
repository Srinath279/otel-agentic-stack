"""FastAPI entrypoint. The entire observability wiring is two lines:
`obs.init(...)` and `obs.instrument_fastapi(app)`."""
from __future__ import annotations

import os

import obs

obs.init(service_name=os.getenv("OTEL_SERVICE_NAME", "agentic-demo"))

from fastapi import FastAPI  # noqa: E402  (import after init so instrumentation attaches)
from pydantic import BaseModel  # noqa: E402

import agent  # noqa: E402
import subagents  # noqa: E402

app = FastAPI(title="otel-agentic-demo")
obs.instrument_fastapi(app)


class ChatRequest(BaseModel):
    message: str
    agent: str = "demo-agent"


@app.get("/healthz")
def healthz():
    return {"status": "ok", "llm_mode": os.getenv("LLM_MODE", "mock")}


@app.post("/chat")
def chat(req: ChatRequest):
    return agent.run(req.message, agent_name=req.agent)


@app.post("/orchestrate")
def orchestrate(req: ChatRequest):
    """Coordinator that fans out to sub-agents in parallel (nested traces)."""
    return subagents.coordinate(req.message, agent_name=req.agent if req.agent != "demo-agent" else "coordinator")
