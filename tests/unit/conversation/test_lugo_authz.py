"""Session-ownership IDOR on apps/api_gateway/app/api/routes/lugo.py's WS
/v1/lugo/stream, and the paired-device admin-bypass hole in the shared
ws_session_owner_denied() helper (app/core/auth_guard.py) both
/v1/conversation/stream and /v1/lugo/stream go through.

1. `wakeup`'s `session_id` flowed straight into cfg.resume_sid with NO
   ownership check at all -- the identical hole conversation.py's WS /stream
   had before c2ca363, just left open on the device path. Fixed by sharing
   conversation.py's ownership gate (now `ws_session_owner_denied` in
   app/core/auth_guard.py) with lugo.py (Finding A).
2. A paired-device identity (`WsIdentity.via_device`) still got the DB-role
   admin bypass in that shared check even after bearer identities lost it
   (round-1 review, I2, on the earlier conversation.py fix) -- a device
   paired to an admin account could resume ANY user's session over
   /v1/lugo/stream (and /v1/conversation/stream too, before this) (Finding B).
"""

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
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


def _silence_wav(ms: int = 100, sr: int = 24000) -> bytes:
    n = int(sr * ms / 1000)
    return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=sr)


class _StubSTT(STTProvider):
    name = "stub-lugo-authz-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-lugo-authz-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return _silence_wav(), "audio/wav"


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
    yield fresh
    stt_service.providers.pop("stub-lugo-authz-stt", None)
    tts_service.providers.pop("stub-lugo-authz-tts", None)


def _own_dev_profile(profiles: ProfileStore, owner_id: str) -> None:
    """usable_profile_or_none now requires shared or owner match -- the
    autouse fixture's "dev" profile is ownerless and so no longer usable by
    anyone (Root Cause A, task-3b-brief.md). Re-bind it to whichever user the
    calling test just authenticated as, preserving every other field the
    fixture set (idle_timeout_s=0 in particular -- several tests below depend
    on it).

    `.invalidate()` first: `fresh` (the autouse fixture's ProfileStore) ran
    its first write before tests/conftest.py's `_tmp_db` repointed the sync
    config-store engine at this test's tmp sqlite file, so its in-memory
    cache is warm against a now-superseded engine. A second `.upsert()`
    straight onto that stale cache skips `_ensure()` (see
    app/services/db/config_store.py) and writes through to a table that was
    never created on the *current* engine -- `.invalidate()` drops the cache
    so the write re-runs `_ensure()` (and `init_config_tables()`) against
    whichever engine is live now. Same root cause documented in
    test_lugo_disabled_cutoff.py's `test_idle_timeout_zero_never_fires_...`
    comment, worked around there with a dedicated single-write store instead."""
    profiles.invalidate()
    profiles.upsert(Profile(name="dev", owner_id=owner_id, session=SessionConfig(idle_timeout_s=0)))


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


def test_lugo_cannot_resume_another_users_session(client, _with_password, _local_hermetic):
    """Reading (or corrupting) another user's history via wakeup.session_id
    is the same IDOR c2ca363 closed on /v1/conversation/stream, left open
    here. bob must get an explicit error (Lugo's own {"type": "error", ...}
    wire shape, NOT conversation.py's {"event": "error", ...}) and the
    connection must close, never a `welcome` for alice's session."""
    bob_id = _as_user(client, "user")
    _own_dev_profile(_local_hermetic, bob_id)
    alice_sid = "alice-lugo-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(alice_sid, user_id="alice-the-victim"))
    asyncio.run(session_store.append_message(alice_sid, 1, "user", "alice's private secret"))

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, alice_sid)
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == f"Session '{alice_sid}' not found"


def test_lugo_can_still_resume_own_session(client, _with_password, _local_hermetic):
    """The fix must not break legitimate same-user resume."""
    bob_id = _as_user(client, "user")
    _own_dev_profile(_local_hermetic, bob_id)
    bob_sid = "bob-lugo-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(bob_sid, user_id=bob_id))

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, bob_sid)
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] == bob_sid


