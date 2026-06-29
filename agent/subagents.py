"""Coordinator + sub-agent fan-out — demonstrates scalable multi-agent tracing.

A coordinator agent delegates sub-tasks to sub-agents that run *concurrently*.
Trace context is propagated across threads (obs.use), so each sub-agent's spans
nest under the coordinator's run — you see one tree:

    agent.run coordinator
      ├── agent.run weather-agent ── chat ── tool get_weather
      └── agent.run math-agent    ── chat ── tool calculate

Scale to more sub-agents by adding a row to SUBAGENTS — no other change needed.
"""
from __future__ import annotations

import concurrent.futures as cf
import re

import obs
from llm import MODEL
from tools import execute


def _city(msg: str) -> str:
    m = re.search(r"\b(?:in|for|at)\s+([A-Z][a-zA-Z]+)", msg)
    return m.group(1) if m else "San Francisco"


def _expr(msg: str) -> str:
    m = re.search(r"[0-9][-+*/0-9(). ]{2,}", msg)
    return m.group(0).strip() if m else "2 + 2"


# name -> (tool, arg-builder). Add rows to scale the fan-out.
SUBAGENTS = {
    "weather-agent": ("get_weather", lambda m: {"city": _city(m)}),
    "math-agent": ("calculate", lambda m: {"expression": _expr(m)}),
}


def _run_subagent(name: str, tool: str, args: dict, parent_ctx) -> str:
    with obs.use(parent_ctx):                  # re-attach the coordinator's context in this thread
        with obs.agent_run(name) as run:
            run.iteration()
            with obs.llm_call(MODEL, system="mock") as call:
                call.set_usage(30, 12)         # sub-agents emit GenAI metrics too
            try:
                with obs.tool_call(tool):
                    out = execute(tool, args)
                return f"[{name}] {out}"
            except Exception as exc:           # noqa: BLE001
                return f"[{name}] error: {exc}"


def coordinate(message: str, agent_name: str = "coordinator") -> dict:
    """Run the coordinator, fanning out to all sub-agents in parallel."""
    with obs.agent_run(agent_name) as run:
        run.iteration()
        ctx = obs.current_context()
        with cf.ThreadPoolExecutor(max_workers=max(2, len(SUBAGENTS))) as ex:
            futures = {
                ex.submit(_run_subagent, name, tool, build(message), ctx): name
                for name, (tool, build) in SUBAGENTS.items()
            }
            results = [f.result() for f in cf.as_completed(futures)]
    return {"answer": " ".join(sorted(results)), "subagents": list(SUBAGENTS)}
