import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.schemas.health import EngineHealth
from app.schemas.stt import STTResult
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store


class _StubSTT(STTProvider):
    name = "stub-idle-stt"
    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


def _patch_conversation(monkeypatch, *, stt_engine=None, tts_engine=None, **overrides):
    """default_stt_engine/default_tts_engine live on system_config_store's
    `engines` group; everything else here (e.g. conversation_goodbye_text)
    lives on its `conversation` group -- neither is on Settings. Patch the
    shared singleton's .get() (not .set()) so this never writes through to the
    shared config_system DB row (see conftest.py's _hermetic for why). Wraps
    whatever .get() currently resolves to, so per-test overrides compose with
    the autouse fixture below.
    """
    _real_get = system_config_store.get

    def _get_with_overrides():
        cfg = _real_get()
        engine_overrides = {}
        if stt_engine is not None:
            engine_overrides["default_stt_engine"] = stt_engine
        if tts_engine is not None:
            engine_overrides["default_tts_engine"] = tts_engine
        updated = cfg
        if engine_overrides:
            updated = updated.model_copy(
                update={"engines": updated.engines.model_copy(update=engine_overrides)}
            )
        if overrides:
            updated = updated.model_copy(
                update={"conversation": updated.conversation.model_copy(update=overrides)}
            )
        return updated

    monkeypatch.setattr(system_config_store, "get", _get_with_overrides)


@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    # Named distinctly from conftest.py's `_hermetic` so both autouse fixtures
    # run (a same-named fixture here would shadow, not compose with, the
    # global one -- see conftest.py's _hermetic for what that one handles).
    # Default: no spoken farewell (tests that want it opt in). Keeps the plain
    # idle tests from depending on a TTS provider.
    _patch_conversation(monkeypatch, stt_engine="stub-idle-stt", conversation_goodbye_text="")
    stt_service.providers["stub-idle-stt"] = _StubSTT()

    async def _ok_health(stt_engine, stt_model, tts_engine, tts_model):
        return (
            EngineHealth(engine=stt_engine, status="ok"),
            EngineHealth(engine=tts_engine, status="ok"),
        )

    # "stub-idle-stt" isn't recognized by stt_service's real engine-listing
    # logic (and the default TTS engine isn't actually configured in this
    # hermetic environment either), so the Task 7 health gate's
    # check_resolved_engines() would refuse/KeyError before the idle-timeout
    # behavior under test ever runs. Stub the gate out.
    monkeypatch.setattr("app.api.routes.lugo.check_resolved_engines", _ok_health)
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="fast", session=SessionConfig(idle_timeout_s=1)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    # Make the watchdog tick fast so the test is quick.
    monkeypatch.setattr("app.api.routes.lugo._IDLE_TICK_S", 0.1, raising=False)
    yield
    stt_service.providers.pop("stub-idle-stt", None)


def _receive_until(ws, msg_type: str, attempts: int = 20) -> dict:
    """Drain messages until `msg_type` arrives. The protocol may interleave
    informational messages (e.g. `engines_ready` when the configured TTS
    engine isn't warm) at any point, so asserting on the literal next message
    makes the test depend on machine warm state -- and its failure path is
    what used to wedge the whole suite at TestClient portal teardown."""
    for _ in range(attempts):
        msg = ws.receive_json()
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"no '{msg_type}' message within {attempts} messages")


def test_idle_timeout_emits_goodbye():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "fast",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        # say nothing; within ~1s the server should give up
        msg = _receive_until(ws, "goodbye")
        assert msg["reason"] == "idle_timeout"


