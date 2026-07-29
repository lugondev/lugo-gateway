import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _login_admin(client, username="pricadm"):
    from app.services.auth.users import user_store
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    user = asyncio.run(user_store.get_by_username(username))
    asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def _seed_entry(kind="llm", engine="openai", model_id="gpt-4o-mini", config=None):
    asyncio.run(init_db())
    return asyncio.run(
        model_registry_store.create(
            kind, engine, model_id, f"{engine}/{model_id}",
            config=config if config is not None else {"provider_id": "prov-1"},
        )
    )


def test_regular_user_cannot_reach_prices(client, _with_password):
    client.post("/api/auth/signup", json={"username": "bobprice", "password": "pw"})
    client.post("/api/auth/login", json={"username": "bobprice", "password": "pw"})
    assert client.get("/v1/model_registry/prices").status_code == 403


def test_list_prices_includes_unpriced_rows_and_the_kinds_unit(client, _with_password):
    _login_admin(client)
    entry = _seed_entry()
    rows = client.get("/v1/model_registry/prices").json()["data"]
    row = next(r for r in rows if r["id"] == entry["id"])
    assert row["price"] is None
    assert row["unit"] == "1M_tokens"
    assert row["provider_id"] == "prov-1"
    assert row["kind"] == "llm" and row["model_id"] == "gpt-4o-mini"


def test_bulk_patch_sets_price_and_preserves_provider_id(client, _with_password):
    _login_admin(client)
    entry = _seed_entry(model_id="gpt-4o-price")
    resp = client.patch("/v1/model_registry/prices", json={
        "prices": [{"id": entry["id"], "price": {"in": 0.15, "out": 0.6}}],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["updated"] == 1
    stored = asyncio.run(model_registry_store.get(entry["id"]))
    assert stored["config"] == {
        "provider_id": "prov-1",
        "price": {"unit": "1M_tokens", "in": 0.15, "out": 0.6},
    }


def test_bulk_patch_null_price_clears_it(client, _with_password):
    _login_admin(client)
    entry = _seed_entry(
        model_id="gpt-4o-clear",
        config={"provider_id": "prov-1", "price": {"unit": "1M_tokens", "in": 1.0, "out": 2.0}},
    )
    resp = client.patch("/v1/model_registry/prices",
                        json={"prices": [{"id": entry["id"], "price": None}]})
    assert resp.status_code == 200, resp.text
    assert asyncio.run(model_registry_store.get(entry["id"]))["config"] == {"provider_id": "prov-1"}


def test_bulk_patch_rejects_all_or_nothing_on_a_bad_price(client, _with_password):
    _login_admin(client)
    good = _seed_entry(model_id="gpt-4o-good")
    bad = _seed_entry(model_id="gpt-4o-bad")
    resp = client.patch("/v1/model_registry/prices", json={"prices": [
        {"id": good["id"], "price": {"in": 0.15}},
        {"id": bad["id"], "price": {"input": 0.15}},
    ]})
    assert resp.status_code == 400
    assert "unknown price field" in resp.json()["detail"]
    # The valid row must NOT have been written -- a half-applied price table is
    # worse than a rejected one, the admin can't tell which rows landed.
    assert "price" not in asyncio.run(model_registry_store.get(good["id"]))["config"]


def test_bulk_patch_unknown_id_is_404(client, _with_password):
    _login_admin(client)
    resp = client.patch("/v1/model_registry/prices",
                        json={"prices": [{"id": "nope", "price": {"in": 1.0}}]})
    assert resp.status_code == 404


def test_create_rejects_a_bad_price_before_any_network_call(client, _with_password):
    _login_admin(client)
    # No httpx mocking on purpose: validation runs before the add-time test
    # call, so a bad price must 400 without the route ever reaching out.
    resp = client.post("/v1/model_registry", json={
        "kind": "llm", "engine": "openai", "model_id": "m", "label": "M",
        "base_url": "http://127.0.0.1:9/v1",
        "config": {"price": {"unit": "minute", "rate": 1.0}},
    })
    assert resp.status_code == 400
    assert "must be '1M_tokens'" in resp.json()["detail"]


def test_patch_entry_config_validates_price(client, _with_password):
    _login_admin(client)
    entry = _seed_entry(model_id="gpt-4o-patch")
    resp = client.patch(f"/v1/model_registry/{entry['id']}",
                        json={"config": {"price": {"in": "cheap"}}})
    assert resp.status_code == 400
    assert "must be a number" in resp.json()["detail"]

    ok = client.patch(f"/v1/model_registry/{entry['id']}",
                      json={"config": {"provider_id": "prov-1", "price": {"in": 0.2}}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["config"]["price"] == {"unit": "1M_tokens", "in": 0.2, "out": 0.0}
