import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.model_registry.availability import is_artifact_installed
from app.services.model_registry.store import model_registry_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _login_admin(client, username="embadm"):
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


@pytest.fixture
def _fake_embeddings(monkeypatch):
    """Stand in for the provider's /embeddings endpoint during the add-time test call."""
    calls = {}

    async def fake_post(self, url, headers=None, json=None):
        calls["url"] = url
        calls["json"] = json

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 3}}

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def test_create_embed_entry_runs_a_live_embed_test_then_persists(
    client, _with_password, _fake_embeddings
):
    _login_admin(client)
    resp = client.post("/v1/model_registry", json={
        "kind": "embed", "engine": "openai", "model_id": "text-embedding-3-small",
        "label": "OpenAI embed small", "base_url": "http://llm.local/v1", "api_key": "k",
        "config": {"price": {"in": 0.02}},
    })
    assert resp.status_code == 200, resp.text
    assert _fake_embeddings["url"] == "http://llm.local/v1/embeddings"
    created = resp.json()["data"]
    assert created["kind"] == "embed"
    assert created["config"]["price"] == {"unit": "1M_tokens", "in": 0.02, "out": 0.0}


def test_embed_entries_are_service_and_need_a_base_url(client, _with_password, _fake_embeddings):
    _login_admin(client)
    client.post("/v1/model_registry", json={
        "kind": "embed", "engine": "openai", "model_id": "text-embedding-3-large",
        "label": "OpenAI embed large", "base_url": "http://llm.local/v1",
    })
    rows = client.get("/v1/model_registry").json()["data"]
    row = next(r for r in rows if r["model_id"] == "text-embedding-3-large")
    assert row["location"] == "service"
    assert row["requires_base_url"] is True


def test_options_accepts_the_embed_kind(client, _with_password):
    _login_admin(client)
    assert client.get("/v1/model_registry/options?kind=embed").status_code == 200
    assert client.get("/v1/model_registry/options?kind=bogus").status_code == 400


def test_embed_has_no_artifact_install_gate():
    # There is no local artifact for a remote embedding model; None means
    # "not applicable" and must not block enabling the row.
    assert is_artifact_installed("embed", "openai", "text-embedding-3-small") is None


def test_recorder_prices_embed_usage_through_an_embed_registry_row():
    from sqlalchemy import select

    from app.services.db.engine import db_session, init_db
    from app.services.db.models import UsageEvent
    from app.services.usage.recorder import record_usage

    async def _run():
        await init_db()
        await model_registry_store.create(
            "embed", "openai", "text-embedding-3-priced", "priced embed",
            config={"provider_id": "prov-e", "price": {"unit": "1M_tokens", "in": 0.02, "out": 0.0}},
        )
        await record_usage(user_id="u1", profile_id="p1", kind="embed", engine="openai",
                           model_id="text-embedding-3-priced", unit="tokens",
                           native_amount=1_000_000, prompt_tokens=1_000_000)
        async with db_session() as s:
            rows = (await s.execute(select(UsageEvent))).scalars().all()
        return [r for r in rows if r.model_id == "text-embedding-3-priced"]

    rows = asyncio.run(_run())
    assert len(rows) == 1
    assert rows[0].provider_id == "prov-e"
    assert abs(rows[0].cost_usd - 0.02) < 1e-12
    assert rows[0].kind == "embed"
