"""`knowledge.api_key` is the first credential to live in SystemConfig.

GET /v1/system/config returned `system_config_store.get().model_dump()`
verbatim, so it handed the bearer key back in plaintext. Every other credential
surface in this gateway masks (profiles' llm.api_key, model registry, providers,
mcp headers). Admin-only is not the same as safe: admin responses land in logs,
screenshots and bug reports.

The write half matters just as much. This repo already shipped the bug once, on
plugins: read the config, write it back, and "***" got stored as the real key.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.system_config import SystemConfigStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.api.routes.system.system_config_store", fresh)
    return fresh


@pytest.fixture
def client():
    return TestClient(app)


def _store():
    from app.api.routes.system import system_config_store

    return system_config_store


def test_the_knowledge_api_key_is_masked_on_read(client):
    client.put("/v1/system/config", json={"knowledge": {"api_key": "kb-real-secret"}})
    data = client.get("/v1/system/config").json()["data"]
    assert data["knowledge"]["api_key"] == "***"
    assert "kb-real-secret" not in str(data)


def test_the_put_response_is_masked_too(client):
    resp = client.put("/v1/system/config", json={"knowledge": {"api_key": "kb-real-secret"}})
    assert resp.json()["data"]["knowledge"]["api_key"] == "***"


def test_echoing_the_mask_back_leaves_the_real_key_intact(client):
    """Read-modify-write is what the admin UI's group Save button does."""
    client.put("/v1/system/config", json={"knowledge": {"api_key": "kb-real-secret"}})
    body = client.get("/v1/system/config").json()["data"]
    assert body["knowledge"]["api_key"] == "***"
    body["knowledge"]["base_url"] = "http://kb.internal:8090"

    resp = client.put("/v1/system/config", json=body)
    assert resp.status_code == 200, resp.text
    assert _store().get().knowledge.api_key == "kb-real-secret"
    assert _store().get().knowledge.base_url == "http://kb.internal:8090"


def test_a_new_key_still_replaces_the_old_one(client):
    client.put("/v1/system/config", json={"knowledge": {"api_key": "kb-real-secret"}})
    client.put("/v1/system/config", json={"knowledge": {"api_key": "kb-rotated"}})
    assert _store().get().knowledge.api_key == "kb-rotated"


def test_a_blank_key_still_clears_it(client):
    """Preserve-on-mask must not make the credential unclearable."""
    client.put("/v1/system/config", json={"knowledge": {"api_key": "kb-real-secret"}})
    client.put("/v1/system/config", json={"knowledge": {"api_key": ""}})
    assert _store().get().knowledge.api_key == ""


def test_an_unset_key_reads_as_blank_not_as_a_mask(client):
    data = client.get("/v1/system/config").json()["data"]
    assert data["knowledge"]["api_key"] == ""
