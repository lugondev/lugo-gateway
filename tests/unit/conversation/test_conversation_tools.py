import asyncio
from datetime import datetime

import pytest

from app.services.conversation.tools.base import Tool, ToolContext, ToolRegistry, ToolSource
from app.services.conversation.tools.local import LocalToolSource
from app.services.conversation.tools.mcp import McpToolSource, mcp_tool_to_tool


# ---- ToolContext -----------------------------------------------------------


def test_tool_context_send_command_forwards_payload():
    seen = []

    async def emit(p):
        seen.append(p)

    ctx = ToolContext(emit_command=emit)
    asyncio.run(ctx.send_command({"action": "led_on"}))
    assert seen == [{"action": "led_on"}]


def test_tool_context_send_command_noop_without_emitter():
    # No emitter configured -> silently does nothing (no crash).
    asyncio.run(ToolContext().send_command({"x": 1}))


# ---- ToolRegistry ----------------------------------------------------------


def _echo_tool(name="echo"):
    async def _run(args, ctx):
        return f"ran {name} with {args}"

    return Tool(name=name, description="d", parameters={"type": "object"}, run=_run)


def test_registry_add_get_len():
    reg = ToolRegistry()
    reg.add(_echo_tool("a"))
    assert len(reg) == 1
    assert reg.get("a") is not None
    assert reg.get("missing") is None


def test_registry_openai_schema_shape():
    reg = ToolRegistry()
    reg.add(_echo_tool("a"))
    schema = reg.openai_schema()
    assert schema == [
        {"type": "function", "function": {"name": "a", "description": "d", "parameters": {"type": "object"}}}
    ]


def test_registry_run_dispatches():
    reg = ToolRegistry()
    reg.add(_echo_tool("a"))
    assert asyncio.run(reg.run("a", {"x": 1}, ToolContext())) == "ran a with {'x': 1}"


def test_registry_run_unknown_tool_returns_error():
    out = asyncio.run(ToolRegistry().run("nope", {}, ToolContext()))
    assert "unknown tool" in out.lower()


def test_registry_run_tool_error_does_not_raise():
    reg = ToolRegistry()

    async def _boom(args, ctx):
        raise ValueError("kaboom")

    reg.add(Tool(name="boom", description="d", parameters={}, run=_boom))
    out = asyncio.run(reg.run("boom", {}, ToolContext()))
    assert "kaboom" in out and out.lower().startswith("error")


def test_registry_aggregates_a_source():
    class Src(ToolSource):
        def list_tools(self):
            return [_echo_tool("x"), _echo_tool("y")]

    reg = ToolRegistry([Src()])
    assert len(reg) == 2
    assert {t["function"]["name"] for t in reg.openai_schema()} == {"x", "y"}


# ---- LocalToolSource -------------------------------------------------------


def test_get_time_uses_injected_clock():
    src = LocalToolSource(clock=lambda: datetime(2026, 7, 1, 9, 30))
    reg = ToolRegistry([src])
    out = asyncio.run(reg.run("get_time", {}, ToolContext()))
    assert "09:30" in out


def test_device_command_emits_and_confirms():
    seen = []

    async def emit(p):
        seen.append(p)

    reg = ToolRegistry([LocalToolSource()])
    out = asyncio.run(
        reg.run("device_command", {"action": "led_on", "params": {"color": "red"}}, ToolContext(emit_command=emit))
    )
    assert seen == [{"event": "device_command", "action": "led_on", "params": {"color": "red"}}]
    assert "led_on" in out


def test_device_command_missing_action_errors_without_emitting():
    seen = []

    async def emit(p):
        seen.append(p)

    reg = ToolRegistry([LocalToolSource()])
    out = asyncio.run(reg.run("device_command", {}, ToolContext(emit_command=emit)))
    assert seen == []
    assert out.lower().startswith("error")


# ---- McpToolSource (interface-ready; transport injected) -------------------


def test_mcp_tool_to_tool_maps_fields():
    async def invoker(name, args):
        return "ok"

    t = mcp_tool_to_tool(
        {"name": "weather", "description": "get weather", "inputSchema": {"type": "object", "properties": {}}},
        invoker,
    )
    assert t.name == "weather"
    assert t.description == "get weather"
    assert t.parameters == {"type": "object", "properties": {}}


def test_mcp_source_runs_via_injected_invoker():
    calls = []

    async def invoker(name, args):
        calls.append((name, args))
        return "sunny"

    reg = ToolRegistry([McpToolSource([{"name": "weather", "description": "w"}], invoker)])
    out = asyncio.run(reg.run("weather", {"city": "Hanoi"}, ToolContext()))
    assert calls == [("weather", {"city": "Hanoi"})]
    assert out == "sunny"


def test_mcp_tool_default_parameters_when_missing():
    async def invoker(name, args):
        return ""

    t = mcp_tool_to_tool({"name": "x"}, invoker)
    assert t.parameters == {"type": "object"}
