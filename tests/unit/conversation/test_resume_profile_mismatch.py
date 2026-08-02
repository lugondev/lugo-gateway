"""Resuming a session that belongs to a different profile must not append to it.

Real incident (2026-08-02): the web client's implicit resume asked for "the most
recent session" without filtering by assistant, so a browser talking under
profile `dev-copy` was handed the session id of an ESP32 conversation under
`esp32-assistant`. The gateway accepted it -- the ownership check passed, both
belonged to the same user -- and appended every turn there. A session keeps the
`profile_id`/`source`/`client_id` it was created with, and History reads per
assistant (`GET /v1/sessions?profile=...`), so the conversation was invisible
under the profile the user had actually selected: they saw an empty history and
their turns were mixed into the speaker's transcript.

The client fix (filter the resume lookup by profile) lives in lugo-web-client;
these tests pin the server half, which is what makes any client's mistake
non-destructive.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.history.store import session_store
from app.services.profiles.models import Profile
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _profiles(tmp_path, monkeypatch):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="speaker"))
    fresh.upsert(Profile(name="browser"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


def _chat(client, profile: str, text: str, session_id: str | None = None):
    params = {"profile": profile}
    if session_id:
        params["session_id"] = session_id
    resp = client.post(
        "/v1/conversation/chat", params=params,
        json={"messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200
    return resp.json()["data"]["session_id"]


def test_chat_does_not_resume_across_profiles(client):
    speaker_sid = _chat(client, "speaker", "nói với loa")
    browser_sid = _chat(client, "browser", "gõ trong trình duyệt", session_id=speaker_sid)

    assert browser_sid != speaker_sid, "browser turn was appended to the speaker's session"
    speaker_msgs = asyncio.run(session_store.get_messages(speaker_sid))
    assert [m["content"] for m in speaker_msgs if m["role"] == "user"] == ["nói với loa"]
    browser_msgs = asyncio.run(session_store.get_messages(browser_sid))
    assert [m["content"] for m in browser_msgs if m["role"] == "user"] == ["gõ trong trình duyệt"]
    assert asyncio.run(session_store.get(browser_sid))["profile_id"] == "browser"


def test_chat_still_resumes_within_the_same_profile(client):
    """The guard must not break normal resume -- that's the whole feature."""
    sid = _chat(client, "speaker", "câu một")
    again = _chat(client, "speaker", "câu hai", session_id=sid)
    assert again == sid
    msgs = asyncio.run(session_store.get_messages(sid))
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["câu một", "câu hai"]


def test_ws_starts_a_new_session_and_warns_on_profile_mismatch(client):
    speaker_sid = "speaker-session-mismatch"
    asyncio.run(session_store.create(speaker_sid, profile_id="speaker", user_id=None))

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&profile=browser&session_id={speaker_sid}"
    ) as ws:
        frames = [ws.receive_json(), ws.receive_json()]

    warnings = [f for f in frames if f["event"] == "warning"]
    started = next(f for f in frames if f["event"] == "session_started")
    assert started["session_id"] != speaker_sid, "WS resumed another profile's session"
    assert warnings, "silently starting a different session is what hid the bug"
    assert "speaker" in warnings[0]["message"]


def test_ws_resume_within_the_same_profile_is_untouched(client):
    sid = "speaker-session-same-profile"
    asyncio.run(session_store.create(sid, profile_id="speaker", user_id=None))

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&profile=speaker&session_id={sid}"
    ) as ws:
        started = ws.receive_json()

    assert started["event"] == "session_started"
    assert started["session_id"] == sid
