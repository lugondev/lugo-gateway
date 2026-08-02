import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.history.store import session_store
from app.services.memory.store import memory_store
from app.services.profiles.store import ProfileStore
from app.services.profiles.models import Profile


@pytest.fixture(autouse=True)
def _profiles(tmp_path, monkeypatch):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="pet"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


def test_chat_creates_session_and_persists(client):
    resp = client.post(
        "/v1/conversation/chat",
        params={"profile": "pet"},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200
    sid = resp.json()["data"]["session_id"]
    assert sid
    msgs = asyncio.run(session_store.get_messages(sid))
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hello"


def test_chat_resumes_session_with_stored_context(client):
    r1 = client.post(
        "/v1/conversation/chat",
        params={"profile": "pet"},
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    sid = r1.json()["data"]["session_id"]
    r2 = client.post(
        "/v1/conversation/chat",
        params={"profile": "pet", "session_id": sid},
        json={"messages": [{"role": "user", "content": "second"}]},
    )
    assert r2.json()["data"]["session_id"] == sid
    msgs = asyncio.run(session_store.get_messages(sid))
    contents = [m["content"] for m in msgs if m["role"] == "user"]
    assert contents == ["first", "second"]


def test_chat_injects_memories_into_prompt(client, monkeypatch):
    asyncio.run(memory_store.add("pet", "User's name is Lugon"))
    seen = {}
    from app.services.conversation import responder as responder_mod

    orig = responder_mod.build_responder_ex

    def spy(**kwargs):
        seen.update(kwargs)
        return orig(**kwargs)

    monkeypatch.setattr("app.api.routes.conversation.build_responder_ex", spy)
    client.post(
        "/v1/conversation/chat",
        params={"profile": "pet"},
        json={"messages": [{"role": "user", "content": "who am I?"}]},
    )
    assert "User's name is Lugon" in (seen.get("system_prompt") or "")


def test_ws_persists_and_resumes(client):
    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        sid = started["session_id"]
        ws.send_json({"type": "text", "text": "xin chào"})
        while True:
            evt = ws.receive_json()
            if evt["event"] == "turn_done":
                break
    msgs = asyncio.run(session_store.get_messages(sid))
    roles = [m["role"] for m in msgs]
    assert roles[0] == "user" and "assistant" in roles

    # resume: history seeded from DB
    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&session_id={sid}"
    ) as ws:
        started = ws.receive_json()
        assert started["session_id"] == sid


async def test_two_users_on_one_profile_keep_separate_memory(monkeypatch):
    """Facts extracted for user A must not surface in user B's context on the
    same profile. Drives get_context directly with a shared profile."""
    from app.services.memory.retriever import memory_retriever
    from app.services.memory.store import memory_store
    from app.services.profiles.models import MemoryConfig, Profile

    profile = Profile(name="shared", memory=MemoryConfig(enabled=True, mode="all"))
    await memory_store.add("shared", "A is in Hanoi", user_id="user-a")

    assert "A is in Hanoi" in await memory_retriever.get_context(profile, user_id="user-a")
    assert "A is in Hanoi" not in await memory_retriever.get_context(profile, user_id="user-b")


def test_chat_caps_the_replayed_history(client, monkeypatch):
    """Resuming replayed the ENTIRE stored transcript into the prompt: cost per
    request grew with the length of the conversation and a long enough session
    just overflowed the context window. The WS path already capped this (_tail);
    this route did not.
    """
    from app.services.system_config import system_config_store

    _real_get = system_config_store.get
    monkeypatch.setattr(
        system_config_store, "get",
        lambda: _real_get().model_copy(update={
            "conversation": _real_get().conversation.model_copy(
                update={"conversation_history_max_messages": 4}
            )
        }),
    )

    seen: list[list[dict]] = []

    class _SpyResponder:
        name = "spy"
        last_usage = None

        async def reply(self, history):
            seen.append(list(history))
            return "ok"

        async def aclose(self):
            pass

    async def _build(**_kwargs):
        return _SpyResponder()

    sid = "chat-cap"
    # profile_id must match the ?profile= used below: a session is only resumed
    # under the profile it was created with (see test_resume_profile_mismatch.py
    # for why -- turns filed under another profile vanish from that profile's
    # History). Without it this reads as a cross-profile resume, the route hands
    # back a fresh session, and nothing is replayed at all.
    asyncio.run(session_store.create(sid, profile_id="pet"))
    for i in range(10):
        asyncio.run(session_store.append_message(sid, i, "user", f"m{i}"))

    monkeypatch.setattr("app.api.routes.conversation.build_responder_ex", _build)
    resp = client.post(
        "/v1/conversation/chat",
        params={"profile": "pet", "session_id": sid},
        json={"messages": [{"role": "user", "content": "latest"}]},
    )

    assert resp.status_code == 200
    assert len(seen[0]) == 4
    assert seen[0][-1]["content"] == "latest"
    # Nothing was dropped from the record -- History still has all of it.
    assert len(asyncio.run(session_store.get_messages(sid))) == 12
