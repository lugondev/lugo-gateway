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


def _seed(**kwargs) -> dict:
    """Create a quota row straight through the store.

    These shapes predate the branch's validation and the API now refuses to
    create them, so the store is the only way to reproduce a legacy row.
    """
    from app.services.quota.store import quota_store

    async def _go():
        from app.services.db.engine import init_db

        await init_db()
        quota_store.invalidate()
        return await quota_store.create(**kwargs)

    return asyncio.run(_go())


def test_a_legacy_unfireable_limit_can_still_be_disabled(client, _with_password):
    """A row with limit_usd=0 fails _validate_limit, which used to reject even
    {"enabled": false} -- the admin UI's Disable button sends only that field,
    so the row became delete-only."""
    _login_admin(client, "q-legacy-limit")
    row = _seed(scope="global", scope_id="", limit_usd=0.0, period="monthly")
    r = client.patch(f"/v1/quotas/{row['id']}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["enabled"] is False


def test_a_legacy_scoped_row_with_no_scope_id_can_still_be_disabled(client, _with_password):
    """Same for the other pre-branch invalid shape: a scoped row with a blank
    scope_id. Disabling only ever loosens enforcement, so it must be allowed."""
    _login_admin(client, "q-legacy-scopeid")
    row = _seed(scope="user", scope_id="", limit_usd=5.0, period="monthly")
    r = client.patch(f"/v1/quotas/{row['id']}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["enabled"] is False


def test_disabling_still_rejects_a_bad_enum(client, _with_password):
    """The disable escape hatch loosens enforcement checks only -- scope/period
    still have to name something the store can hold."""
    _login_admin(client, "q-legacy-enum")
    row = _seed(scope="global", scope_id="", limit_usd=0.0, period="monthly")
    r = client.patch(f"/v1/quotas/{row['id']}", json={"enabled": False, "scope": "bogus"})
    assert r.status_code == 400, r.text
def test_a_global_rows_stray_scope_id_is_normalized_on_any_patch(client, _with_password):
    """_reject_duplicate compares the NORMALIZED scope_id, so leaving the stray
    one on the row made a plain limit_usd edit 400 as a duplicate of a clean
    global row -- and the row kept an id its scope can never use."""
    _login_admin(client, "q-stray")
    from app.services.quota.store import quota_store

    row = _seed(scope="global", scope_id="stray", limit_usd=5.0, period="monthly")
    r = client.patch(f"/v1/quotas/{row['id']}", json={"limit_usd": 9.0})
    assert r.status_code == 200, r.text
    stored = asyncio.run(quota_store.get(row["id"]))
    assert stored["scope_id"] == "", stored
    assert stored["limit_usd"] == 9.0


def test_quota_list_includes_current_spend(client, _with_password):
    """An admin cannot judge a limit without seeing what has been spent against it."""
    import asyncio

    from app.services.db.engine import init_db
    from app.services.model_registry.store import model_registry_store
    from app.services.usage.recorder import record_usage

    _login_admin(client, "q-spend")
    asyncio.run(init_db())
    asyncio.run(model_registry_store.create(
        "llm", "spend-eng", "spend-model", "Spend",
        config={"price": {"unit": "1M_tokens", "in": 3.0}},
    ))
    asyncio.run(record_usage(user_id="u-spend", profile_id="", kind="llm", engine="spend-eng",
                             model_id="spend-model", unit="tokens",
                             native_amount=1_000_000, prompt_tokens=1_000_000))  # $3
    created = client.post("/v1/quotas", json={
        "scope": "user", "scope_id": "u-spend", "limit_usd": 10.0, "period": "monthly",
    }).json()["data"]

    row = next(q for q in client.get("/v1/quotas").json()["data"] if q["id"] == created["id"])
    assert abs(row["spend_usd"] - 3.0) < 1e-9
    assert row["limit_usd"] == 10.0
