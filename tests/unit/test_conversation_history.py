import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.db import engine as db_engine
from app.services.history.store import session_store
from app.services.memory.store import memory_store
from app.services.profiles.store import ProfileStore
from app.services.profiles.models import Profile


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


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
