"""Regression tests for the "session attributed to the wrong person" bug.

conversation_stream (and livehost_stream) already resolve the WS caller's
identity via resolve_ws_identity(), but historically threw it away when
recording the session row, using the *profile's owner* (or nobody, when
there's no profile) instead of the person actually speaking. This meant
GET /v1/sessions (which scopes by the caller's own user id) could never find
sessions the caller had just created themselves -- the History screen shipped
as a permanently empty screen.

The fix: prefer the authenticated speaker's id, falling back to the existing
profile-owner behavior only when there is no identity (auth disabled / dev
mode, or the legacy shared device_auth_token).
"""

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.auth.tokens import issue_access_token
from app.services.auth.users import user_store
from app.services.history.store import session_store
from app.services.profiles.models import Profile
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-attrib-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="ok", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-attrib-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _clean_profile_store(tmp_path, monkeypatch):
    """profile_store is a module-level singleton whose in-memory cache, once
    warmed by an earlier test in the same process, silently skips table
    creation against this test's fresh tmp DB (see app/services/db/config_
    store.py's `_ensure`). Same fixture pattern as test_profiles_routes.py:
    swap in a fresh instance, patched into every module that imported the
    name by reference."""
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.services.profiles.store.profile_store", fresh)
    monkeypatch.setattr("app.services.conversation.session.profile_store", fresh)
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh)
    monkeypatch.setattr("app.api.routes.livehost.profile_store", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _register_stub_engines():
    stt_service.providers["stub-attrib-stt"] = _StubSTT()
    tts_service.providers["stub-attrib-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-attrib-stt", None)
    tts_service.providers.pop("stub-attrib-tts", None)


_ENGINE_QS = "stt_engine=stub-attrib-stt&tts_engine=stub-attrib-tts"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


@pytest.fixture
async def user_a():
    return await user_store.create("attrib-user-a", "pw12345678", role="user")


@pytest.fixture
async def user_b():
    return await user_store.create("attrib-user-b", "pw12345678", role="user")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_ws_conversation_session_owned_by_authenticated_speaker(client, _with_password, user_a):
    """The property that matters most: a WS conversation opened by an
    authenticated user creates a session row owned by *that user*, and
    GET /v1/sessions with that user's bearer then returns it."""
    token = issue_access_token(user_a["id"])
    with client.websocket_connect(
        f"/v1/conversation/stream?{_ENGINE_QS}", subprotocols=["bearer", token]
    ) as ws:
        first = ws.receive_json()
        assert first["event"] == "session_started"
        session_id = first["session_id"]

    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    ids = [row["id"] for row in body["data"]]
    assert session_id in ids, (
        f"session {session_id} created by user {user_a['id']} was not returned by "
        f"GET /v1/sessions for that same user; got ids={ids}"
    )


def test_ws_livehost_session_owned_by_authenticated_speaker(client, _with_password, user_a):
    """Same bug, same fix, at the second call site (livehost.py)."""
    token = issue_access_token(user_a["id"])
    with client.websocket_connect(
        f"/v1/livehost/stream?{_ENGINE_QS}", subprotocols=["bearer", token]
    ) as ws:
        first = ws.receive_json()
        assert first["event"] == "session_started"
        session_id = first["session_id"]

    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    ids = [row["id"] for row in body["data"]]
    assert session_id in ids


async def test_ws_conversation_no_identity_falls_back_to_profile_owner(client, _clean_profile_store):
    """Dev mode (auth disabled): identity.user_id is legitimately None. The
    fallback to profile.owner_id must be preserved -- this pins today's
    behavior for that path so the fix doesn't silently change it."""
    _clean_profile_store.upsert(Profile(name="attrib-fallback-profile", owner_id="owner-abc-123"))

    with client.websocket_connect(
        f"/v1/conversation/stream?profile=attrib-fallback-profile&{_ENGINE_QS}"
    ) as ws:
        first = ws.receive_json()
        assert first["event"] == "session_started"
        session_id = first["session_id"]

    row = await session_store.get(session_id)
    assert row is not None
    assert row["user_id"] == "owner-abc-123"


async def test_ws_conversation_no_identity_no_profile_stays_unowned(client):
    """Dev mode, no profile at all: user_id stays None, exactly as before --
    no regression for the fully-anonymous path."""
    with client.websocket_connect(f"/v1/conversation/stream?{_ENGINE_QS}") as ws:
        first = ws.receive_json()
        assert first["event"] == "session_started"
        session_id = first["session_id"]

    row = await session_store.get(session_id)
    assert row is not None
    assert row["user_id"] is None


def test_ws_conversation_user_only_sees_own_sessions(client, _with_password, user_a, user_b):
    """No regression on existing scoping: a user must not see another user's
    sessions in GET /v1/sessions."""
    token_a = issue_access_token(user_a["id"])
    token_b = issue_access_token(user_b["id"])

    with client.websocket_connect(
        f"/v1/conversation/stream?{_ENGINE_QS}", subprotocols=["bearer", token_a]
    ) as ws:
        session_a = ws.receive_json()["session_id"]

    with client.websocket_connect(
        f"/v1/conversation/stream?{_ENGINE_QS}", subprotocols=["bearer", token_b]
    ) as ws:
        session_b = ws.receive_json()["session_id"]

    resp_a = client.get("/v1/sessions", headers=_auth(token_a))
    ids_a = [row["id"] for row in resp_a.json()["data"]]
    assert session_a in ids_a
    assert session_b not in ids_a
