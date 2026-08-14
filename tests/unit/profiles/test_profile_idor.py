"""C2 (Critical) -- profile IDOR via ?profile= / ?tts_profile= at every
CONSUMER site (not just the CRUD routes, which already enforced owner_id
visibility). See docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md.

Before the fix, `conversation.py`'s HTTP /chat and WS /stream, `lugo.py`'s
wakeup, and the ?tts_profile= resolution all resolved a client-supplied name
by bare `profile_store.get(name)` with no ownership check -- any signed-up
user could name another user's private profile and run on that victim's
llm.api_key / system_prompt / mcp_servers / tts voice, and the WS
session_started event even echoed the victim's private engine/tool choices
straight back to the attacker.

Every test below proves TWO things at once, per the brief's "no new
enumeration oracle" constraint:
  1. The attacker never gets the victim's private config (no leak).
  2. The response for "victim's real but invisible profile" is
     indistinguishable from "a profile name that was never created" (no new
     oracle on top of the pre-existing ones -- global 409-on-create and the
     not-found warning itself, both out of scope here).
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.users import user_store
from app.services.mcp import pool as mcp_pool_module
from app.services.profiles.models import LlmConfig, Profile, SttConfig
from app.services.profiles.store import profile_store
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import tts_profile_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """tests/conftest.py's autouse `_hermetic` fixture blanks the admin
    passwords, which makes settings.auth_enabled False and short-circuits
    both AuthGuardMiddleware and resolve_ws_identity's cookie-session lookup
    (see auth_guard.py). These tests need two REAL, distinct user identities
    to prove cross-user isolation, so turn auth back on -- same pattern as
    test_conversation_authz.py / test_lugo_authz.py."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient, role: str = "user") -> str:
    """Give `client` a logged-in session with the given role and return the
    new user's id. Same helper as test_conversation_authz.py's."""
    username = f"{role}-{uuid.uuid4().hex[:10]}"
    password = "s3cret-password"
    signup = client.post("/api/auth/signup", json={"username": username, "password": password})
    assert signup.status_code == 200, signup.text
    if role == "admin":
        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return asyncio.run(user_store.get_by_username(username)).id


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# --- HTTP /chat --------------------------------------------------------


