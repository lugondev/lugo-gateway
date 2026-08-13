import pytest
from fastapi.testclient import TestClient
from app.core.settings import settings
from app.main import app
from app.services.model_registry.store import model_registry_store
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    # Mirrors tests/unit/profiles/test_profile_model_gate.py: profile_store is a
    # module-level singleton with an in-memory cache that, once populated,
    # ignores the fresh per-test SQLite file the autouse `_tmp_db` fixture
    # points the engine at -- a brand new ProfileStore (cache=None) per test
    # avoids that staleness.
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.profiles.profile_store", fresh)
    monkeypatch.setattr("app.services.profiles.store.profile_store", fresh)


def _client(): return TestClient(app)

def _login(client, name="u"):
    client.post("/api/auth/signup", json={"username": name, "password": "pw"})
    client.post("/api/auth/login", json={"username": name, "password": "pw"})


def test_profile_response_has_resolved_labels(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    import asyncio
    from app.services.db.engine import init_db
    asyncio.run(init_db())
    # a registry entry whose label we expect to see
    asyncio.run(model_registry_store.create("stt", "qwen3_asr_or", "qwen3-asr-flash", "Qwen3 ASR Flash"))
    client = _client()
    _login(client)
    # profile pinning that stt engine/model
    r = client.post("/v1/profiles", json={"name": "p1", "stt": {"engine": "qwen3_asr_or", "model": "qwen3-asr-flash"}})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["stt_label"] == "Qwen3 ASR Flash"       # resolved registry label, not raw engine/model
    assert "llm_label" in d and "tts_label" in d
    # raw still present (editors need it)
    assert d["stt"]["engine"] == "qwen3_asr_or" and d["stt"]["model"] == "qwen3-asr-flash"


def test_unpinned_fields_show_server_default_label(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    client = _client()
    _login(client, "u2")
    r = client.post("/v1/profiles", json={"name": "p2", "stt": {"language": "vi"}})  # no stt engine/model
    d = r.json()["data"]
    # unpinned -> a "(default)" label or the literal "server default", never blank
    assert d["stt_label"] and d["tts_label"] and d["llm_label"]
    assert d["tts_label"]  # profile_name empty -> default engine label or "server default"
