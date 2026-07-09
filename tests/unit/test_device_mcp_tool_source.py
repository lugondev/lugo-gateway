import pytest
from app.services.conversation.tools.base import ToolContext
from app.services.conversation.tools.device_mcp import (
    DeviceMcpToolSource, sanitize_tool_name,
)


class _FakeTransport:
    def __init__(self, result):
        self._result = result
        self.calls = []
    async def call(self, method, params=None, **kw):
        self.calls.append((method, params))
        return self._result


def test_sanitize_replaces_dots():
    assert sanitize_tool_name("self.audio.set_volume") == "self_audio_set_volume"
    assert sanitize_tool_name("ok-name_1") == "ok-name_1"


def test_list_tools_sanitizes_name_and_maps_schema():
    defs = [{
        "name": "self.audio.set_volume",
        "description": "Set speaker volume",
        "inputSchema": {"type": "object",
                        "properties": {"volume": {"type": "integer"}},
                        "required": ["volume"]},
        "annotations": {"requiresConfirm": False},
    }]
    src = DeviceMcpToolSource(defs, _FakeTransport({}))
    tools = src.list_tools()
    assert tools[0].name == "self_audio_set_volume"
    assert "volume" in tools[0].parameters["properties"]
    assert "confirm" not in tools[0].parameters["properties"]


def test_confirm_tool_injects_confirm_property():
    defs = [{"name": "self.device.shutdown", "description": "Power off",
             "inputSchema": {"type": "object", "properties": {}},
             "annotations": {"requiresConfirm": True}}]
    src = DeviceMcpToolSource(defs, _FakeTransport({}))
    tool = src.list_tools()[0]
    assert tool.parameters["properties"]["confirm"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_confirm_required_without_confirm_blocks_and_does_not_call():
    ft = _FakeTransport({"content": [{"type": "text", "text": "off"}]})
    defs = [{"name": "self.device.shutdown", "description": "Power off",
             "annotations": {"requiresConfirm": True}}]
    tool = DeviceMcpToolSource(defs, ft).list_tools()[0]
    out = await tool.run({}, ToolContext())
    assert "CONFIRMATION_REQUIRED" in out
    assert ft.calls == []


@pytest.mark.asyncio
async def test_confirm_true_relays_with_real_name_and_strips_confirm():
    ft = _FakeTransport({"content": [{"type": "text", "text": "powered off"}]})
    defs = [{"name": "self.device.shutdown", "description": "Power off",
             "annotations": {"requiresConfirm": True}}]
    tool = DeviceMcpToolSource(defs, ft).list_tools()[0]
    out = await tool.run({"confirm": True}, ToolContext())
    assert out == "powered off"
    method, params = ft.calls[0]
    assert method == "tools/call"
    assert params == {"name": "self.device.shutdown", "arguments": {}}


@pytest.mark.asyncio
async def test_non_confirm_tool_relays_and_unwraps_text():
    ft = _FakeTransport({"content": [{"type": "text", "text": "volume set to 70"}]})
    defs = [{"name": "self.audio.set_volume", "description": "vol",
             "annotations": {"requiresConfirm": False}}]
    tool = DeviceMcpToolSource(defs, ft).list_tools()[0]
    out = await tool.run({"volume": 70}, ToolContext())
    assert out == "volume set to 70"
    assert ft.calls[0][1] == {"name": "self.audio.set_volume", "arguments": {"volume": 70}}


@pytest.mark.asyncio
async def test_is_error_result_becomes_error_string():
    ft = _FakeTransport({"isError": True, "error": "bad pin"})
    defs = [{"name": "self.gpio.set", "description": "gpio"}]
    tool = DeviceMcpToolSource(defs, ft).list_tools()[0]
    out = await tool.run({"pin": 2}, ToolContext())
    assert "Error" in out and "bad pin" in out