def test_chat_private_profile_indistinguishable_from_nonexistent(client, _with_password):
    """Bob names alice's real, private profile; the response must be
    byte-identical (modulo the caller-known name and the random session_id)
    to naming a profile that was never created, and must never contain
    alice's system_prompt or api_key."""
    alice_id = _rand("alice")
    victim_name = _rand("alice-private-profile")
    profile_store.upsert(Profile(
        name=victim_name, owner_id=alice_id,
        system_prompt="ALICE PRIVATE SYSTEM PROMPT -- DO NOT LEAK",
        llm=LlmConfig(api_key="alice-super-secret-api-key"),
    ))

    _as_user(client, "user")  # caller is bob

    resp_victim = client.post(
        f"/v1/conversation/chat?profile={victim_name}",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    nonexistent_name = _rand("truly-nonexistent-profile")
    resp_ghost = client.post(
        f"/v1/conversation/chat?profile={nonexistent_name}",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp_victim.status_code == resp_ghost.status_code == 200

    # No leak, regardless of the byte-identity check below.
    assert "ALICE PRIVATE SYSTEM PROMPT" not in resp_victim.text
    assert "alice-super-secret-api-key" not in resp_victim.text

    data_v = resp_victim.json()["data"]
    data_g = resp_ghost.json()["data"]
    # session_id is a fresh random uuid per call; profile is the caller-known
    # queried name (legitimately different between the two calls) -- both
    # normalized out before the identity check.
    data_v.pop("session_id")
    data_g.pop("session_id")
    data_v["profile"] = data_g["profile"] = "<NAME>"
    assert data_v == data_g


def test_chat_private_profile_never_reaches_responder_construction(client, _with_password, monkeypatch):
    """Stronger version of the above: spy on build_responder_ex (same
    technique as test_conversation_history.py's
    test_chat_injects_memories_into_prompt) to directly observe what
    system_prompt/api_key/base_url the route resolved -- rather than relying
    on the echo responder happening not to surface them in its reply text.
    The spy REPLACES build_responder_ex (never calls through), so this never
    risks a real network call even though alice's profile pins a base_url."""
    import app.api.routes.conversation as conversation_route

    alice_id = _rand("alice")
    victim_name = _rand("alice-private-profile")
    profile_store.upsert(Profile(
        name=victim_name, owner_id=alice_id,
        system_prompt="ALICE PRIVATE SYSTEM PROMPT -- DO NOT LEAK",
        llm=LlmConfig(base_url="https://alice-llm.example/v1", api_key="alice-super-secret-api-key", model="alice-model"),
    ))

    captured: list[dict] = []

    class _StubResponder:
        name = "stub"
        model = ""
        last_usage = None

        async def reply(self, history):
            return "ok"

        async def aclose(self):
            return None

    async def _spy_build_responder_ex(**kwargs):
        captured.append(kwargs)
        return _StubResponder()

    monkeypatch.setattr(conversation_route, "build_responder_ex", _spy_build_responder_ex)

    _as_user(client, "user")  # caller is bob

    resp = client.post(
        f"/v1/conversation/chat?profile={victim_name}",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["base_url"] is None
    assert kwargs["api_key"] is None
    assert kwargs["model"] is None
    assert kwargs["system_prompt"] is None

    # Same call, but naming a name that was never created -- must produce
    # THE SAME kwargs (no new oracle).
    nonexistent_name = _rand("truly-nonexistent-profile")
    resp2 = client.post(
        f"/v1/conversation/chat?profile={nonexistent_name}",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp2.status_code == 200, resp2.text
    assert captured[1] == kwargs


def test_chat_owner_can_still_use_own_profile(client, _with_password):
    bob_id = _as_user(client, "user")
    own_name = _rand("bobs-own-profile")
    profile_store.upsert(Profile(name=own_name, owner_id=bob_id))

    resp = client.post(
        f"/v1/conversation/chat?profile={own_name}",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["profile"] == own_name


# --- Conversation WS /stream (?profile=) --------------------------------


def test_ws_private_profile_falls_back_like_nonexistent(client, _with_password):
    """A private profile pins a DIFFERENT engine than the server default so
    the leak (or its absence) is directly observable in session_started
    without needing a real LLM/network call. `whisper`/`vieneu` are real
    registered provider names (get_provider() must not raise) -- their
    actual availability doesn't matter since conftest's autouse
    `_hermetic_engine_health` stubs check_resolved_engines() to always pass."""
    alice_id = _rand("alice")
    victim_name = _rand("alice-private-profile")
    profile_store.upsert(Profile(
        name=victim_name, owner_id=alice_id,
        stt=SttConfig(engine="whisper"),
    ))

    _as_user(client, "user")  # caller is bob

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&profile={victim_name}"
    ) as ws:
        warning = ws.receive_json()
        started = ws.receive_json()

    assert warning["event"] == "warning"
    assert started["event"] == "session_started"
    # Must NOT resolve to alice's pinned engine.
    assert started["stt_engine"] != "whisper"
    assert started["active_tools"] == []

    nonexistent_name = _rand("truly-nonexistent-profile")
    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&profile={nonexistent_name}"
    ) as ws:
        warning_g = ws.receive_json()
        started_g = ws.receive_json()

    assert warning_g["event"] == "warning"
    assert warning["message"].replace(victim_name, "<NAME>") == warning_g["message"].replace(
        nonexistent_name, "<NAME>"
    )
    assert started_g["event"] == "session_started"
    assert started["stt_engine"] == started_g["stt_engine"]
    assert started["active_tools"] == started_g["active_tools"] == []


def test_ws_private_profile_tools_never_leak(client, _with_password, monkeypatch):
    """The specific leak vector called out in the audit: session_started's
    active_tools echoing the victim's private MCP tool names directly."""
    from app.services.mcp.models import McpServer

    alice_id = _rand("alice")
    victim_name = _rand("alice-private-profile")
    victim_url = "https://alice-private-mcp.example/mcp"
    profile_store.upsert(Profile(
        name=victim_name, owner_id=alice_id,
        mcp_servers=[McpServer(name="alice-secret-server", url=victim_url)],
    ))

    async def _fake_get_tools(self, url, headers=None):
        if url == victim_url:
            return [{"name": "alice-private-tool", "description": "", "inputSchema": {}}]
        return []

    monkeypatch.setattr(mcp_pool_module.McpConnectionPool, "get_tools", _fake_get_tools)

    _as_user(client, "user")  # caller is bob

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&profile={victim_name}"
    ) as ws:
        ws.receive_json()  # warning
        started = ws.receive_json()

    assert started["event"] == "session_started"
    assert "alice-private-tool" not in started["active_tools"]


def test_ws_owner_can_still_use_own_profile(client, _with_password):
    bob_id = _as_user(client, "user")
    own_name = _rand("bobs-own-profile")
    profile_store.upsert(Profile(name=own_name, owner_id=bob_id, stt=SttConfig(engine="whisper")))

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&profile={own_name}"
    ) as ws:
        started = ws.receive_json()

    assert started["event"] == "session_started"
    assert started["stt_engine"] == "whisper"


# --- ?tts_profile= -------------------------------------------------------


def test_ws_private_tts_profile_falls_back_like_nonexistent(client, _with_password):
    alice_id = _rand("alice")
    victim_tts_name = _rand("alice-private-tts")
    tts_profile_store.upsert(TtsProfile(
        name=victim_tts_name, owner_id=alice_id, engine="vieneu", voice="alice-secret-voice",
    ))

    _as_user(client, "user")  # caller is bob

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&tts_profile={victim_tts_name}"
    ) as ws:
        started = ws.receive_json()

    assert started["event"] == "session_started"
    assert started["tts_engine"] != "vieneu"

    nonexistent_name = _rand("truly-nonexistent-tts-profile")
    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&tts_profile={nonexistent_name}"
    ) as ws:
        started_g = ws.receive_json()

    assert started_g["event"] == "session_started"
    assert started["tts_engine"] == started_g["tts_engine"]


