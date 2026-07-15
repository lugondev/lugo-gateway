import json
import pytest
from fastapi.testclient import TestClient
from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store


class _StubSTT(STTProvider):
    name = "stub-mcp-stt"
    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        return STTResult(engine=self.name, text="louder please", is_final=True)


class _FakeToolCallResponder:
    """Stand-in for the real LLM responder: always emits exactly one tool call
    to the (sanitized) device tool name, then a final text sentence. Matches
    the ``Responder`` interface consumed by ``ConversationSession`` (see
    ``session.py:reply_stream`` call at the text-turn path): a ``name``
    attribute and an async-generator ``reply_stream(history, registry, ctx,
    max_iters)``. It intentionally has no ``system_prompt`` attribute so
    ``ConversationSession._refresh_memory`` (which checks via ``hasattr``)
    is a no-op, same as the built-in EchoResponder.
    """

    name = "stub-mcp-responder"

    async def reply(self, history: list[dict]) -> str:
        return "ok"

    async def reply_stream(self, history, registry=None, ctx=None, max_iters=3):
        if registry is not None:
            await registry.run("self_audio_set_volume", {"volume": 90}, ctx)
        yield "Volume set to 90."


async def _fake_build_responder_ex(*args, **kwargs):
    return _FakeToolCallResponder()


@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    # Named distinctly from conftest.py's `_hermetic` so both autouse fixtures
    # run (a same-named fixture here would shadow, not compose with, the
    # global one -- see conftest.py's _hermetic for what that one handles).
    # conversation_stt_engine now lives on system_config_store's `conversation`
    # group (Task 3), not Settings. Patch the shared singleton's .get() (not
    # .set()) so this never writes through to the shared config_system DB row.
    _real_get = system_config_store.get

    def _get_with_stub_stt():
        cfg = _real_get()
        return cfg.model_copy(update={
            "conversation": cfg.conversation.model_copy(update={"conversation_stt_engine": "stub-mcp-stt"})
        })

    monkeypatch.setattr(system_config_store, "get", _get_with_stub_stt)
    monkeypatch.setattr(settings, "device_mcp_enabled", True)
    # The real LLM responder can't be exercised hermetically (no live LLM), so
    # replace the responder builder that ConversationSession.start() calls
    # (session.py imports it by name via `from ...responder import
    # build_responder_ex`, so patching it on the session module -- not the
    # defining responder module -- is what actually takes effect at call time)
    # with a stub that deterministically calls the device tool.
    monkeypatch.setattr("app.services.conversation.session.build_responder_ex", _fake_build_responder_ex)
    stt_service.providers["stub-mcp-stt"] = _StubSTT()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-mcp-stt", None)


def _device_answer(payload: dict) -> dict | None:
    """Given a downlink mcp payload, return the uplink result payload."""
    mid = payload["id"]
    method = payload["method"]
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"serverInfo": {"name": "dev"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
            "name": "self.audio.set_volume",
            "description": "Set speaker volume",
            "inputSchema": {"type": "object",
                            "properties": {"volume": {"type": "integer"}},
                            "required": ["volume"]},
            "annotations": {"requiresConfirm": False},
        }]}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": "volume set to 90"}]}}
    return None


def test_device_tools_are_discovered_and_callable():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "features": {"mcp": True},
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"

        # The gateway drives discovery: answer initialize then tools/list.
        seen_methods = []
        got_tools_call = False
        # Answer downlink mcp frames until we've serviced a tools/call.
        for _ in range(20):
            msg = ws.receive_json()
            if msg.get("type") != "mcp":
                continue
            payload = msg["payload"]
            seen_methods.append(payload["method"])
            ans = _device_answer(payload)
            if ans is not None:
                ws.send_json({"type": "mcp", "payload": ans})
            if payload["method"] == "tools/list":
                # Discovery done; now trigger a turn that calls the tool.
                ws.send_json({"type": "text", "text": "louder"})
            if payload["method"] == "tools/call":
                got_tools_call = True
                assert payload["params"]["name"] == "self.audio.set_volume"
                break
        assert "initialize" in seen_methods
        assert "tools/list" in seen_methods
        assert got_tools_call
