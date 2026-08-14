"""A paired device (services.auth.devices) with no profile_id bound must be
refused at wakeup rather than allowed to run on whatever it requested or on
server defaults -- see docs/superpowers/specs/2026-08-12-device-profile-pairing-admin-ui-design.md.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.models import Profile
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store


class _StubSTT(STTProvider):
    name = "stub-lugo-profile-gate-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    _real_get = system_config_store.get

    def _get_with_stub():
        cfg = _real_get()
        return cfg.model_copy(update={
            "engines": cfg.engines.model_copy(update={"default_stt_engine": "stub-lugo-profile-gate-stt"}),
        })

    monkeypatch.setattr(system_config_store, "get", _get_with_stub)
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    stt_service.providers["stub-lugo-profile-gate-stt"] = _StubSTT()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="bound-profile"))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-lugo-profile-gate-stt", None)
    monkeypatch.setattr(settings, "admin_password", "")


def test_unbound_paired_device_is_refused_at_wakeup():
    user = asyncio.run(user_store.create("gate-user-unbound", "pw"))
    _device, raw_token = asyncio.run(device_store.create(user["id"], "ESP32", "AA:BB:GATE1"))

    client = TestClient(app)
    with client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        ws.send_json({
            "type": "wakeup",
            "audio_params": {"format": "opus", "sample_rate": 16000},
        })
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "profile" in msg["message"]
        # The gate must actually refuse the connection, not just announce the
        # refusal and fall through into full session setup -- see Finding 3 of
        # the 2026-08-12 whole-branch review.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_bound_paired_device_still_connects(monkeypatch, tmp_path):
    user = asyncio.run(user_store.create("gate-user-bound", "pw"))
    _device, raw_token = asyncio.run(
        device_store.create(user["id"], "ESP32", "AA:BB:GATE2", profile_id="bound-profile")
    )
    # profile_visible now requires shared or owner match -- bind the profile
    # to the same user whose device is paired to it, mirroring how a real
    # user's own profile would be bound (not shared -- a shared profile is
    # not runnable at all, see Root Cause A in task-3b-brief.md).
    #
    # Use a dedicated, single-write ProfileStore for this test rather than a
    # second upsert on the autouse fixture's already-warmed store: see
    # test_lugo_disabled_cutoff.py's identical comment -- SqliteBackedStore
    # caches after its first _ensure(), so a second write on an already-warm
    # store silently skips re-initializing the (possibly-since-reconfigured)
    # backing table. A fresh store's first write always runs _ensure()
    # against whatever engine is currently live.
    fresh = ProfileStore(str(tmp_path / "profiles_bound.json"))
    fresh.upsert(Profile(name="bound-profile", owner_id=user["id"]))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)

    client = TestClient(app)
    with client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        ws.send_json({
            "type": "wakeup",
            "audio_params": {"format": "opus", "sample_rate": 16000},
        })
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
