import asyncio

import pytest

from app.core.identity_watch import IdentityWatchdog, receive_with_watchdog


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False


@pytest.mark.asyncio
async def test_watchdog_stays_valid_when_still_valid_returns_true():
    watchdog = IdentityWatchdog(still_valid=_true, interval_s=0.01)
    watchdog.start()
    await asyncio.sleep(0.03)
    assert watchdog.invalid is False
    watchdog.cancel()


@pytest.mark.asyncio
async def test_watchdog_flags_invalid_when_still_valid_returns_false():
    watchdog = IdentityWatchdog(still_valid=_false, interval_s=0.01)
    watchdog.start()
    await asyncio.sleep(0.03)
    assert watchdog.invalid is True


class _FakeWebSocket:
    def __init__(self, messages):
        self._messages = list(messages)

    async def receive(self):
        if not self._messages:
            await asyncio.sleep(3600)  # simulate "no more messages, block forever"
        return self._messages.pop(0)


@pytest.mark.asyncio
async def test_receive_with_watchdog_yields_messages_when_valid():
    ws = _FakeWebSocket([{"text": "one"}, {"text": "two"}])
    watchdog = IdentityWatchdog(still_valid=_true, interval_s=10)
    watchdog.start()
    received = []
    async for message in receive_with_watchdog(ws, watchdog):
        received.append(message)
        if len(received) == 2:
            break
    watchdog.cancel()
    assert received == [{"text": "one"}, {"text": "two"}]


@pytest.mark.asyncio
async def test_receive_with_watchdog_yields_none_when_watchdog_fires():
    ws = _FakeWebSocket([])  # receive() blocks forever -- only the watchdog can end this
    watchdog = IdentityWatchdog(still_valid=_false, interval_s=0.01)
    watchdog.start()
    result = "unset"
    async for message in receive_with_watchdog(ws, watchdog):
        result = message
        break
    assert result is None


class _SlowFakeWebSocket:
    """Like _FakeWebSocket, but receive() actually suspends before returning
    so the `recv` task is still pending when asyncio.wait() resolves via the
    (already-cancelled) watchdog side -- reproducing the race where the
    watchdog task finishes first without `recv` being done yet."""

    def __init__(self, messages):
        self._messages = list(messages)

    async def receive(self):
        await asyncio.sleep(0.02)
        return self._messages.pop(0)


@pytest.mark.asyncio
async def test_receive_with_watchdog_survives_watchdog_cancelled_mid_wait():
    ws = _SlowFakeWebSocket([{"text": "one"}])
    watchdog = IdentityWatchdog(still_valid=_true, interval_s=10)
    watchdog.start()
    watchdog.cancel()  # simulate an external caller tearing down the watchdog
    await asyncio.sleep(0)  # let the cancellation actually land on the task
    assert watchdog.task.done()  # sanity: watchdog side is already finished
    assert watchdog.invalid is False  # ...but not via "fired"
    received = []
    async for message in receive_with_watchdog(ws, watchdog):
        received.append(message)
        break
    assert received == [{"text": "one"}]


@pytest.mark.asyncio
async def test_receive_with_watchdog_works_with_no_watchdog():
    ws = _FakeWebSocket([{"text": "one"}])
    received = []
    async for message in receive_with_watchdog(ws, None):
        received.append(message)
        break
    assert received == [{"text": "one"}]


@pytest.mark.asyncio
async def test_build_identity_watchdog_none_for_unowned_identity():
    from app.core.auth_guard import WsIdentity
    from app.core.identity_watch import build_identity_watchdog

    assert build_identity_watchdog(WsIdentity(user_id=None, device_id=None)) is None


@pytest.mark.asyncio
async def test_build_identity_watchdog_fires_when_user_disabled():
    from app.core.auth_guard import WsIdentity
    from app.core.identity_watch import build_identity_watchdog
    from app.services.auth.users import user_store

    user = await user_store.create("toan", "pw")
    watchdog = build_identity_watchdog(WsIdentity(user_id=user["id"], device_id=None), interval_s=0.01)
    assert watchdog is not None
    watchdog.start()
    await asyncio.sleep(0.03)
    assert watchdog.invalid is False
    await user_store.set_fields(user["id"], disabled=True)
    await asyncio.sleep(0.03)
    assert watchdog.invalid is True


@pytest.mark.asyncio
async def test_build_identity_watchdog_fires_when_device_revoked():
    from app.core.auth_guard import WsIdentity
    from app.core.identity_watch import build_identity_watchdog
    from app.services.auth.devices import device_store
    from app.services.auth.users import user_store

    user = await user_store.create("toan", "pw")
    device, _ = await device_store.create(user["id"], "ESP32", "AA:BB:CC")
    identity = WsIdentity(user_id=user["id"], device_id=device["id"])
    watchdog = build_identity_watchdog(identity, interval_s=0.01)
    watchdog.start()
    await device_store.revoke(device["id"])
    await asyncio.sleep(0.03)
    assert watchdog.invalid is True
