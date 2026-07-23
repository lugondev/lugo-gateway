import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


async def _seed_rows(user_id: str) -> None:
    await init_db()
    async with db_session() as s:
        s.add(UsageEvent(
            id=str(uuid.uuid4()), ts=datetime.now(timezone.utc), user_id=user_id,
            profile_id="", provider_id="prov-a", kind="llm", engine="openrouter",
            model_id="qwen-max", unit="tokens", native_amount=100, cost_usd=1.0,
        ))
        s.add(UsageEvent(
            id=str(uuid.uuid4()), ts=datetime.now(timezone.utc), user_id="someone-else",
            profile_id="", provider_id="prov-b", kind="tts", engine="vieneu",
            model_id="v1", unit="chars", native_amount=50, cost_usd=0.5,
        ))
        await s.commit()


def test_admin_can_read_summary(client, _with_password):
    import asyncio

    asyncio.run(_seed_rows("u1"))
    _signup_login(client, "root", role="admin")
    resp = client.get("/v1/usage/summary", params={"group_by": "kind"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert isinstance(body["data"], list)
    assert len(body["data"]) >= 1


def test_admin_summary_bad_group_by_400(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.get("/v1/usage/summary", params={"group_by": "bogus"})
    assert resp.status_code == 400


def test_non_admin_forbidden_on_summary_but_ok_on_me(client, _with_password):
    import asyncio

    _signup_login(client, "toan", role="user")
    from app.services.auth.users import user_store

    user = asyncio.run(user_store.get_by_username("toan"))
    asyncio.run(_seed_rows(user.id))

    resp = client.get("/v1/usage/summary", params={"group_by": "kind"})
    assert resp.status_code == 403

    resp = client.get("/v1/usage/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["data"]) == 1
    assert body["data"][0]["kind"] == "llm"
    assert body["data"][0]["model_id"] == "qwen-max"
