from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.model_registry.store import model_registry_store


@pytest.fixture
def client():
    return TestClient(app)


async def test_whisper_download_creates_registry_entry(client, monkeypatch):
    # Don't actually fetch weights -- the endpoint queues via BackgroundTasks and
    # calls whisper_manager.validate/download; stub download so the test is fast.
    from app.services.whisper_models import whisper_manager
    monkeypatch.setattr(whisper_manager, "download", lambda size: None)

    resp = client.post("/v1/models/whisper/download", json={"size": "tiny"})
    assert resp.status_code == 200
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry is not None and entry["enabled"] is True


async def test_whisper_delete_disables_registry_entry(client, monkeypatch):
    from app.services.whisper_models import whisper_manager
    monkeypatch.setattr(whisper_manager, "delete", lambda size: None)
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True)

    resp = client.delete("/v1/models/whisper/tiny")
    assert resp.status_code == 200
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry["enabled"] is False


async def test_vosk_download_creates_registry_entry(client, monkeypatch):
    from app.services.models import model_manager
    monkeypatch.setattr(model_manager, "download", lambda name: None)

    resp = client.post("/v1/models/vosk/download", json={"name": "small"})
    assert resp.status_code == 200
    entry = await model_registry_store.find("stt", "vosk", "small")
    assert entry is not None and entry["enabled"] is True


async def test_vosk_delete_disables_registry_entry(client, monkeypatch):
    from app.services.models import model_manager
    monkeypatch.setattr(model_manager, "delete", lambda name: None)
    await model_registry_store.create("stt", "vosk", "small", "Small", enabled=True)

    resp = client.delete("/v1/models/vosk/small")
    assert resp.status_code == 200
    entry = await model_registry_store.find("stt", "vosk", "small")
    assert entry["enabled"] is False


async def test_omnivoice_download_creates_registry_entry(client, monkeypatch):
    from app.services.tts_models import tts_model_manager
    monkeypatch.setattr(tts_model_manager, "download_omnivoice", lambda repo: None)

    resp = client.post("/v1/models/omnivoice/download", json={"id": "org/repo"})
    assert resp.status_code == 200
    entry = await model_registry_store.find("tts", "omnivoice", "org/repo")
    assert entry is not None and entry["enabled"] is True


async def test_omnivoice_delete_disables_registry_entry(client, monkeypatch):
    from app.services.tts_models import tts_model_manager
    monkeypatch.setattr(tts_model_manager, "delete_omnivoice", lambda repo: None)
    await model_registry_store.create("tts", "omnivoice", "org/repo", "Repo", enabled=True)

    resp = client.post("/v1/models/omnivoice/delete", json={"id": "org/repo"})
    assert resp.status_code == 200
    entry = await model_registry_store.find("tts", "omnivoice", "org/repo")
    assert entry["enabled"] is False


async def test_vieneu_download_creates_registry_entry(client, monkeypatch):
    from app.services.tts_models import tts_model_manager
    monkeypatch.setattr(tts_model_manager, "download_vieneu", lambda mode: None)

    resp = client.post("/v1/models/vieneu/download", json={"mode": "v3turbo"})
    assert resp.status_code == 200
    entry = await model_registry_store.find("tts", "vieneu", "v3turbo")
    assert entry is not None and entry["enabled"] is True


async def test_vieneu_delete_disables_registry_entry(client, monkeypatch):
    from app.services.tts_models import tts_model_manager
    monkeypatch.setattr(tts_model_manager, "delete_vieneu", lambda mode: None)
    await model_registry_store.create("tts", "vieneu", "v3turbo", "v3 Turbo", enabled=True)

    resp = client.post("/v1/models/vieneu/delete", json={"mode": "v3turbo"})
    assert resp.status_code == 200
    entry = await model_registry_store.find("tts", "vieneu", "v3turbo")
    assert entry["enabled"] is False


async def test_llm_download_creates_registry_entry(client, monkeypatch):
    from app.services.llm_models import llm_manager
    monkeypatch.setattr(llm_manager, "download", lambda model: None)

    resp = client.post("/v1/models/llm/download", json={"model": "llama3"})
    assert resp.status_code == 200
    entry = await model_registry_store.find("llm", "ollama", "llama3")
    assert entry is not None and entry["enabled"] is True


async def test_llm_delete_disables_registry_entry(client, monkeypatch):
    from app.services.llm_models import llm_manager
    monkeypatch.setattr(llm_manager, "delete", AsyncMock(return_value=None))
    await model_registry_store.create("llm", "ollama", "llama3", "llama3", enabled=True)

    resp = client.post("/v1/models/llm/delete", json={"model": "llama3"})
    assert resp.status_code == 200
    entry = await model_registry_store.find("llm", "ollama", "llama3")
    assert entry["enabled"] is False
