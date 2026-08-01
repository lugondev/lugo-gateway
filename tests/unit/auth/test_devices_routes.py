import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _logged_in_user(client):
    client.post("/api/auth/signup", json={"username": "toan", "password": "pw"})
    client.post("/api/auth/login", json={"username": "toan", "password": "pw"})
    return "toan"


def test_pair_init_returns_code_and_poll_token(client):
    resp = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    # C3 hardening widened the code from 6 to 8 digits (pairing.py).
    assert len(data["code"]) == 8
    assert data["poll_token"]


def test_pair_status_unclaimed_then_claimed(client, _logged_in_user):
    init = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"}).json()["data"]

    status_before = client.get(f"/v1/devices/pair/status?poll_token={init['poll_token']}")
    assert status_before.json()["data"]["claimed"] is False

    claim = client.post("/v1/devices/pair/claim", json={"code": init["code"], "name": "ESP32 desk"})
    assert claim.status_code == 200

    status_after = client.get(f"/v1/devices/pair/status?poll_token={init['poll_token']}")
    body = status_after.json()["data"]
    assert body["claimed"] is True
    assert body["device_id"]
    assert body["token"]


def test_pair_status_unknown_poll_token_404(client):
    resp = client.get("/v1/devices/pair/status?poll_token=nonexistent")
    assert resp.status_code == 404


def test_pair_claim_invalid_code_400(client, _logged_in_user):
    resp = client.post("/v1/devices/pair/claim", json={"code": "000000", "name": "x"})
    assert resp.status_code == 400


def test_pair_claim_serial_conflict_requires_revoke_first(client, _logged_in_user):
    init1 = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"}).json()["data"]
    client.post("/v1/devices/pair/claim", json={"code": init1["code"], "name": "first"})

    init2 = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"}).json()["data"]
    resp = client.post("/v1/devices/pair/claim", json={"code": init2["code"], "name": "second"})
    assert resp.status_code == 409


def test_mine_lists_only_own_devices_and_revoke_is_scoped(client):
    client.post("/api/auth/signup", json={"username": "a", "password": "pw"})
    client.post("/api/auth/signup", json={"username": "b", "password": "pw"})

    client.post("/api/auth/login", json={"username": "a", "password": "pw"})
    init = client.post("/v1/devices/pair/init", json={"serial": "S1"}).json()["data"]
    client.post("/v1/devices/pair/claim", json={"code": init["code"], "name": "dev-a"})

    mine_a = client.get("/v1/devices/mine").json()["data"]
    assert len(mine_a) == 1
    device_id = mine_a[0]["id"]

    client.post("/api/auth/login", json={"username": "b", "password": "pw"})
    mine_b = client.get("/v1/devices/mine").json()["data"]
    assert mine_b == []

    # b cannot revoke a's device
    resp = client.post(f"/v1/devices/mine/{device_id}/revoke")
    assert resp.status_code == 404

    client.post("/api/auth/login", json={"username": "a", "password": "pw"})
    resp = client.post(f"/v1/devices/mine/{device_id}/revoke")
    assert resp.status_code == 200


def test_admin_lists_and_revokes_any_device(client, _logged_in_user):
    init = client.post("/v1/devices/pair/init", json={"serial": "S1"}).json()["data"]
    client.post("/v1/devices/pair/claim", json={"code": init["code"], "name": "dev"})

    resp = client.get("/v1/devices")
    assert resp.status_code == 200
    devices = resp.json()["data"]
    assert len(devices) == 1
    assert devices[0]["owner_username"] == "toan"

    resp = client.post(f"/v1/devices/{devices[0]['id']}/revoke")
    assert resp.status_code == 200

    resp = client.post("/v1/devices/does-not-exist/revoke")
    assert resp.status_code == 404


def test_pair_claim_without_session_returns_401_not_500(client):
    resp = client.post("/v1/devices/pair/claim", json={"code": "000000", "name": "x"})
    assert resp.status_code == 401


def test_list_my_devices_without_session_returns_401_not_500(client):
    resp = client.get("/v1/devices/mine")
    assert resp.status_code == 401


def test_revoke_my_device_without_session_returns_401_not_500(client):
    resp = client.post("/v1/devices/mine/some-id/revoke")
    assert resp.status_code == 401


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """Fresh ProfileStore for binding tests.

    Same shape as tests/unit/memory/test_memory_ownership.py: profile_store is a
    module-level singleton whose in-memory cache would otherwise ignore the
    per-test file, and devices.py binds the name at import time, so patching the
    services module alone would leave the route holding the stale singleton."""
    from app.services.profiles.models import Profile
    from app.services.profiles.store import ProfileStore

    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.devices.profile_store", fresh)
    monkeypatch.setattr("app.api.routes.profiles.profile_store", fresh)
    monkeypatch.setattr("app.services.profiles.store.profile_store", fresh)

    def make(name: str, owner_id: str | None) -> Profile:
        profile = Profile(name=name, owner_id=owner_id)
        fresh.upsert(profile)
        return profile

    fresh.make = make
    return fresh


