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
    assert len(data["code"]) == 6
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
