"""Two demo tools the agent can call. Real implementations would hit services;
these are deterministic so the telemetry pipeline is the thing under test."""
from __future__ import annotations

import ast
import operator as op

# Anthropic-style tool schemas (also consumed by the mock client).
TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
    {
        "name": "calculate",
        "description": "Evaluate a basic arithmetic expression, e.g. '3 * (4 + 5)'.",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    },
]

_OPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
    ast.Div: op.truediv, ast.Pow: op.pow, ast.USub: op.neg, ast.Mod: op.mod,
}


def _safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")


def execute(name: str, args: dict) -> str:
    if name == "get_weather":
        city = args.get("city", "unknown")
        return f"It is 21C and clear in {city}."
    if name == "calculate":
        expr = args.get("expression", "")
        return str(_safe_eval(ast.parse(expr, mode="eval").body))
    raise ValueError(f"unknown tool: {name}")
