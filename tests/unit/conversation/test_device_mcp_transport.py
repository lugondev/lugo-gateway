import asyncio
import pytest
from app.services.conversation.tools.device_mcp import DeviceMcpTransport, DeviceMcpError


def _collector():
    sent = []
    async def send_json(msg):
        sent.append(msg)
    return sent, send_json


@pytest.mark.asyncio
async def test_call_sends_envelope_and_resolves_on_matching_id():
    sent, send_json = _collector()
    t = DeviceMcpTransport(send_json)

    async def responder():
        await asyncio.sleep(0)  # let call() register the future + send
        mid = sent[-1]["payload"]["id"]
        t.on_message({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}})

    task = asyncio.create_task(responder())
    result = await t.call("tools/call", {"name": "x", "arguments": {}})
    await task

    assert result == {"ok": True}
    env = sent[-1]
    assert env["type"] == "mcp"
    assert env["payload"]["jsonrpc"] == "2.0"
    assert env["payload"]["method"] == "tools/call"
    assert env["payload"]["params"] == {"name": "x", "arguments": {}}
    assert env["payload"]["id"] >= 3  # call ids start at 3


@pytest.mark.asyncio
async def test_fixed_msg_id_is_honored():
    sent, send_json = _collector()
    t = DeviceMcpTransport(send_json)

    async def responder():
        await asyncio.sleep(0)
        t.on_message({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "dev"}}})

    task = asyncio.create_task(responder())
    result = await t.call("initialize", {"protocolVersion": "2024-11-05"}, msg_id=1)
    await task
    assert result["serverInfo"]["name"] == "dev"
    assert sent[-1]["payload"]["id"] == 1


@pytest.mark.asyncio
async def test_error_payload_raises():
    sent, send_json = _collector()
    t = DeviceMcpTransport(send_json)

    async def responder():
        await asyncio.sleep(0)
        mid = sent[-1]["payload"]["id"]
        t.on_message({"jsonrpc": "2.0", "id": mid, "error": {"code": -1, "message": "boom"}})

    task = asyncio.create_task(responder())
    with pytest.raises(DeviceMcpError, match="boom"):
        await t.call("tools/call", {"name": "x"})
    await task


@pytest.mark.asyncio
async def test_timeout_raises_and_cleans_up():
    _, send_json = _collector()
    t = DeviceMcpTransport(send_json, request_timeout=0.01)
    with pytest.raises(DeviceMcpError, match="timed out"):
        await t.call("tools/call", {"name": "x"})
    assert t._pending == {}


@pytest.mark.asyncio
async def test_send_exception_wrapped_in_device_mcp_error():
    async def send_json(msg):
        raise ConnectionResetError("closed")

    t = DeviceMcpTransport(send_json)
    with pytest.raises(DeviceMcpError) as exc_info:
        await t.call("tools/call", {"name": "x"})
    assert not isinstance(exc_info.value, ConnectionResetError)
    assert t._pending == {}


@pytest.mark.asyncio
async def test_unknown_id_is_dropped_not_raised():
    _, send_json = _collector()
    t = DeviceMcpTransport(send_json)
    t.on_message({"jsonrpc": "2.0", "id": 999, "result": {}})  # must not raise


@pytest.mark.asyncio
async def test_close_rejects_pending():
    sent, send_json = _collector()
    t = DeviceMcpTransport(send_json)
    fut_task = asyncio.create_task(t.call("tools/call", {"name": "x"}))
    await asyncio.sleep(0)  # register future
    t.close()
    with pytest.raises(DeviceMcpError, match="closed"):
        await fut_task
