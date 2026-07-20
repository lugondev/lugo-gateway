import pytest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


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
