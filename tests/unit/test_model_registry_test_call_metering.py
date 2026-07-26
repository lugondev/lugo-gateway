"""The Model Registry's add-time test call really hits the provider and really
costs money. It is metered so the spend is visible, and deliberately NOT gated:
an admin over quota must still be able to validate the provider they need in
order to fix the config that put them over."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.settings import settings
from app.main import app
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


@pytest.fixture
def _with_admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    client = TestClient(app)
    client.post("/api/auth/signup", json={"username": "regadm", "password": "pw"})
    from app.services.auth.users import user_store
    user = asyncio.run(user_store.get_by_username("regadm"))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": "regadm", "password": "pw"})
    yield client
    monkeypatch.setattr(settings, "admin_password", "")


@pytest.fixture
def _fake_llm(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def _rows():
    async with db_session() as s:
        return list((await s.execute(select(UsageEvent))).scalars().all())


def test_the_add_time_test_call_is_metered(_with_admin, _fake_llm):
    resp = _with_admin.post("/v1/model_registry", json={
        "kind": "llm", "engine": "OA", "model_id": "gpt-4o-mini", "label": "OA mini",
        "base_url": "http://llm.local/v1", "api_key": "k",
    })
    assert resp.status_code == 200, resp.text
    rows = asyncio.run(_rows())
    metered = [r for r in rows if r.request_id == "registry-test-call"]
    assert len(metered) == 1, f"the add-time provider call was not metered: {rows}"
    assert metered[0].kind == "llm" and metered[0].model_id == "gpt-4o-mini"


def test_an_over_quota_admin_can_still_validate_a_provider(_with_admin, _fake_llm):
    """The recovery path: gating this call would trap an admin whose quota is
    exhausted, unable to test the credentials needed to fix it."""
    asyncio.run(init_db())
    quota_store.invalidate()
    asyncio.run(model_registry_store.create(
        "llm", "priced-eng", "priced-model", "Priced",
        config={"price": {"unit": "1M_tokens", "in": 100.0}},
    ))
    asyncio.run(record_usage(user_id="", profile_id="", kind="llm", engine="priced-eng",
                             model_id="priced-model", unit="tokens",
                             native_amount=1_000_000, prompt_tokens=1_000_000))
    asyncio.run(quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="monthly"))

    resp = _with_admin.post("/v1/model_registry", json={
        "kind": "llm", "engine": "OA2", "model_id": "gpt-4o-mini", "label": "OA mini 2",
        "base_url": "http://llm.local/v1", "api_key": "k",
    })
    assert resp.status_code == 200, f"an over-quota admin must still be able to test: {resp.text}"
