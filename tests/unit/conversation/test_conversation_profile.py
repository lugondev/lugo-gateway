from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.mcp.models import McpServer
from app.services.mcp.server_store import McpServerStore
from app.services.profiles.models import LlmConfig, Profile
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    # Named distinctly from conftest.py's `_hermetic` so both autouse fixtures
    # run (a same-named fixture here would shadow, not compose with, the
    # global one). conversation_llm_base_url and omnivoice_use_server live on
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


@pytest.mark.asyncio
async def test_chat_uses_model_registry_key_when_profile_engine_and_model_match(client, monkeypatch, tmp_path):
    """A profile that only names an engine+model (no inline base_url/api_key)
    should pick up its credentials from a matching Model Registry (kind="llm")
    entry -- this is what lets an admin set the key once per model instead of
    duplicating it into every profile."""
    from app.services.model_registry.store import model_registry_store
    from app.services.profiles.store import ProfileStore

    await model_registry_store.create(
        "llm", "openrouter", "openrouter/some-model", "Some Model",
        base_url="https://openrouter.ai/api/v1", api_key="sk-or-registry-key",
    )

    fresh = ProfileStore(str(tmp_path / "p4.json"))
    fresh.upsert(Profile(
        name="registry-llm",
        llm=LlmConfig(engine="openrouter", model="openrouter/some-model"),
        system_prompt="Always say howdy.",
    ))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh)

    captured = {}
    original_init = __import__(
        "app.services.conversation.responder", fromlist=["OpenAICompatResponder"]
    ).OpenAICompatResponder.__init__

    def _patched_init(self, base_url, api_key, model, system_prompt, timeout):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["model"] = model
        original_init(self, base_url, api_key, model, system_prompt, timeout)

    with patch(
        "app.services.conversation.responder.OpenAICompatResponder.__init__",
        _patched_init,
    ):
        client.post(
            "/v1/conversation/chat?profile=registry-llm",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["api_key"] == "sk-or-registry-key"
    assert captured["model"] == "openrouter/some-model"


def test_disabled_mcp_server_tools_not_fetched(client, monkeypatch, _local_hermetic):
    _, servers = _local_hermetic
    servers.upsert(McpServer(name="off", url="http://off.test", enabled=False))
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        ws.receive_json()  # session_started
    mock_pool.get_tools.assert_not_called()


def test_enabled_mcp_server_tools_are_fetched(client, monkeypatch, _local_hermetic):
    _, servers = _local_hermetic
    servers.upsert(McpServer(name="on", url="http://on.test", enabled=True))
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        ws.receive_json()  # session_started
    mock_pool.get_tools.assert_called_once_with("http://on.test", headers={})


def test_chat_endpoint_fetches_enabled_mcp_tools(client, monkeypatch, _local_hermetic):
    _, servers = _local_hermetic
    servers.upsert(McpServer(name="on", url="http://on.test", enabled=True))
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    mock_pool.get_tools.assert_called_once_with("http://on.test", headers={})


def test_chat_endpoint_skips_disabled_mcp_tools(client, monkeypatch, _local_hermetic):
    _, servers = _local_hermetic
    servers.upsert(McpServer(name="off", url="http://off.test", enabled=False))
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    mock_pool.get_tools.assert_not_called()


# ---------------------------------------------------------------------------
# Round-2 finding (second half of M4): _build_tool_registry must not inject
# an owner-scoped mcp_server row's tools into every user's turn. Non-admins
# can no longer create such rows (mcp.py's create/update/enabled/clone are
# all _require_admin, see test_mcp_enabled_gate.py), so today's DB only has
# ownerless rows -- but a legacy owner-scoped row left enabled from before
# that authz work must still not be globally broadcast. Only owner_id is
# None rows (server-managed/template) are eligible for the global merge.
# ---------------------------------------------------------------------------


def test_owner_scoped_enabled_mcp_server_is_not_globally_injected(client, monkeypatch, _local_hermetic):
    _, servers = _local_hermetic
    servers.upsert(
        McpServer(name="mallory-owned", owner_id="mallory-id", url="http://mallory.test", enabled=True)
    )
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    # The row is enabled, so if it weren't owner-filtered its tools/headers
    # would be fetched (and injected into this -- unrelated -- caller's turn).
    mock_pool.get_tools.assert_not_called()


def test_ownerless_enabled_mcp_server_is_globally_injected(client, monkeypatch, _local_hermetic):
    """Sibling of the negative case above: an owner_id is None (server-
    managed/template) row is still injected -- the fix must filter on
    owner_id, not disable the global merge entirely."""
    _, servers = _local_hermetic
    servers.upsert(McpServer(name="template", owner_id=None, url="http://template.test", enabled=True))
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=[{"name": "t", "description": "d"}])
    monkeypatch.setattr("app.services.conversation.session.mcp_pool", mock_pool)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    mock_pool.get_tools.assert_called_once_with("http://template.test", headers={})
