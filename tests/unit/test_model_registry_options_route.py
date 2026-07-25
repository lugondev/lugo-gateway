import pytest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    from app.core.settings import settings
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


def test_options_is_readable_by_non_admin_but_registry_list_is_not(client, _with_password):
    """The options endpoint is THE feed every user's profile-editor dropdowns
    read, so a logged-in non-admin must get 200 from it -- even though the rest
    of /v1/model_registry (the admin CRUD surface) stays admin-only 403. This
    pins the _USER_PREFIXES carve-out for /v1/model_registry/options."""
    _signup_login(client, "regular_dropdown_user", role="user")

    opts = client.get("/v1/model_registry/options?kind=llm")
    assert opts.status_code == 200

    admin_list = client.get("/v1/model_registry")
    assert admin_list.status_code == 403


async def test_options_returns_enabled_entries_for_kind(client):
    from app.services.model_registry.store import model_registry_store
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True)
    await model_registry_store.create("tts", "vieneu", "v3turbo", "VieNeu", enabled=True)

    resp = client.get("/v1/model_registry/options?kind=stt")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == [{"engine": "whisper", "model_id": "tiny", "label": "Tiny"}]


async def test_options_rejects_unknown_kind(client):
    resp = client.get("/v1/model_registry/options?kind=bogus")
    assert resp.status_code == 400


def test_qwencloud_is_a_fixed_endpoint_service_engine():
    from app.api.routes.model_registry import _location, _requires_base_url
    assert _location("stt", "qwencloud") == "service"
    assert _requires_base_url("stt", "qwencloud") is False