def test_idle_countdown_starts_after_the_bot_finishes(monkeypatch):
    """The idle countdown must start when the bot FINISHES replying, not before —
    a slow turn's think/response time must NOT be counted toward idle.

    Drives a turn that takes ~1.5s (> idle_timeout_s=1) with the client sitting
    silent (no uplink), then measures the gap between the turn's tts-stop and the
    idle goodbye. If the think time were counted (the bug), goodbye fires almost
    immediately after tts-stop (~one 0.1s tick); with the fix it fires ~idle (1s)
    later. Asserting the gap is close to idle catches the regression."""
    import asyncio
    import json as _json
    import time as _time
    from app.core.audio import pcm16_to_wav_bytes
    from app.schemas.tts import TTSResult
    from app.services.artifacts import artifact_store
    from app.services.tts.base import TTSProvider
    from app.services.tts.service import tts_service

    class _SlowTTS(TTSProvider):
        name = "stub-slow-tts"
        async def synthesize(self, payload) -> TTSResult:
            await asyncio.sleep(1.5)  # turn takes longer than idle_timeout_s=1
            wav = pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000)
            _, url = artifact_store.save_wav(wav)
            return TTSResult(engine=self.name, sample_rate=24000, audio_url=url,
                             duration_seconds=0.1, text=payload.text)

    _patch_conversation(monkeypatch, tts_engine="stub-slow-tts")
    tts_service.providers["stub-slow-tts"] = _SlowTTS()
    try:
        with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
            ws.send_json({"type": "wakeup", "profile": "fast",
                          "audio_params": {"format": "opus", "sample_rate": 16000}})
            assert ws.receive_json()["type"] == "welcome"
            ws.send_json({"type": "text", "text": "hi"})  # echo responder -> slow TTS
            # Consume the turn up to and including its tts stop.
            for _ in range(60):
                m = ws.receive()
                if m.get("bytes") is not None:
                    continue
                d = _json.loads(m["text"])
                if d.get("type") == "tts" and d.get("state") == "stop":
                    break
            bot_done = _time.monotonic()
            # Now sit silent and wait for the idle goodbye; time how long it takes.
            for _ in range(60):
                m = ws.receive()
                if m.get("bytes") is not None:
                    continue
                d = _json.loads(m["text"])
                if d.get("type") == "goodbye":
                    break
            gap = _time.monotonic() - bot_done
            assert d["type"] == "goodbye"
            # Countdown restarted at turn end -> ~idle (1s). The bug fires ~immediately.
            assert gap >= 0.6, f"idle fired {gap:.2f}s after the bot finished (think time was counted)"
    finally:
        tts_service.providers.pop("stub-slow-tts", None)


def test_idle_speaks_farewell_before_goodbye(monkeypatch):
    """On idle timeout the bot says a spoken farewell (TTS) right before the
    goodbye/disconnect."""
    import json as _json
    from app.core.audio import pcm16_to_wav_bytes
    from app.schemas.tts import TTSResult
    from app.services.artifacts import artifact_store
    from app.services.tts.base import TTSProvider
    from app.services.tts.service import tts_service

    class _FarewellTTS(TTSProvider):
        name = "stub-fw-tts"
        async def synthesize(self, payload) -> TTSResult:
            wav = pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000)
            _, url = artifact_store.save_wav(wav)
            return TTSResult(engine=self.name, sample_rate=24000, audio_url=url,
                             duration_seconds=0.1, text=payload.text)

    _patch_conversation(monkeypatch, tts_engine="stub-fw-tts", conversation_goodbye_text="Tạm biệt nha")
    tts_service.providers["stub-fw-tts"] = _FarewellTTS()
    try:
        with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
            ws.send_json({"type": "wakeup", "profile": "fast",
                          "audio_params": {"format": "opus", "sample_rate": 16000}})
            assert ws.receive_json()["type"] == "welcome"
            saw_farewell = False
            for _ in range(60):
                m = ws.receive()
                if m.get("bytes") is not None:
                    continue  # farewell opus audio
                d = _json.loads(m["text"])
                if d.get("type") == "tts" and d.get("text") and "biệt" in d["text"]:
                    saw_farewell = True
                if d.get("type") == "goodbye":
                    break
            assert d["type"] == "goodbye" and d["reason"] == "idle_timeout"
            assert saw_farewell, "no spoken farewell was sent before the idle goodbye"
    finally:
        tts_service.providers.pop("stub-fw-tts", None)
