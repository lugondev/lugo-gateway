import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.db.engine import db_session
from app.services.db.models import Device
from app.services.history.store import session_store
from app.services.profiles.models import Profile
from app.services.profiles.store import profile_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> str:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    if role == "admin":
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})
    return user.id


def test_regular_user_sees_only_their_own_counts(client, _with_password):
    me_id = _signup_login(client, "toan", role="user")
    other_id = _signup_login(client, "khoa", role="user")

    profile_store.upsert(Profile(name="toan-profile", owner_id=me_id))
    profile_store.upsert(Profile(name="khoa-profile", owner_id=other_id))
    profile_store.upsert(Profile(name="template", owner_id=None))

    asyncio.run(device_store.create(me_id, "my-esp32", "serial-a"))
    asyncio.run(device_store.create(other_id, "their-esp32", "serial-b"))

    asyncio.run(session_store.create("s-mine", user_id=me_id))
    asyncio.run(session_store.create("s-theirs", user_id=other_id))

    # log back in as "toan" -- the last _signup_login call above left the
    # session logged in as "khoa"
    client.post("/api/auth/login", json={"username": "toan", "password": "pw"})

    resp = client.get("/v1/stats/home")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["profiles"]["count"] == 2  # their own + the template
    assert data["devices"]["count"] == 1
    assert data["sessions"]["count"] == 1


def test_admin_sees_global_counts(client, _with_password):
    _signup_login(client, "root", role="admin")
    other_id = _signup_login(client, "user1", role="user")
    client.post("/api/auth/login", json={"username": "root", "password": "pw"})

    asyncio.run(device_store.create(other_id, "device-1", "serial-x"))
    asyncio.run(session_store.create("s-x", user_id=other_id))

    resp = client.get("/v1/stats/home")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["devices"]["count"] >= 1
    assert data["sessions"]["count"] >= 1


def test_device_active_recent_reflects_last_seen(client, _with_password):
    me_id = _signup_login(client, "pat", role="user")

    device, _token = asyncio.run(device_store.create(me_id, "seen", "serial-seen"))
    asyncio.run(device_store.create(me_id, "unseen", "serial-unseen"))
    asyncio.run(device_store.touch_last_seen(device["id"]))

    resp = client.get("/v1/stats/home")
    assert resp.status_code == 200
    data = resp.json()["data"]["devices"]
    assert data["count"] == 2
    assert data["active_recent"] == 1


def test_device_active_recent_excludes_stale_last_seen(client, _with_password):
    me_id = _signup_login(client, "sam", role="user")

    fresh, _token = asyncio.run(device_store.create(me_id, "fresh", "serial-fresh"))
    stale, _token2 = asyncio.run(device_store.create(me_id, "stale", "serial-stale"))
    asyncio.run(device_store.touch_last_seen(fresh["id"]))
    asyncio.run(device_store.touch_last_seen(stale["id"]))

    async def _backdate():
        async with db_session() as s:
            row = await s.get(Device, stale["id"])
            row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=45)
            await s.commit()

    asyncio.run(_backdate())

    resp = client.get("/v1/stats/home")
    assert resp.status_code == 200
    data = resp.json()["data"]["devices"]
    assert data["count"] == 2
    assert data["active_recent"] == 1


def test_revoked_devices_are_excluded_from_both_counts(client, _with_password):
    """A revoked device is gone as far as the owner is concerned -- it cannot
    connect and the Devices tab offers to delete it. Counting it on Home would
    tell someone they still have hardware paired after they revoked it, and
    (worse) keep counting it as recently-active for the 30 minutes after, since
    revoking does not clear last_seen_at."""
    me_id = _signup_login(client, "mai", role="user")

    keep, _t1 = asyncio.run(device_store.create(me_id, "keep", "serial-keep"))
    gone, _t2 = asyncio.run(device_store.create(me_id, "gone", "serial-gone"))
    asyncio.run(device_store.touch_last_seen(keep["id"]))
    asyncio.run(device_store.touch_last_seen(gone["id"]))
    assert asyncio.run(device_store.revoke(gone["id"], owner_user_id=me_id))

    data = client.get("/v1/stats/home").json()["data"]["devices"]
    assert data["count"] == 1, "a revoked device is still counted as paired"
    assert data["active_recent"] == 1, "a revoked device is still counted as active"


def test_admin_profile_count_stays_personal_while_devices_go_global(client, _with_password):
    """Home scopes an admin's three counts differently on purpose, and the
    asymmetry looks like a bug until you check the routes behind each tile.

    Devices and sessions have admin-global routes (list_all / scope_user_id), so
    Home shows an admin the fleet-wide number and the Devices tab agrees.
    Profiles do NOT: GET /v1/profiles filters by the same `profile_visible`
    predicate for every role, so an admin's Profiles tab lists only templates
    plus their own. Making Home's profile tile global to "match" the other two
    would both disagree with the tab it links to and turn the count into an
    oracle for how many private profiles other users hold.
    """
    other_id = _signup_login(client, "hoa", role="user")
    admin_id = _signup_login(client, "boss", role="admin")

    profile_store.upsert(Profile(name="hoa-private", owner_id=other_id))
    profile_store.upsert(Profile(name="boss-private", owner_id=admin_id))
    profile_store.upsert(Profile(name="shared-template", owner_id=None))
    asyncio.run(device_store.create(other_id, "hoa-device", "serial-hoa"))

    client.post("/api/auth/login", json={"username": "boss", "password": "pw"})
    data = client.get("/v1/stats/home").json()["data"]

    assert data["profiles"]["count"] == 2, (
        "admin's profile count must be their own + templates, never every user's"
    )
    listed = len(client.get("/v1/profiles").json()["data"])
    assert data["profiles"]["count"] == listed, (
        "Home's profile tile disagrees with the Profiles list it links to"
    )
    assert data["devices"]["count"] >= 1, "admin should still see another user's device"


def test_login_required(client, _with_password):
    resp = client.get("/v1/stats/home")
    assert resp.status_code in (401, 403)