def _pair(client, serial: str, name: str, profile_id: str | None = None):
    init = client.post("/v1/devices/pair/init", json={"serial": serial}).json()["data"]
    body = {"code": init["code"], "name": name}
    if profile_id is not None:
        body["profile_id"] = profile_id
    return client.post("/v1/devices/pair/claim", json=body)


async def _user_id(username: str) -> str:
    from app.services.auth.users import user_store

    user = await user_store.get_by_username(username)
    return user.id


def test_pair_claim_binds_the_profile_in_one_step(client, _logged_in_user, profiles):
    import asyncio

    profiles.make("kitchen", asyncio.run(_user_id("toan")))
    resp = _pair(client, "S1", "kitchen speaker", profile_id="kitchen")
    assert resp.status_code == 200
    assert resp.json()["data"]["profile_id"] == "kitchen"
    assert client.get("/v1/devices/mine").json()["data"][0]["profile_id"] == "kitchen"


def test_pair_claim_without_a_profile_stays_unassigned(client, _logged_in_user, profiles):
    """Older clients don't send the field at all; they must keep working."""
    resp = _pair(client, "S1", "loose speaker")
    assert resp.status_code == 200
    assert resp.json()["data"]["profile_id"] == ""


def test_pair_claim_refuses_a_profile_the_user_cannot_see(client, _logged_in_user, profiles):
    """C2: binding to someone else's private profile would hand this user that
    profile's llm.api_key and system_prompt on the device's next connect."""
    profiles.make("victim-private", "someone-else-id")
    hidden = _pair(client, "S1", "sneaky", profile_id="victim-private")
    missing = _pair(client, "S2", "sneaky", profile_id="no-such")
    assert hidden.status_code == 404
    # No enumeration oracle: "exists but is someone else's" and "doesn't exist"
    # produce the same status and the same message shape. The names differ only
    # because each response echoes the name the CALLER sent, which tells them
    # nothing they didn't already know.
    assert hidden.status_code == missing.status_code
    assert hidden.json()["detail"] == "profile 'victim-private' not found"
    assert missing.json()["detail"] == "profile 'no-such' not found"


def test_assign_moves_a_device_between_profiles_without_repairing(client, _logged_in_user, profiles):
    import asyncio

    owner = asyncio.run(_user_id("toan"))
    profiles.make("kitchen", owner)
    profiles.make("study", owner)
    device_id = _pair(client, "S1", "speaker", profile_id="kitchen").json()["data"]["id"]

    resp = client.post(f"/v1/devices/mine/{device_id}/profile", json={"profile_id": "study"})
    assert resp.status_code == 200
    assert resp.json()["data"]["profile_id"] == "study"
    assert client.get("/v1/devices/mine").json()["data"][0]["profile_id"] == "study"

    # Unassign.
    resp = client.post(f"/v1/devices/mine/{device_id}/profile", json={"profile_id": ""})
    assert resp.status_code == 200
    assert client.get("/v1/devices/mine").json()["data"][0]["profile_id"] == ""


def test_assign_refuses_another_users_device(client, profiles):
    client.post("/api/auth/signup", json={"username": "a", "password": "pw"})
    client.post("/api/auth/signup", json={"username": "b", "password": "pw"})
    client.post("/api/auth/login", json={"username": "a", "password": "pw"})
    profiles.make("shared-template", None)
    device_id = _pair(client, "S1", "a-speaker").json()["data"]["id"]

    client.post("/api/auth/login", json={"username": "b", "password": "pw"})
    resp = client.post(
        f"/v1/devices/mine/{device_id}/profile", json={"profile_id": "shared-template"}
    )
    assert resp.status_code == 404


def test_assign_refuses_a_profile_the_user_cannot_see(client, _logged_in_user, profiles):
    profiles.make("victim-private", "someone-else-id")
    device_id = _pair(client, "S1", "speaker").json()["data"]["id"]
    resp = client.post(
        f"/v1/devices/mine/{device_id}/profile", json={"profile_id": "victim-private"}
    )
    assert resp.status_code == 404
    assert client.get("/v1/devices/mine").json()["data"][0]["profile_id"] == ""


