import asyncio
import pytest
from fastapi.testclient import TestClient
from app.main import app
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
    `engines` group; everything else here (e.g. conversation_silence_ms)
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
    # No TTS engine is patched in here, so ConversationSession.announce finds no
    # provider and the pre-idle farewell is skipped: the plain idle tests stay about
    # timing, not about speech. The test that wants the farewell opts in below.
    _patch_conversation(monkeypatch, stt_engine="stub-idle-stt")
    stt_service.providers["stub-idle-stt"] = _StubSTT()
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


def test_a_streaming_but_silent_device_still_idles_out():
    """Mic frames are not activity.

    Found on hardware: an auto-wake ESP32 streams Opus continuously, and the loop
    used to refresh the idle countdown on every received message -- so on exactly
    the always-listening device the timeout exists for, it could never fire. The
    device's own watchdog (idle_timeout_s + grace) closed the link instead, which is
    why no pre-idle farewell was ever heard. Activity is speech, a turn, or audio
    playing, which is what docs/api.md always claimed.

    The assertion is about WHEN the goodbye fires, because that is the only thing
    that separates the two behaviours through a synchronous TestClient. Frames go up
    for 2.5x the idle window; then the first read is timed. Already queued (instant)
    means the server gave up while the stream was still running -- correct. A read
    that blocks for about a full idle window means the goodbye only started counting
    once the frames stopped, i.e. they were being treated as activity.
    """
    import time as _time

    silence = b"\x00" * 40   # not decodable Opus; feed_audio logs and skips it
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "fast",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"

        idle_s = 1.0                              # the "fast" profile's idle_timeout_s
        deadline = _time.monotonic() + idle_s * 2.5
        while _time.monotonic() < deadline:
            ws.send_bytes(silence)                # keep the uplink busy, say nothing
            _time.sleep(0.05)

        started = _time.monotonic()
        msg = _receive_until(ws, "goodbye")
        waited = _time.monotonic() - started

        assert msg["reason"] == "idle_timeout"
        assert waited < idle_s * 0.4, (
            f"the goodbye took {waited:.2f}s to arrive after the stream stopped, so "
            "the countdown had been restarting on every mic frame"
        )


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
    from app.services.tts.base import TTSProvider
    from app.services.tts.service import tts_service

    class _SlowTTS(TTSProvider):
        name = "stub-slow-tts"

        async def render_audio(self, payload) -> tuple[bytes, str]:
            await asyncio.sleep(1.5)  # turn takes longer than idle_timeout_s=1
            wav = pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000)
            return wav, "audio/wav"

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


def test_a_mic_that_never_goes_quiet_cannot_hold_the_countdown(monkeypatch):
    """An endpointer stuck "mid-utterance" must not stop the idle timeout.

    Measured against the real VAD: an open mic in a room with sustained sound
    leaves `endpointer.speaking` set essentially all the time -- loud constant
    noise held it 40s out of 40s (the endpoint fires only at max_utterance_ms and
    speech_start reopens immediately), TV-like bursts 31.7s out of 40s. A watchdog
    that paused on that flag never reached its own timeout on exactly the
    always-listening device it exists for: 28 seconds of server silence after a
    real turn, and the speaker hanging up on its own watchdog with nothing said.
    """
    import time as _time

    from app.services.conversation.endpointer import VadEndpointer

    # The pathological case in one line: this endpointer is ALWAYS mid-utterance.
    monkeypatch.setattr(VadEndpointer, "speaking", property(lambda self: True))

    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "fast",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        started = _time.monotonic()
        msg = _receive_until(ws, "goodbye", attempts=40)
        waited = _time.monotonic() - started

        assert msg["reason"] == "idle_timeout"
        # On time, not eventually: the overrun cap would also produce a goodbye
        # here, just far too late to beat the device's own watchdog -- which is
        # the whole failure. idle_timeout_s is 1s for this profile.
        assert waited < 5, (
            f"the goodbye took {waited:.1f}s: the countdown was being paused by a "
            "mic that never goes quiet, and only the overrun cap released it"
        )