def test_lugo_admin_can_still_resume_any_session(client, _with_password, _local_hermetic):
    admin_id = _as_user(client, "admin")
    _own_dev_profile(_local_hermetic, admin_id)
    victim_sid = "victim-lugo-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(victim_sid, user_id="someone-else"))

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, victim_sid)
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] == victim_sid


def test_lugo_unknown_session_id_still_creates_it(client, _with_password, _local_hermetic):
    """A caller-chosen session_id that doesn't exist yet isn't an IDOR --
    nothing to read. Must keep working exactly as before the fix."""
    user_id = _as_user(client, "user")
    _own_dev_profile(_local_hermetic, user_id)
    fresh_sid = "brand-new-lugo-session-" + uuid.uuid4().hex[:8]

    with client.websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws, fresh_sid)
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] == fresh_sid


# --- Finding B: a paired device still got the DB-role admin bypass ---------


def test_lugo_device_paired_to_admin_cannot_resume_other_users_session(client, _with_password, _local_hermetic):
    """A device token carries no role, same as a bearer token -- a device
    acts as its owning user, never as an admin, even when that user's DB
    role is "admin". Confirmed pre-fix: this exact setup (ESP32 paired to an
    admin account) got `welcome` on another real user's session.

    Connects with a FRESH client (no cookies): resolve_ws_identity checks
    the browser cookie session before the device token, so reusing `client`
    (which is logged in as the admin from _as_user) would resolve via that
    cookie session, not via the device token -- silently testing the wrong
    code path."""
    admin_id = _as_user(client, "admin")
    _own_dev_profile(_local_hermetic, admin_id)
    _device, raw_token = asyncio.run(
        device_store.create(admin_id, "kitchen-esp32", "serial-001", profile_id="dev")
    )

    victim_sid = "victim-device-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(victim_sid, user_id="someone-else"))

    device_client = TestClient(app)
    with device_client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        _wakeup(ws, victim_sid)
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == f"Session '{victim_sid}' not found"


def test_lugo_paired_device_can_still_resume_its_owners_session(client, _with_password, _local_hermetic):
    """The comparison is by user_id, so a device must still be able to
    resume ITS OWN owner's session -- the fix must not break that. Fresh
    client for the same cookie-vs-device-token reason as the test above."""
    owner_id = _as_user(client, "user")
    _own_dev_profile(_local_hermetic, owner_id)
    _device, raw_token = asyncio.run(
        device_store.create(owner_id, "living-room-esp32", "serial-002", profile_id="dev")
    )

    owner_sid = "owner-device-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(owner_sid, user_id=owner_id))

    device_client = TestClient(app)
    with device_client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        _wakeup(ws, owner_sid)
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] == owner_sid


def test_a_reconnecting_device_is_told_the_conversation_it_actually_resumed(client, _with_password, _local_hermetic):
    """The `welcome` id must be the conversation the server put this connection
    in, not the fresh one it minted before looking.

    The device persists whatever arrives here (rpi-assistant writes it to disk),
    so reporting the pre-resume id hands it one nothing will ever write to -- and
    the next reconnect asks to resume an empty conversation. Caught live, not by a
    test: the route was sending its own local variable.
    """
    owner_id = _as_user(client, "resume-owner")
    _own_dev_profile(_local_hermetic, owner_id)
    _device, raw_token = asyncio.run(
        device_store.create(owner_id, "speaker", "serial-resume", profile_id="dev")
    )

    first = TestClient(app)
    with first.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        _wakeup(ws)
        first_id = ws.receive_json()["session_id"]
        # Give it something: a conversation exists only once something was said.
        ws.send_json({"type": "text", "text": "chào bạn"})
        for _ in range(40):
            message = ws.receive()
            if message.get("bytes") is not None or "text" not in message:
                continue
            if json.loads(message["text"]).get("type") == "tts":
                if json.loads(message["text"]).get("state") == "stop":
                    break

    second = TestClient(app)
    with second.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        _wakeup(ws)                      # no session id asked for
        resumed_id = ws.receive_json()["session_id"]

    assert resumed_id == first_id, (
        "welcome announced a different conversation than the one this connection "
        "was actually placed in"
    )
