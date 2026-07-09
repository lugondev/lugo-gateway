import asyncio
import pytest
from app.services.conversation.tools.device_mcp import (
    DeviceMcpTransport, discover_device_tools, DeviceMcpError,
)


class _ScriptedDevice:
    """Answers initialize + one or more tools/list pages via on_message."""
    def __init__(self, transport, pages):
        self.t = transport
        self.pages = pages  # list of result dicts for successive tools/list calls
    async def pump(self, sent):
        # Respond to whatever the transport just sent.
        while True:
            await asyncio.sleep(0)
            if not sent:
                continue
            payload = sent[-1]["payload"]
            mid = payload["id"]
            if payload["method"] == "initialize":
                self.t.on_message({"jsonrpc": "2.0", "id": mid,
                                   "result": {"serverInfo": {"name": "dev"}}})
            elif payload["method"] == "tools/list":
                page = self.pages.pop(0)
                self.t.on_message({"jsonrpc": "2.0", "id": mid, "result": page})
                if not page.get("nextCursor"):
                    return


@pytest.mark.asyncio
async def test_discovery_single_page():
    sent = []
    async def send_json(m): sent.append(m)
    t = DeviceMcpTransport(send_json)
    dev = _ScriptedDevice(t, [{"tools": [{"name": "self.audio.set_volume"}]}])
    pump = asyncio.create_task(dev.pump(sent))
    tools = await discover_device_tools(t)
    pump.cancel()
    assert [x["name"] for x in tools] == ["self.audio.set_volume"]
    # initialize used id 1, tools/list used id 2
    ids = [m["payload"]["id"] for m in sent]
    assert ids[0] == 1 and ids[1] == 2


@pytest.mark.asyncio
async def test_discovery_follows_cursor():
    sent = []
    async def send_json(m): sent.append(m)
    t = DeviceMcpTransport(send_json)
    dev = _ScriptedDevice(t, [
        {"tools": [{"name": "a"}], "nextCursor": "c1"},
        {"tools": [{"name": "b"}]},
    ])
    pump = asyncio.create_task(dev.pump(sent))
    tools = await discover_device_tools(t)
    pump.cancel()
    assert [x["name"] for x in tools] == ["a", "b"]
    # second tools/list carried the cursor
    list_calls = [m["payload"] for m in sent if m["payload"]["method"] == "tools/list"]
    assert list_calls[1]["params"] == {"cursor": "c1"}


@pytest.mark.asyncio
async def test_discovery_returns_empty_on_timeout():
    async def send_json(m): pass
    t = DeviceMcpTransport(send_json, request_timeout=0.01)
    tools = await discover_device_tools(t, discovery_timeout=0.01)
    assert tools == []