def test_only_real_interaction_refreshes_the_idle_countdown():
    """What the VAD guesses is not interaction.

    Observed on the speaker: after one real exchange, background sound opened and
    closed an utterance twice in twenty seconds, each producing a turn whose
    transcript came back EMPTY. Every one of them refreshed the countdown, so the
    idle timeout -- and the farewell hanging off it -- never arrived.

    Tested against the policy directly rather than through the socket: driving it
    end-to-end needs Opus the VAD accepts as speech, and a version of this test
    that faked those turns with `flush` control frames passed against the OLD
    behaviour too (a control frame legitimately IS interaction), which is worse
    than having no test at all.
    """
    from app.api.routes.lugo import refreshes_idle

    # Guesses and their fallout: nothing the user did.
    assert not refreshes_idle("speech_start", {})
    assert not refreshes_idle("speech_end", {"speech_ms": 400})
    assert not refreshes_idle("processing", {"turn": 3})
    assert not refreshes_idle("user_transcript", {"text": "   "})
    assert not refreshes_idle("turn_done", {"turn": 3, "skipped": "empty transcript"})

    # Someone actually said something, or the bot actually answered.
    assert refreshes_idle("user_transcript", {"text": "chào Lugo"})
    assert refreshes_idle("response_text", {"text": "chào bạn"})
    assert refreshes_idle("audio_start", {"turn": 1})
    assert refreshes_idle("turn_done", {"turn": 1})
    assert refreshes_idle("aborted", {"reason": "barge-in"})


def test_the_farewell_survives_a_device_that_keeps_streaming(monkeypatch):
    """The failure the speaker actually had: goodbye never heard, idle straight away.

    `closing` is set BEFORE the farewell is written and spoken. The receive loop
    treated that flag as "tear down now", and an always-listening device streams
    mic frames continuously -- so `recv` completed the instant the flag was set and
    the socket closed mid-farewell. With nothing uplinking (a test client, a browser
    on mute) the loop stayed parked in asyncio.wait and the farewell was audible,
    which is exactly why this hid from every check that did not stream.
    """
    import json as _json
    import time as _time
    from app.core.audio import pcm16_to_wav_bytes
    from app.services.tts.base import TTSProvider
    from app.services.tts.service import tts_service

    class _FarewellTTS(TTSProvider):
        name = "stub-stream-fw-tts"

        async def render_audio(self, payload) -> tuple[bytes, str]:
            return pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000), "audio/wav"

    async def _fake_generate_line(*, responder, persona, history, language, event):
        await asyncio.sleep(0.3)   # an LLM call is not instant; that is the window
        return "Bạn im lặng rồi, mình tạm biệt nhé"

    _patch_conversation(monkeypatch, tts_engine="stub-stream-fw-tts",
                        conversation_farewell_drain_s=0.2)
    monkeypatch.setattr("app.services.conversation.session.generate_line", _fake_generate_line)
    tts_service.providers["stub-stream-fw-tts"] = _FarewellTTS()
    silence = b"\x00" * 40
    try:
        with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
            ws.send_json({"type": "wakeup", "profile": "fast",
                          "audio_params": {"format": "opus", "sample_rate": 16000}})
            assert ws.receive_json()["type"] == "welcome"
            ws.send_json({"type": "text", "text": "chào Lugo"})
            for _ in range(60):
                m = ws.receive()
                if m.get("bytes") is not None:
                    continue
                d = _json.loads(m["text"])
                if d.get("type") == "tts" and d.get("state") == "stop":
                    break

            # Keep the uplink alive across the idle mark (profile idle is 1s), the
            # way a device with an open mic does.
            deadline = _time.monotonic() + 2.0
            while _time.monotonic() < deadline:
                ws.send_bytes(silence)
                _time.sleep(0.05)

            saw_farewell = False
            saw_goodbye = False
            for _ in range(80):
                m = ws.receive()
                if m.get("bytes") is not None:
                    continue
                if "text" not in m:
                    break            # socket closed on us; assertions below say why
                d = _json.loads(m["text"])
                if d.get("type") == "tts" and d.get("text") and "tạm biệt" in d["text"]:
                    saw_farewell = True
                if d.get("type") == "goodbye":
                    saw_goodbye = d["reason"] == "idle_timeout"
                    break
            assert saw_farewell, (
                "the connection closed before the farewell was spoken -- a streaming "
                "device tore the socket down through the `closing` flag"
            )
            assert saw_goodbye, "no idle goodbye followed the farewell"
    finally:
        tts_service.providers.pop("stub-stream-fw-tts", None)


