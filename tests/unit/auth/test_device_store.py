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


@pytest.mark.asyncio
async def test_create_defaults_to_unassigned_and_can_bind_at_creation(store):
    user_id = await _make_user()
    loose, _ = await store.create(user_id, "loose", "S1")
    bound, _ = await store.create(user_id, "bound", "S2", profile_id="kitchen")
    assert loose["profile_id"] == ""
    assert bound["profile_id"] == "kitchen"


@pytest.mark.asyncio
async def test_set_profile_binds_rebinds_and_unbinds(store):
    user_id = await _make_user()
    device, _ = await store.create(user_id, "ESP32", "AA:BB:CC")

    assert await store.set_profile(device["id"], "kitchen", owner_user_id=user_id) is True
    assert (await store.list_for_user(user_id))[0]["profile_id"] == "kitchen"

    # Moving between assistants must not need a re-pair: same device row, same token.
    assert await store.set_profile(device["id"], "study", owner_user_id=user_id) is True
    assert (await store.list_for_user(user_id))[0]["profile_id"] == "study"

    assert await store.set_profile(device["id"], "", owner_user_id=user_id) is True
    assert (await store.list_for_user(user_id))[0]["profile_id"] == ""


@pytest.mark.asyncio
async def test_set_profile_refuses_someone_elses_device(store):
    owner = await _make_user("owner")
    attacker = await _make_user("attacker")
    device, _ = await store.create(owner, "ESP32", "AA:BB:CC")

    assert await store.set_profile(device["id"], "mine", owner_user_id=attacker) is False
    assert (await store.list_for_user(owner))[0]["profile_id"] == ""
    assert await store.set_profile("missing-id", "mine", owner_user_id=owner) is False


@pytest.mark.asyncio
async def test_set_profile_refuses_revoked_device(store):
    user_id = await _make_user()
    device, _ = await store.create(user_id, "ESP32", "AA:BB:CC")
    await store.revoke(device["id"], owner_user_id=user_id)
    assert await store.set_profile(device["id"], "kitchen", owner_user_id=user_id) is False


@pytest.mark.asyncio
async def test_clear_profile_unbinds_only_matching_devices_without_revoking(store):
    user_id = await _make_user()
    a, _ = await store.create(user_id, "a", "S1", profile_id="kitchen")
    b, _ = await store.create(user_id, "b", "S2", profile_id="kitchen")
    c, _ = await store.create(user_id, "c", "S3", profile_id="study")

    assert await store.clear_profile("kitchen") == 2
    by_id = {d["id"]: d for d in await store.list_for_user(user_id)}
    assert by_id[a["id"]]["profile_id"] == ""
    assert by_id[b["id"]]["profile_id"] == ""
    assert by_id[c["id"]]["profile_id"] == "study"
    # Losing an assistant must never cost a physical re-pairing trip.
    assert all(d["revoked"] is False for d in by_id.values())


@pytest.mark.asyncio
async def test_clear_profile_ignores_the_empty_name(store):
    """Guard: "" is the unassigned marker, so clearing it would be a no-op sweep
    over every unbound device rather than a targeted one."""
    user_id = await _make_user()
    await store.create(user_id, "a", "S1")
    assert await store.clear_profile("") == 0
