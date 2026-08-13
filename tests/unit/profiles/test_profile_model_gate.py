import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.model_registry.store import ModelRegistryStore
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    # Mirrors tests/unit/profiles/test_profiles_routes.py and test_profile_ownership.py:
    # profile_store is a module-level singleton with an in-memory cache that,
    # once populated, ignores the fresh per-test SQLite file the autouse
    # tests/conftest.py `_tmp_db` fixture points the engine at -- writes would
    # silently target a tableless DB. A brand new ProfileStore (cache=None)
    # per test avoids that staleness.
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.profiles.profile_store", fresh)
    monkeypatch.setattr("app.services.profiles.store.profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str) -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


@pytest.mark.asyncio
async def test_disabled_llm_model_rejected_on_profile_create():
    store = ModelRegistryStore()
    await store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash", stage="stable")
    created = await store.find("llm", "openrouter", "qwen3-asr-flash")
    await store.set_fields(created["id"], enabled=False)


def test_profile_create_rejects_disabled_llm_engine(client, _with_password):
    import asyncio

    store = ModelRegistryStore()
    entry = asyncio.run(store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash"))
    asyncio.run(store.set_fields(entry["id"], enabled=False))

    _signup_login(client, "toan")
    resp = client.post("/v1/profiles", json={
        "name": "p1",
        "llm": {"engine": "openrouter", "model": "qwen3-asr-flash", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 403


def test_profile_create_allows_llm_engine_not_in_registry(client, _with_password):
    _signup_login(client, "toan")
    resp = client.post("/v1/profiles", json={
        "name": "p1",
        "llm": {"engine": "", "model": "my-self-hosted-model", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 200


def test_profile_create_rejects_testing_stage_llm_for_non_tester(client, _with_password):
    import asyncio

    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash", stage="testing"))

    _signup_login(client, "toan")
    resp = client.post("/v1/profiles", json={
        "name": "p1",
        "llm": {"engine": "openrouter", "model": "qwen3-asr-flash", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 403


def test_profile_create_allows_testing_stage_llm_for_tester(client, _with_password):
    import asyncio

    from app.services.auth.users import user_store as _users

    store = ModelRegistryStore()
    asyncio.run(store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash", stage="testing"))

    _signup_login(client, "toan")
    user = asyncio.run(_users.get_by_username("toan"))
    asyncio.run(_users.set_fields(user.id, can_use_testing=True))

    resp = client.post("/v1/profiles", json={
        "name": "p1",
        "llm": {"engine": "openrouter", "model": "qwen3-asr-flash", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 200


def test_profile_clone_rejects_model_disabled_after_template_was_created(client, _with_password):
    """Clone copies stt/llm straight from the source profile without going
    through the model-registry gate that create/update run -- a user denied
    a model could otherwise still get it onto their own profile by cloning
    an admin template pinned to it (created back when the model was still
    enabled). Cloning must be gated the same as create/update."""
    import asyncio

    # The route's check_model_allowed reads through the module-level
    # model_registry_store singleton, which caches rows in memory (see
    # store.py) -- a second, freshly-instantiated ModelRegistryStore() here
    # would write through to the same DB but leave the singleton's cache
    # stale after the create-profile API call below has already primed it.
    # Use the singleton directly so this test's create/disable is visible to
    # the same instance the clone route reads.
    from app.services.auth.users import user_store as _users
    from app.services.model_registry.store import model_registry_store as store

    asyncio.run(store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash"))

    client.post("/api/auth/signup", json={"username": "root", "password": "pw"})
    admin = asyncio.run(_users.get_by_username("root"))
    asyncio.run(_users.set_fields(admin.id, role="admin"))
    client.post("/api/auth/login", json={"username": "root", "password": "pw"})

    resp = client.post("/v1/profiles", json={
        "name": "template-a",
        "llm": {"engine": "openrouter", "model": "qwen3-asr-flash", "base_url": "https://x", "api_key": ""},
    })
    assert resp.status_code == 200

    entry = asyncio.run(store.find("llm", "openrouter", "qwen3-asr-flash"))
    asyncio.run(store.set_fields(entry["id"], enabled=False))

    _signup_login(client, "toan")
    resp = client.post("/v1/profiles/template-a/clone", json={"new_name": "toan-copy"})
    # Same error create/update would give for this now-disabled model.
    assert resp.status_code == 403


def test_profile_create_accepts_enabled_stt_model_for_engine_without_variant_catalog(client, _with_password):
    """qwen3_asr_or (OpenRouter-backed remote STT) has no entry in
    STT_MODEL_CATALOGS -- it's not a local engine with selectable sizes/repos
    like whisper/qwen3_asr. Since the Model Registry options endpoint now
    lists remote-engine models too (an admin can register one for its
    api_key), saving a profile that selects one must succeed via the
    registry gate alone, not be blocked by the local-variant catalog check
    that predates the registry-driven dropdown."""
    import asyncio

    store = ModelRegistryStore()
    asyncio.run(store.create(
        "stt", "qwen3_asr_or", "qwen/qwen3-asr-flash-2026-02-10", "Qwen3 ASR Flash (OpenRouter)"
    ))

    _signup_login(client, "toan")
    resp = client.post("/v1/profiles", json={
        "name": "p1",
        "stt": {"engine": "qwen3_asr_or", "model": "qwen/qwen3-asr-flash-2026-02-10", "language": ""},
    })
    assert resp.status_code == 200
