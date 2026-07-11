import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_create_list_users(client):
    resp = client.post("/v1/users", json={"username": "toan", "password": "pw", "role": "user"})
    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "toan"

    resp = client.get("/v1/users")
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()["data"]]
    assert usernames == ["toan"]


def test_create_duplicate_username_409(client):
    client.post("/v1/users", json={"username": "toan", "password": "pw"})
    resp = client.post("/v1/users", json={"username": "toan", "password": "pw2"})
    assert resp.status_code == 409


def test_create_invalid_role_400(client):
    resp = client.post("/v1/users", json={"username": "toan", "password": "pw", "role": "superuser"})
    assert resp.status_code == 400


def test_patch_disabled_role_testing(client):
    created = client.post("/v1/users", json={"username": "toan", "password": "pw"}).json()["data"]
    resp = client.patch(f"/v1/users/{created['id']}", json={"disabled": True, "can_use_testing": True})
    assert resp.status_code == 200
    assert resp.json()["data"]["disabled"] is True
    assert resp.json()["data"]["can_use_testing"] is True


def test_patch_missing_user_404(client):
    resp = client.patch("/v1/users/does-not-exist", json={"disabled": True})
    assert resp.status_code == 404


def test_reset_password(client):
    created = client.post("/v1/users", json={"username": "toan", "password": "old-pw"}).json()["data"]
    resp = client.post(f"/v1/users/{created['id']}/reset_password", json={"new_password": "new-pw"})
    assert resp.status_code == 200

    from app.services.auth.users import user_store
    import asyncio

    assert asyncio.run(user_store.verify_login("toan", "old-pw")) is None
    assert asyncio.run(user_store.verify_login("toan", "new-pw")) is not None


def test_reset_password_missing_user_404(client):
    resp = client.post("/v1/users/does-not-exist/reset_password", json={"new_password": "x"})
    assert resp.status_code == 404
