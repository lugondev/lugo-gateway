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
    assert "engine" in body["data"][0]


def test_summary_labels_provider_ids_with_the_providers_name(client, _with_password):
    """The summary groups by raw provider_id; an admin reading the dashboard
    needs "Qwen Cloud", not "faba8dad-a8a5-...". Rows whose provider_id is blank
    (a local engine, no provider) get no label -- that is not the same thing as
    an unknown provider, and the client renders the two differently."""
    import asyncio

    from app.services.providers.store import provider_store

    _signup_login(client, "prov-label-adm", role="admin")
    asyncio.run(init_db())
    prov = asyncio.run(provider_store.create(name="qwencloud", label="Qwen Cloud (DashScope)"))
    asyncio.run(_record(provider_id=prov["id"]))
    asyncio.run(_record(provider_id=""))  # local engine

    rows = client.get("/v1/usage/summary?group_by=provider").json()["data"]
    labelled = next(r for r in rows if r["key"] == prov["id"])
    assert labelled["label"] == "Qwen Cloud (DashScope)"
    local = next(r for r in rows if r["key"] == "")
    assert local["label"] == ""


def test_summary_falls_back_to_the_provider_name_when_it_has_no_label(client, _with_password):
    import asyncio

    from app.services.providers.store import provider_store

    _signup_login(client, "prov-name-adm", role="admin")
    asyncio.run(init_db())
    prov = asyncio.run(provider_store.create(name="bare-openai", label=""))
    asyncio.run(_record(provider_id=prov["id"]))

    rows = client.get("/v1/usage/summary?group_by=provider").json()["data"]
    assert next(r for r in rows if r["key"] == prov["id"])["label"] == "bare-openai"


def test_summary_labels_user_ids_with_the_username(client, _with_password):
    import asyncio

    from app.services.auth.users import user_store

    _signup_login(client, "user-label-adm", role="admin")
    asyncio.run(init_db())
    me = asyncio.run(user_store.get_by_username("user-label-adm"))
    asyncio.run(_record(user_id=me.id))

    rows = client.get("/v1/usage/summary?group_by=user").json()["data"]
    assert next(r for r in rows if r["key"] == me.id)["label"] == "user-label-adm"


def test_summary_dimensions_without_ids_carry_no_label(client, _with_password):
    """kind/engine/model keys are already human-readable; inventing a label for
    them would just be a second copy of the key."""
    import asyncio

    _signup_login(client, "kind-label-adm", role="admin")
    asyncio.run(init_db())
    asyncio.run(_record(provider_id=""))

    for dim in ("kind", "engine", "model"):
        rows = client.get(f"/v1/usage/summary?group_by={dim}").json()["data"]
        assert rows, f"no rows for group_by={dim}"
        assert all("label" not in r for r in rows), f"group_by={dim} should not label"


def test_my_usage_reports_the_callers_own_limits(client, _with_password):
    """A user who gets a 429 must be able to see why. Their own user quota and
    the global one -- never another user's, never a provider's."""
    import asyncio

    from app.services.auth.users import user_store
    from app.services.quota.store import quota_store

    _signup_login(client, "limit-viewer")
    me = asyncio.run(user_store.get_by_username("limit-viewer"))
    asyncio.run(init_db())
    quota_store.invalidate()
    asyncio.run(quota_store.create(scope="user", scope_id=me.id, limit_usd=5.0, period="monthly"))
    asyncio.run(quota_store.create(scope="global", scope_id="", limit_usd=50.0, period="monthly"))
    asyncio.run(quota_store.create(scope="user", scope_id="someone-else", limit_usd=1.0,
                                   period="monthly"))
    asyncio.run(quota_store.create(scope="provider", scope_id="prov-x", limit_usd=2.0,
                                   period="monthly"))

    body = client.get("/v1/usage/me").json()
    scopes = sorted((l["scope"], l["limit_usd"]) for l in body["limits"])
    assert scopes == [("global", 50.0), ("user", 5.0)], f"leaked or missing limits: {body['limits']}"
    assert all("spend_usd" in l for l in body["limits"])
    # The existing shape must not change -- the React client reads `data`.
    assert isinstance(body["data"], list)


async def _record(*, provider_id: str = "", user_id: str = "u-label") -> None:
    await init_db()
    async with db_session() as s:
        s.add(UsageEvent(
            id=str(uuid.uuid4()), ts=datetime.now(timezone.utc), user_id=user_id,
            profile_id="", provider_id=provider_id, kind="llm", engine="openrouter",
            model_id="qwen-max", unit="tokens", native_amount=10, cost_usd=0.0,
        ))
        await s.commit()
