import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _login_admin(client, username="adm"):
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_regular_user_cannot_reach_quotas(client, _with_password):
    client.post("/api/auth/signup", json={"username": "bob", "password": "pw"})
    client.post("/api/auth/login", json={"username": "bob", "password": "pw"})
    assert client.get("/v1/quotas").status_code == 403


def test_admin_crud(client, _with_password):
    _login_admin(client)
    # create
    resp = client.post("/v1/quotas", json={
        "scope": "user", "scope_id": "u1", "limit_usd": 10.0, "period": "monthly",
    })
    assert resp.status_code == 200, resp.text
    created = resp.json()["data"]
    assert created["scope"] == "user"
    assert created["scope_id"] == "u1"
    assert created["limit_usd"] == 10.0
    assert created["period"] == "monthly"
    assert created["enabled"] is True

    # list
    listed = client.get("/v1/quotas").json()["data"]
    assert any(q["id"] == created["id"] for q in listed)

    # patch
    r = client.patch(f"/v1/quotas/{created['id']}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["enabled"] is False

    # delete
    d = client.delete(f"/v1/quotas/{created['id']}")
    assert d.status_code == 200, d.text
    assert d.json()["data"]["deleted"] is True


def test_create_rejects_bad_scope(client, _with_password):
    _login_admin(client)
    resp = client.post("/v1/quotas", json={
        "scope": "bogus", "limit_usd": 5.0,
    })
    assert resp.status_code == 400


def test_create_rejects_bad_period(client, _with_password):
    _login_admin(client)
    resp = client.post("/v1/quotas", json={
        "scope": "global", "limit_usd": 5.0, "period": "bogus",
    })
    assert resp.status_code == 400


def test_patch_rejects_bad_scope_and_period(client, _with_password):
    _login_admin(client)
    created = client.post("/v1/quotas", json={
        "scope": "provider", "scope_id": "p1", "limit_usd": 1.0,
    }).json()["data"]

    r = client.patch(f"/v1/quotas/{created['id']}", json={"scope": "bogus"})
    assert r.status_code == 400

    r = client.patch(f"/v1/quotas/{created['id']}", json={"period": "bogus"})
    assert r.status_code == 400


def test_create_rejects_a_scoped_quota_with_no_scope_id(client, _with_password):
    """A blank scope_id on a user scope matches the shared-device bucket, not
    the person the admin had in mind."""
    _login_admin(client, "q-scopeid")
    for scope in ("user", "provider"):
        resp = client.post("/v1/quotas", json={"scope": scope, "scope_id": "  ", "limit_usd": 5.0})
        assert resp.status_code == 400, f"{scope}: {resp.text}"
        assert "scope_id" in resp.json()["detail"]


def test_global_scope_normalizes_its_scope_id_away(client, _with_password):
    _login_admin(client, "q-global")
    created = client.post(
        "/v1/quotas", json={"scope": "global", "scope_id": "ignored", "limit_usd": 5.0},
    ).json()["data"]
    assert created["scope_id"] == ""


def test_create_rejects_a_limit_that_can_never_fire(client, _with_password):
    """The gate requires `limit_usd > 0`, so 0 and negatives are silently
    unlimited -- the opposite of what an admin setting 0 intends."""
    _login_admin(client, "q-limit")
    for bad in (0, -5.0):
        resp = client.post("/v1/quotas", json={"scope": "global", "limit_usd": bad})
        assert resp.status_code == 400, f"{bad}: {resp.text}"
        assert "greater than 0" in resp.json()["detail"]


def test_create_rejects_a_duplicate_scope(client, _with_password):
    _login_admin(client, "q-dup")
    first = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u1", "limit_usd": 5.0, "period": "monthly"},
    )
    assert first.status_code == 200
    dup = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u1", "limit_usd": 9.0, "period": "monthly"},
    )
    assert dup.status_code == 400
    assert "already" in dup.json()["detail"]

    # A different period for the same scope is a legitimate second quota.
    other = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u1", "limit_usd": 50.0, "period": "total"},
    )
    assert other.status_code == 200, other.text


def test_patch_is_validated_the_same_way(client, _with_password):
    _login_admin(client, "q-patch")
    created = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u2", "limit_usd": 5.0},
    ).json()["data"]

    assert client.patch(f"/v1/quotas/{created['id']}", json={"limit_usd": 0}).status_code == 400
    assert client.patch(f"/v1/quotas/{created['id']}", json={"scope_id": ""}).status_code == 400

    # Editing a row into a collision with another row is also a duplicate.
    other = client.post(
        "/v1/quotas", json={"scope": "user", "scope_id": "u3", "limit_usd": 5.0},
    ).json()["data"]
    collide = client.patch(f"/v1/quotas/{other['id']}", json={"scope_id": "u2"})
    assert collide.status_code == 400
    assert "already" in collide.json()["detail"]

    # A no-op edit of an unrelated field must still be allowed.
    assert client.patch(f"/v1/quotas/{created['id']}", json={"enabled": False}).status_code == 200