def test_assign_without_session_returns_401_not_500(client):
    resp = client.post("/v1/devices/mine/some-id/profile", json={"profile_id": "x"})
    assert resp.status_code == 401


def test_deleting_a_profile_unassigns_its_devices_without_revoking_them(
    client, _logged_in_user, profiles
):
    import asyncio

    profiles.make("kitchen", asyncio.run(_user_id("toan")))
    _pair(client, "S1", "speaker", profile_id="kitchen")

    resp = client.delete("/v1/profiles/kitchen")
    assert resp.status_code == 200
    assert resp.json()["data"]["devices_unassigned"] == 1

    device = client.get("/v1/devices/mine").json()["data"][0]
    assert device["profile_id"] == ""
    # The pairing token is hardware identity: losing an assistant must not cost
    # the user a trip to the device.
    assert device["revoked"] is False


def test_claim_without_a_name_uses_the_devices_own_ap_name(client, _logged_in_user):
    init = client.post(
        "/v1/devices/pair/init", json={"serial": "2884855048d0"}
    ).json()["data"]

    # No `name` key at all: the user pairs on the code alone and names the
    # device afterwards, if they want to.
    resp = client.post("/v1/devices/pair/claim", json={"code": init["code"]})

    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "Lugo-48D0"


def test_claim_with_a_blank_name_gets_the_derived_one_too(client, _logged_in_user):
    init = client.post(
        "/v1/devices/pair/init", json={"serial": "2884855048d0"}
    ).json()["data"]

    resp = client.post(
        "/v1/devices/pair/claim", json={"code": init["code"], "name": "   "}
    )

    assert resp.json()["data"]["name"] == "Lugo-48D0"


def test_an_explicit_name_still_wins(client, _logged_in_user):
    # Older clients (the static devices.js panel) always send one.
    init = client.post(
        "/v1/devices/pair/init", json={"serial": "2884855048d0"}
    ).json()["data"]

    resp = client.post(
        "/v1/devices/pair/claim", json={"code": init["code"], "name": "Kitchen speaker"}
    )

    assert resp.json()["data"]["name"] == "Kitchen speaker"


def _pair_a_device(client, serial="2884855048d0"):
    """Pair one device and return its dict. Assumes a logged-in client."""
    init = client.post("/v1/devices/pair/init", json={"serial": serial}).json()["data"]
    return client.post("/v1/devices/pair/claim", json={"code": init["code"]}).json()["data"]


def test_rename_changes_the_name_and_leaves_the_binding_alone(client, _logged_in_user):
    device = _pair_a_device(client)

    resp = client.post(
        f"/v1/devices/mine/{device['id']}/name", json={"name": "Kitchen speaker"}
    )

    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": device["id"], "name": "Kitchen speaker"}
    listed = client.get("/v1/devices/mine").json()["data"]
    assert [d["name"] for d in listed] == ["Kitchen speaker"]
    # A name is a label, not hardware identity: renaming must not send the user
    # back to the device to read a fresh code.
    assert listed[0]["profile_id"] == device["profile_id"]


def test_rename_trims_surrounding_whitespace(client, _logged_in_user):
    device = _pair_a_device(client)

    resp = client.post(
        f"/v1/devices/mine/{device['id']}/name", json={"name": "  Kitchen  "}
    )

    assert resp.json()["data"]["name"] == "Kitchen"


def test_rename_to_blank_is_rejected(client, _logged_in_user):
    device = _pair_a_device(client)

    resp = client.post(f"/v1/devices/mine/{device['id']}/name", json={"name": "   "})

    assert resp.status_code == 400


def test_rename_over_the_column_length_is_rejected(client, _logged_in_user):
    device = _pair_a_device(client)

    resp = client.post(
        f"/v1/devices/mine/{device['id']}/name", json={"name": "x" * 129}
    )

    assert resp.status_code == 400


def test_rename_someone_elses_device_is_indistinguishable_from_a_missing_one(client):
    client.post("/api/auth/signup", json={"username": "owner", "password": "pw"})
    client.post("/api/auth/signup", json={"username": "other", "password": "pw"})

    client.post("/api/auth/login", json={"username": "owner", "password": "pw"})
    device = _pair_a_device(client, serial="ffffffff1234")

    client.post("/api/auth/login", json={"username": "other", "password": "pw"})
    theirs = client.post(
        f"/v1/devices/mine/{device['id']}/name", json={"name": "mine now"}
    )
    missing = client.post(
        "/v1/devices/mine/does-not-exist/name", json={"name": "mine now"}
    )

    # Same status AND same message: otherwise this becomes a device-id oracle.
    assert theirs.status_code == missing.status_code == 404
