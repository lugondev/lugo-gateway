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
    admin_id = _signup_login(client, "root", role="admin")
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


def test_login_required(client, _with_password):
    resp = client.get("/v1/stats/home")
    assert resp.status_code in (401, 403)
