from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.mcp.models import McpServer
from app.services.mcp.server_store import McpServerStore
from app.services.profiles.models import LlmConfig, Profile, TtsConfig
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    # conversation_llm_base_url and omnivoice_use_server now live on
    # system_config_store (Task 3 / Task 7), not Settings; the module-level
    # conftest._hermetic fixture already zeroes/disables them.
    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_servers = McpServerStore(str(tmp_path / "mcp.json"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)
    # _build_tool_registry moved into app.services.conversation.session, so the MCP
    # singletons it reads must be patched there (the WS route delegates to the core).
    monkeypatch.setattr("app.services.conversation.session.mcp_server_store", fresh_servers)
    return fresh_profiles, fresh_servers


@pytest.fixture
def client():
    return TestClient(app)


def test_chat_without_profile_uses_echo(client):
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.json()["data"]["responder"] == "echo"


def test_chat_unknown_profile_falls_back(client):
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


def test_chat_with_voice_optimized_profile_appends_directive(client, monkeypatch, tmp_path):
    from app.services.conversation.responder import VOICE_OPTIMIZATION_DIRECTIVE
    from app.services.profiles.store import ProfileStore
    fresh = ProfileStore(str(tmp_path / "p3.json"))
    fresh.upsert(Profile(
        name="voice",
        llm=LlmConfig(base_url="http://localhost:11434/v1", model="llama3"),
        system_prompt="Always say howdy.",
        voice_optimized=True,
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
            "/v1/conversation/chat?profile=voice",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert any(VOICE_OPTIMIZATION_DIRECTIVE in sp for sp in captured)


def test_disabled_mcp_server_tools_not_fetched(client, monkeypatch, _hermetic):
    _, servers = _hermetic
    servers.upsert(McpServer(name="off", url="http://off.test", enabled=False))
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        ws.receive_json()  # session_started
    mock_pool.get_tools.assert_not_called()


def test_enabled_mcp_server_tools_are_fetched(client, monkeypatch, _hermetic):
    _, servers = _hermetic
    servers.upsert(McpServer(name="on", url="http://on.test", enabled=True))
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        ws.receive_json()  # session_started
    mock_pool.get_tools.assert_called_once_with("http://on.test", headers={})


def test_chat_endpoint_fetches_enabled_mcp_tools(client, monkeypatch, _hermetic):
    _, servers = _hermetic
    servers.upsert(McpServer(name="on", url="http://on.test", enabled=True))
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    mock_pool.get_tools.assert_called_once_with("http://on.test", headers={})


def test_chat_endpoint_skips_disabled_mcp_tools(client, monkeypatch, _hermetic):
    _, servers = _hermetic
    servers.upsert(McpServer(name="off", url="http://off.test", enabled=False))
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    mock_pool.get_tools.assert_not_called()
