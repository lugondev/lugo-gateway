"""Hanging up belongs to the speaking path, not to whoever decided to hang up.

Two ways a conversation ends by choice rather than by timeout: the user says so
("tạm biệt", "tắt đi") and the model calls end_conversation, or the idle watchdog
arms the same flag. Both then behave identically: the reply is spoken to the end,
the device is given time to play it, and only then does the socket go.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.services.conversation.session import ConversationSession
from app.services.conversation.tools.base import ToolContext
from app.services.conversation.tools.local import LocalToolSource
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-end-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="tạm biệt nhé", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-end-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000), "audio/wav"


@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    # NOT named `_hermetic`: that would shadow conftest.py's autouse fixture of the
    # same name rather than compose with it, and the connection would be rejected
    # before it ever reached the code under test.
    _real_get = system_config_store.get

    def _get():
        cfg = _real_get()
        return cfg.model_copy(update={
            "engines": cfg.engines.model_copy(update={
                "default_stt_engine": _StubSTT.name, "default_tts_engine": _StubTTS.name,
            }),
            # Keep the drain measurable but quick.
            "conversation": cfg.conversation.model_copy(
                update={"conversation_farewell_drain_s": 0.2}
            ),
        })

    monkeypatch.setattr(system_config_store, "get", _get)
    stt_service.providers[_StubSTT.name] = _StubSTT()
    tts_service.providers[_StubTTS.name] = _StubTTS()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    # idle_timeout_s=0: nothing here is about the idle path.
    fresh.upsert(Profile(name="fast", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop(_StubSTT.name, None)
    tts_service.providers.pop(_StubTTS.name, None)


@pytest.mark.asyncio
async def test_the_tool_arms_the_hang_up_rather_than_disconnecting():
    """The goodbye the user hears is the model's next reply, so the tool must not
    close anything itself -- it only says "hang up once you have spoken"."""
    armed: list[str] = []
    ctx = ToolContext(end_conversation=armed.append)
    tool = next(
        t for t in LocalToolSource(end_conversation=True).list_tools()
        if t.name == "end_conversation"
    )

    result = await tool.run({}, ctx)

    assert armed == ["user_goodbye"]
    assert "goodbye" in result.lower()


@pytest.mark.asyncio
async def test_a_client_that_cannot_hang_up_is_told_so():
    """A browser tab has no disconnect. Better the model knows than promises it."""
    tool = next(
        t for t in LocalToolSource(end_conversation=True).list_tools()
        if t.name == "end_conversation"
    )

    result = await tool.run({}, ToolContext())

    assert "stays connected" in result.lower()


def _capture_sessions(monkeypatch) -> list[ConversationSession]:
    """Hand the test the live session object without putting a hook in the route."""
    made: list[ConversationSession] = []

    class _Capturing(ConversationSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr("app.api.routes.lugo.ConversationSession", _Capturing)
    return made


def _wakeup(ws) -> None:
    ws.send_json({"type": "wakeup", "profile": "fast",
                  "audio_params": {"format": "opus", "sample_rate": 16000}})
    assert ws.receive_json()["type"] == "welcome"


def test_the_connection_closes_after_the_goodbye_is_spoken(monkeypatch):
    """End to end through the route: arm the flag the way the tool does, then let a
    turn run. The goodbye must be sent AFTER the reply's tts stop, never before."""
    seen: list[dict] = []
    sessions = _capture_sessions(monkeypatch)
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws)
        # Arm it exactly as the tool would, from inside the connection's session.
        session = sessions[-1]
        session.close_after_speaking = "user_goodbye"

        ws.send_json({"type": "text", "text": "tạm biệt nhé"})
        for _ in range(60):
            message = ws.receive()
            if message.get("bytes") is not None or "text" not in message:
                continue
            frame = json.loads(message["text"])
            seen.append(frame)
            if frame.get("type") == "goodbye":
                break

    kinds = [(f.get("type"), f.get("state")) for f in seen]
    assert ("goodbye", None) in kinds, f"never hung up: {kinds}"
    assert ("tts", "stop") in kinds, f"the reply was never finished: {kinds}"
    # Order is the whole point: speak, then hang up.
    assert kinds.index(("tts", "stop")) < kinds.index(("goodbye", None))
    assert seen[-1]["reason"] == "user_goodbye"


def test_speaking_over_the_goodbye_cancels_the_hang_up(monkeypatch):
    """The user came back mid-farewell. There is nobody to say goodbye to any more,
    so the connection stays up rather than dropping on someone mid-sentence."""
    async def _slow_render(payload):
        await asyncio.sleep(0.4)
        return pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000), "audio/wav"

    monkeypatch.setattr(_StubTTS, "render_audio", staticmethod(_slow_render))

    sessions = _capture_sessions(monkeypatch)
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws)
        session = sessions[-1]
        session.close_after_speaking = "idle_timeout"

        ws.send_json({"type": "text", "text": "tạm biệt"})
        _await(ws, ("tts", "start"))        # the goodbye is being spoken...
        ws.send_json({"type": "abort"})     # ...and the user cuts in

        stop = _await(ws, ("tts", "stop"))
        assert stop.get("reason"), "the stop should carry the barge-in reason"
        assert session.close_after_speaking is None, (
            "the hang-up stayed armed, so the next turn to finish would drop a user "
            "who is clearly still there"
        )


def _await(ws, want: tuple[str, str | None], attempts: int = 40) -> dict:
    for _ in range(attempts):
        message = ws.receive()
        if message.get("bytes") is not None or "text" not in message:
            continue
        frame = json.loads(message["text"])
        if (frame.get("type"), frame.get("state")) == want:
            return frame
    raise AssertionError(f"never saw {want}")
