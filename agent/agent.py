"""The agent loop. Backend-agnostic; instrumented with obs context managers so
each run produces a trace tree: agent.run -> chat (per LLM call) -> tool (per tool)."""
from __future__ import annotations

import logging

import obs
from llm import MODEL, get_client
from tools import execute

log = logging.getLogger("agent")
MAX_ITERS = 8


def run(message: str, agent_name: str = "demo-agent") -> dict:
    client = get_client()
    system = "anthropic" if client.__class__.__name__ == "ClaudeClient" else "mock"
    messages = [{"role": "user", "content": message}]

    with obs.agent_run(agent_name) as agent:
        for _ in range(MAX_ITERS):
            agent.iteration()

            with obs.llm_call(MODEL, system=system) as call:
                result = client.step(messages)
                call.set_usage(result.input_tokens, result.output_tokens)

            messages.append({"role": "assistant", "content": result.assistant_content})

            if not result.tool_uses:
                log.info("agent finished", extra={"final": result.text[:200]})
                return {"answer": result.text}

            tool_results = []
            for tu in result.tool_uses:
                with obs.tool_call(tu["name"]):
                    try:
                        output = execute(tu["name"], tu["input"])
                        is_error = False
                    except Exception as exc:  # noqa: BLE001
                        output, is_error = f"error: {exc}", True
                    log.info("tool executed",
                             extra={"tool": tu["name"], "error": is_error})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": output,
                    "is_error": is_error,
                })
            messages.append({"role": "user", "content": tool_results})

        log.warning("agent hit max iterations")
        return {"answer": "(stopped: max iterations reached)"}
