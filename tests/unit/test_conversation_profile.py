from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.mcp.models import McpServer
from app.services.mcp.server_store import McpServerStore
from app.services.profiles.models import LlmConfig, Profile, TtsConfig
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "omnivoice_use_server", False)

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_servers = McpServerStore(str(tmp_path / "mcp.json"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)
    monkeypatch.setattr("app.api.routes.conversation.mcp_server_store", fresh_servers)
    return fresh_profiles, fresh_servers


@pytest.fixture
def client():
    return TestClient(app)


def test_chat_without_profile_uses_echo(client):
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.json()["data"]["responder"] == "echo"


def test_chat_unknown_profile_falls_back(client, monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    resp = client.post(
        "/v1/conversation/chat?profile=ghost",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["responder"] == "echo"


def test_chat_with_profile_uses_profile_system_prompt(client, monkeypatch, tmp_path):
    from app.services.profiles.store import ProfileStore
    fresh = ProfileStore(str(tmp_path / "p2.json"))
    fresh.upsert(Profile(
        name="greet",
        llm=LlmConfig(base_url="http://localhost:11434/v1", model="llama3"),
        system_prompt="Always say howdy.",
    ))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh)

    captured = []
    original_init = __import__(
        "app.services.conversation.responder", fromlist=["OpenAICompatResponder"]
    ).OpenAICompatResponder.__init__

    def _patched_init(self, base_url, api_key, model, system_prompt, timeout):
        captured.append(system_prompt)
        original_init(self, base_url, api_key, model, system_prompt, timeout)

    with patch(
        "app.services.conversation.responder.OpenAICompatResponder.__init__",
        _patched_init,
    ):
        client.post(
            "/v1/conversation/chat?profile=greet",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert any("howdy" in sp for sp in captured)
