"""LLM client abstraction with two interchangeable backends, selected by LLM_MODE:

  * claude — real Anthropic SDK calls (auto-instrumented by obs/OpenLLMetry)
  * mock   — scripted, deterministic, no API key or cost

Both return the same normalized StepResult so the agent loop is backend-agnostic.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from tools import TOOLS

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")


@dataclass
class StepResult:
    assistant_content: list  # raw provider content blocks to append to history
    tool_uses: list          # [{"id","name","input"}]
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str


def get_client():
    mode = os.getenv("LLM_MODE", "mock").lower()
    return ClaudeClient() if mode == "claude" else MockClient()


class ClaudeClient:
    system = "You are a helpful assistant. Use tools when they help."

    def __init__(self):
        import anthropic
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    def step(self, messages: list) -> StepResult:
        # thinking omitted: Opus 4.8 runs without thinking, which keeps the
        # tool-loop history simple (no thinking blocks to replay).
        resp = self._client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=self.system,
            tools=TOOLS,
            messages=messages,
        )
        tool_uses = [
            {"id": b.id, "name": b.name, "input": b.input}
            for b in resp.content if b.type == "tool_use"
        ]
        text = "".join(b.text for b in resp.content if b.type == "text")
        return StepResult(
            assistant_content=resp.content,
            tool_uses=tool_uses,
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason,
        )


class MockClient:
    """Deterministic stand-in. Emits the same content-block shapes as Anthropic
    so the agent loop and telemetry are identical to the real path."""
    system = "mock"
    _n = 0

    def step(self, messages: list) -> StepResult:
        last = messages[-1]
        # If the last turn carried tool results, produce a final answer.
        if last["role"] == "user" and isinstance(last["content"], list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in last["content"]
        ):
            results = [b["content"] for b in last["content"] if b.get("type") == "tool_result"]
            text = "Done. " + " ".join(str(r) for r in results)
            return self._text(text)

        user_text = _last_user_text(messages)
        low = user_text.lower()
        if "weather" in low:
            city = _extract_city(user_text)
            return self._tool("get_weather", {"city": city})
        if re.search(r"\d\s*[-+*/]\s*\d", user_text):
            expr = re.sub(r"[^0-9+\-*/(). ]", "", user_text).strip()
            return self._tool("calculate", {"expression": expr})
        return self._text(f"(mock) You said: {user_text}")

    def _tool(self, name: str, args: dict) -> StepResult:
        self._n += 1
        tid = f"toolu_mock_{self._n}"
        content = [{"type": "tool_use", "id": tid, "name": name, "input": args}]
        return StepResult(content, [{"id": tid, "name": name, "input": args}], "",
                          _toklen(str(args)) + 20, 15, "tool_use")

    def _text(self, text: str) -> StepResult:
        content = [{"type": "text", "text": text}]
        return StepResult(content, [], text, 25, _toklen(text), "end_turn")


def _last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m["role"] == "user":
            c = m["content"]
            if isinstance(c, str):
                return c
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    return b["text"]
    return ""


def _extract_city(text: str) -> str:
    m = re.search(r"\b(?:in|for|at)\s+([A-Z][a-zA-Z]+)", text)
    return m.group(1) if m else "San Francisco"


def _toklen(s: str) -> int:
    return max(1, len(s) // 4)
