import pytest

from app.services.auth.devices import DeviceStore
from app.services.auth.users import user_store


@pytest.fixture
def store():
    return DeviceStore()


async def _make_user(username="toan") -> str:
    created = await user_store.create(username, "pw")
    return created["id"]


@pytest.mark.asyncio
async def test_create_returns_device_and_raw_token(store):
    user_id = await _make_user()
    device, raw_token = await store.create(user_id, "ESP32 desk", "AA:BB:CC")
    assert device["user_id"] == user_id
    assert device["name"] == "ESP32 desk"
    assert device["serial"] == "AA:BB:CC"
    assert device["revoked"] is False
    assert isinstance(raw_token, str) and len(raw_token) > 16


@pytest.mark.asyncio
async def test_get_by_token_roundtrip(store):
    user_id = await _make_user()
    device, raw_token = await store.create(user_id, "ESP32", "AA:BB:CC")
    found = await store.get_by_token(raw_token)
    assert found is not None
    assert found.id == device["id"]
    assert await store.get_by_token("wrong-token") is None


@pytest.mark.asyncio
async def test_find_active_by_serial_ignores_revoked(store):
    user_id = await _make_user()
    device, _ = await store.create(user_id, "ESP32", "AA:BB:CC")
    assert (await store.find_active_by_serial("AA:BB:CC")).id == device["id"]
    await store.revoke(device["id"])
    assert await store.find_active_by_serial("AA:BB:CC") is None


@pytest.mark.asyncio
async def test_get_by_id(store):
    user_id = await _make_user()
    device, _ = await store.create(user_id, "ESP32", "AA:BB:CC")
    found = await store.get_by_id(device["id"])
    assert found is not None and found.id == device["id"]
    assert await store.get_by_id("missing-id") is None


@pytest.mark.asyncio
async def test_list_for_user_and_list_all(store):
    u1 = await _make_user("a")
    u2 = await _make_user("b")
    await store.create(u1, "dev1", "S1")
    await store.create(u2, "dev2", "S2")
    assert len(await store.list_for_user(u1)) == 1
    assert len(await store.list_for_user(u2)) == 1
    assert len(await store.list_all()) == 2


@pytest.mark.asyncio
async def test_revoke_scoped_to_owner(store):
    u1 = await _make_user("a")
    u2 = await _make_user("b")
    device, _ = await store.create(u1, "dev1", "S1")
    assert await store.revoke(device["id"], owner_user_id=u2) is False
    assert await store.revoke(device["id"], owner_user_id=u1) is True
    assert await store.revoke("missing-id") is False


@pytest.mark.asyncio
async def test_touch_last_seen(store):
    user_id = await _make_user()
    device, _ = await store.create(user_id, "ESP32", "AA:BB:CC")
    assert (await store.list_for_user(user_id))[0]["last_seen_at"] is None
    await store.touch_last_seen(device["id"])
    assert (await store.list_for_user(user_id))[0]["last_seen_at"] is not None
