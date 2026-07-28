"""Session-ownership IDOR on apps/api_gateway/app/api/routes/lugo.py's WS
/v1/lugo/stream: `wakeup`'s `session_id` flowed straight into cfg.resume_sid
with NO ownership check at all -- the identical hole conversation.py's WS
/stream had before c2ca363, just left open on the device path. Fixed by
sharing conversation.py's ownership gate (now `ws_session_owner_denied` in
app/core/auth_guard.py) with lugo.py."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.users import user_store
from app.services.history.store import session_store
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult


class _StubSTT(STTProvider):
    name = "stub-lugo-authz-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-lugo-authz-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(engine=self.name, sample_rate=24000, audio_url="", duration_seconds=0.0, text=payload.text)


@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    """Same shape as test_lugo_stream.py's `_local_hermetic`: stub STT/TTS so
    the "allowed" resume cases below (own session / admin / device-owner) can
    get past lugo.py's engine-health gate and reach `welcome` without a real
    STT/TTS backend. Named distinctly from conftest.py's autouse `_hermetic`
    so both compose (a same-named fixture would shadow, not add to, it)."""
    _real_get = system_config_store.get

    def _get_with_stub_engines():
        cfg = _real_get()
        return cfg.model_copy(update={
            "engines": cfg.engines.model_copy(update={
                "default_stt_engine": "stub-lugo-authz-stt",
                "default_tts_engine": "stub-lugo-authz-tts",
            })
        })

    monkeypatch.setattr(system_config_store, "get", _get_with_stub_engines)
    stt_service.providers["stub-lugo-authz-stt"] = _StubSTT()
    tts_service.providers["stub-lugo-authz-tts"] = _StubTTS()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-lugo-authz-stt", None)
    tts_service.providers.pop("stub-lugo-authz-tts", None)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """tests/conftest.py's autouse `_hermetic` fixture blanks the admin
    passwords, making settings.auth_enabled False and short-circuiting
    resolve_ws_identity to an unscoped `unauthenticated=True` identity (see
    auth_guard.py). These tests need a real, ownership-checkable identity, so
    turn auth back on -- same pattern as test_conversation_authz.py."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient, role: str) -> str:
    """Log `client` in with a fresh user of the given role, return the new
    user's id. Same helper as test_conversation_authz.py's."""
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


def _wakeup(ws, session_id: str | None = None) -> None:
    frame = {"type": "wakeup", "profile": "dev", "audio_params": {"format": "opus", "sample_rate": 16000}}
    if session_id:
        frame["session_id"] = session_id
    ws.send_json(frame)


# --- Finding A: wakeup's session_id had NO ownership check at all ----------


def test_lugo_cannot_resume_another_users_session(client, _with_password):
    """Reading (or corrupting) another user's history via wakeup.session_id
    is the same IDOR c2ca363 closed on /v1/conversation/stream, left open
    here. bob must get an explicit error (Lugo's own {"type": "error", ...}
    wire shape, NOT conversation.py's {"event": "error", ...}) and the
    connection must close, never a `welcome` for alice's session."""
    _as_user(client, "user")  # caller is 'bob'
    alice_sid = "alice-lugo-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(alice_sid, user_id="alice-the-victim"))
    asyncio.run(session_store.append_message(alice_sid, 1, "user", "alice's private secret"))

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, alice_sid)
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == f"Session '{alice_sid}' not found"


def test_lugo_can_still_resume_own_session(client, _with_password):
    """The fix must not break legitimate same-user resume."""
    bob_id = _as_user(client, "user")
    bob_sid = "bob-lugo-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(bob_sid, user_id=bob_id))

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, bob_sid)
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] == bob_sid


def test_lugo_admin_can_still_resume_any_session(client, _with_password):
    _as_user(client, "admin")
    victim_sid = "victim-lugo-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(victim_sid, user_id="someone-else"))

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, victim_sid)
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] == victim_sid


def test_lugo_unknown_session_id_still_creates_it(client, _with_password):
    """A caller-chosen session_id that doesn't exist yet isn't an IDOR --
    nothing to read. Must keep working exactly as before the fix."""
    _as_user(client, "user")
    fresh_sid = "brand-new-lugo-session-" + uuid.uuid4().hex[:8]

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, fresh_sid)
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] == fresh_sid
