import json
import time

import anyio
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store


class _StubSTT(STTProvider):
    name = "stub-lugo-cutoff-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    # conversation_stt_engine/conversation_goodbye_text now live on
    # system_config_store's `conversation` group (Task 3), not Settings. Patch
    # the shared singleton's .get() (not .set()) so this never writes through
    # to the shared config_system DB row (see conftest.py's _hermetic for why).
    _real_get = system_config_store.get

    def _get_with_stub_conversation():
        cfg = _real_get()
        return cfg.model_copy(update={
            "conversation": cfg.conversation.model_copy(update={
                "conversation_stt_engine": "stub-lugo-cutoff-stt",
                "conversation_goodbye_text": "",
            })
        })

    monkeypatch.setattr(system_config_store, "get", _get_with_stub_conversation)
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    stt_service.providers["stub-lugo-cutoff-stt"] = _StubSTT()
    # idle_timeout_s huge so only the identity re-check can fire in this test.
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="fast", session=SessionConfig(idle_timeout_s=3600)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    monkeypatch.setattr("app.api.routes.lugo._IDLE_TICK_S", 0.05, raising=False)
    monkeypatch.setattr("app.api.routes.lugo._IDENTITY_RECHECK_INTERVAL_S", 0.05, raising=False)
    yield
    stt_service.providers.pop("stub-lugo-cutoff-stt", None)
    monkeypatch.setattr(settings, "admin_password", "")


def _poll_no_message(ws, duration_s: float, poll_interval_s: float = 0.01) -> None:
    """Assert no websocket message arrives within `duration_s`.

    starlette's TestClient websocket has no built-in receive-with-timeout, so
    poll the underlying anyio memory stream non-blockingly (receive_nowait)
    instead of calling the blocking `ws.receive()`, which would hang forever
    if nothing ever arrives.
    """
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        try:
            message = ws._send_rx.receive_nowait()
        except anyio.WouldBlock:
            time.sleep(poll_interval_s)
            continue
        ws._raise_on_close(message)
        data = json.loads(message["text"]) if message.get("text") else {}
        if data.get("type") == "engines_ready":
            continue  # informational warm-state notice, not a goodbye
        raise AssertionError(f"unexpected message arrived: {data}")


def _receive_until(ws, msg_type: str, attempts: int = 20) -> dict:
    """Drain messages until `msg_type` arrives -- the protocol may interleave
    informational messages (e.g. `engines_ready`) at any point, and this
    test's failure path used to wedge the suite at portal teardown."""
    for _ in range(attempts):
        msg = ws.receive_json()
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"no '{msg_type}' message within {attempts} messages")


def test_disabled_owner_cuts_off_paired_device():
    import asyncio

    user = asyncio.run(user_store.create("toan", "pw"))
    device, raw_token = asyncio.run(device_store.create(user["id"], "ESP32", "AA:BB:CC"))

    client = TestClient(app)
    with client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        ws.send_json({"type": "wakeup", "profile": "fast",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        asyncio.run(user_store.set_fields(user["id"], disabled=True))
        msg = _receive_until(ws, "goodbye")
        assert msg["reason"] == "account_disabled"


def test_idle_timeout_zero_never_fires_for_identity_owned_connection(monkeypatch, tmp_path):
    """Regression test: scheduling the watchdog for identity_owned connections
    (even when idle_timeout_s <= 0) must NOT resurrect idle-timeout goodbyes.

    idle_timeout_s=0 is a profile setting meaning "never idle-disconnect". The
    watchdog is now also scheduled for identity-owned (paired device) sessions
    so it can periodically recheck the owner's account status. Without a
    separate `idle > 0` guard on the idle-check branch, the watchdog would
    fire a spurious idle_timeout goodbye almost immediately (monotonic time
    only increases, so `now - last_activity >= 0` is true on the very first
    tick) -- defeating the "never" setting for exactly the identity-owned
    population this feature targets.
    """
    import asyncio

    from app.api.routes import lugo as lugo_module

    # Use a dedicated, single-write ProfileStore for this test rather than a
    # second upsert on the module-level store the autouse fixture already
    # warmed: SqliteBackedStore caches after its first _ensure(), so a second
    # write on an already-warm store silently skips re-initializing the
    # (possibly-since-reconfigured) backing table. A fresh store's first
    # write always runs _ensure() against whatever engine is currently live.
    fresh = ProfileStore(str(tmp_path / "profiles_never_idle.json"))
    fresh.upsert(Profile(name="fast", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr(lugo_module, "profile_store", fresh)

    user = asyncio.run(user_store.create("toan2", "pw"))
    device, raw_token = asyncio.run(device_store.create(user["id"], "ESP32", "AA:BB:DD"))

    client = TestClient(app)
    with client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        ws.send_json({"type": "wakeup", "profile": "fast",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"

        # Wait across several _IDLE_TICK_S ticks (0.05s, set by the hermetic
        # fixture) without disabling the user. No goodbye should arrive: with
        # idle_timeout_s=0 the connection must survive indefinitely, even
        # though the watchdog task is now running (for identity-recheck
        # purposes).
        _poll_no_message(ws, duration_s=0.05 * 8)

        # Now disable the user: the identity-based goodbye should still
        # arrive, proving the watchdog was running the whole time (for
        # identity reasons) while the idle-timeout branch stayed dormant.
        asyncio.run(user_store.set_fields(user["id"], disabled=True))
        msg = _receive_until(ws, "goodbye")
        assert msg["reason"] == "account_disabled"