def test_ws_template_tts_profile_visible_to_everyone(client, _with_password):
    template_tts_name = _rand("shared-template-tts")
    tts_profile_store.upsert(TtsProfile(name=template_tts_name, owner_id=None, engine="vieneu"))

    _as_user(client, "user")
    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&tts_profile={template_tts_name}"
    ) as ws:
        started = ws.receive_json()

    assert started["event"] == "session_started"
    assert started["tts_engine"] == "vieneu"


def test_ws_owner_can_still_use_own_tts_profile(client, _with_password):
    bob_id = _as_user(client, "user")
    own_tts_name = _rand("bobs-own-tts")
    tts_profile_store.upsert(TtsProfile(name=own_tts_name, owner_id=bob_id, engine="vieneu"))

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&tts_profile={own_tts_name}"
    ) as ws:
        started = ws.receive_json()

    assert started["event"] == "session_started"
    assert started["tts_engine"] == "vieneu"


# --- Lugo /v1/lugo/stream -------------------------------------------------


def _wakeup(ws, profile_name: str) -> None:
    ws.send_json({
        "type": "wakeup", "profile": profile_name,
        "audio_params": {"format": "opus", "sample_rate": 16000},
    })


def test_lugo_private_profile_indistinguishable_from_nonexistent(client, _with_password):
    alice_id = _rand("alice")
    victim_name = _rand("alice-private-profile")
    profile_store.upsert(Profile(
        name=victim_name, owner_id=alice_id,
        system_prompt="ALICE PRIVATE SYSTEM PROMPT -- DO NOT LEAK",
        llm=LlmConfig(api_key="alice-super-secret-api-key"),
    ))

    _as_user(client, "user")  # caller is bob

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, victim_name)
        msg_victim = ws.receive_json()

    nonexistent_name = _rand("truly-nonexistent-profile")
    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, nonexistent_name)
        msg_ghost = ws.receive_json()

    assert msg_victim["type"] == msg_ghost["type"] == "error"
    assert "ALICE PRIVATE SYSTEM PROMPT" not in str(msg_victim)
    assert "alice-super-secret-api-key" not in str(msg_victim)
    assert msg_victim["message"].replace(victim_name, "<NAME>") == msg_ghost["message"].replace(
        nonexistent_name, "<NAME>"
    )


def test_lugo_owner_can_still_use_own_profile(client, _with_password):
    bob_id = _as_user(client, "user")
    own_name = _rand("bobs-own-profile")
    profile_store.upsert(Profile(name=own_name, owner_id=bob_id))

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, own_name)
        msg = ws.receive_json()

    assert msg["type"] == "welcome"