def test_idle_speaks_farewell_before_goodbye(monkeypatch):
    """On idle timeout the bot says a spoken farewell (TTS) right before the
    goodbye/disconnect -- and the words come from the profile's LLM, not from a
    phrase stored in config. generate_line is stubbed so the assertion is about the
    wiring (announce -> speak -> wire) rather than about what a model felt like
    saying."""
    import json as _json
    from app.core.audio import pcm16_to_wav_bytes
    from app.services.tts.base import TTSProvider
    from app.services.tts.service import tts_service

    class _FarewellTTS(TTSProvider):
        name = "stub-fw-tts"

        async def render_audio(self, payload) -> tuple[bytes, str]:
            wav = pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000)
            return wav, "audio/wav"

    seen_events: list[str] = []

    async def _fake_generate_line(*, responder, persona, history, language, event):
        seen_events.append(event)
        return "Tạm biệt nha, hẹn gặp lại"

    # Real drain is seconds of dead air on purpose (the device is still playing);
    # here it only needs to be non-zero to prove the goodbye waits for it.
    _patch_conversation(monkeypatch, tts_engine="stub-fw-tts", conversation_farewell_drain_s=0.2)
    monkeypatch.setattr("app.services.conversation.session.generate_line", _fake_generate_line)
    tts_service.providers["stub-fw-tts"] = _FarewellTTS()
    try:
        with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
            ws.send_json({"type": "wakeup", "profile": "fast",
                          "audio_params": {"format": "opus", "sample_rate": 16000}})
            assert ws.receive_json()["type"] == "welcome"
            # One real exchange first: there is no farewell for a conversation that
            # never happened (see the watchdog's session.turn check).
            ws.send_json({"type": "text", "text": "chào Lugo"})
            for _ in range(60):
                m = ws.receive()
                if m.get("bytes") is not None:
                    continue
                d = _json.loads(m["text"])
                if d.get("type") == "tts" and d.get("state") == "stop":
                    break
            saw_farewell = False
            kept_alive_before_farewell = False
            seen_processing = False
            for _ in range(60):
                m = ws.receive()
                if m.get("bytes") is not None:
                    continue  # farewell opus audio
                d = _json.loads(m["text"])
                if d.get("type") == "processing":
                    seen_processing = True
                if d.get("type") == "tts" and d.get("text") and "biệt" in d["text"]:
                    saw_farewell = True
                    kept_alive_before_farewell = seen_processing
                if d.get("type") == "goodbye":
                    break
            assert d["type"] == "goodbye" and d["reason"] == "idle_timeout"
            assert saw_farewell, "no spoken farewell was sent before the idle goodbye"
            assert seen_events == ["idle_goodbye"]
            # Writing the line costs an LLM call plus synthesis, and the device hangs
            # up on its own after a few seconds of server silence. Something has to go
            # out first or the farewell races the device's watchdog.
            assert kept_alive_before_farewell, (
                "nothing was sent between the idle decision and the farewell, so the "
                "device's own watchdog can close the link while the line is written"
            )
    finally:
        tts_service.providers.pop("stub-fw-tts", None)
