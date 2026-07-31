"""Starting a new conversation without dropping the WebSocket.

session_id used to be fixed for the lifetime of a connection. That is wrong for a
mains-powered speaker: it holds one socket open for days, so everything it says
lands in a single History entry, the LLM context grows without bound, and memory
extraction -- which only runs in close() -- never runs at all. The RPi client makes
it worse by persisting session_id to disk and resuming it after a restart, so the
conversation is not merely long-lived, it is permanent.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.history.store import session_store
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-rotate-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-rotate-tts"

    async def render_audio(self, payload) -> tuple[bytes, str]:
        return pcm16_to_wav_bytes(b"\x00\x00" * 2400, sample_rate=24000), "audio/wav"


@pytest.fixture(autouse=True)
def _local_hermetic(monkeypatch, tmp_path):
    """Same shape as test_lugo_stream.py's fixture -- see its comments."""
    _real_get = system_config_store.get

    def _get_with_stub_engines():
        cfg = _real_get()
        return cfg.model_copy(update={
            "engines": cfg.engines.model_copy(update={
                "default_stt_engine": "stub-rotate-stt",
                "default_tts_engine": "stub-rotate-tts",
            })
        })

    monkeypatch.setattr(system_config_store, "get", _get_with_stub_engines)
    stt_service.providers["stub-rotate-stt"] = _StubSTT()
    tts_service.providers["stub-rotate-tts"] = _StubTTS()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-rotate-stt", None)
    tts_service.providers.pop("stub-rotate-tts", None)


def _wakeup(ws) -> str:
    ws.send_json({"type": "wakeup", "profile": "dev",
                  "audio_params": {"format": "opus", "sample_rate": 16000}})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    return welcome["session_id"]


def _one_turn(ws, text: str = "hi") -> None:
    """Send a text turn and drain until the reply finishes."""
    ws.send_json({"type": "text", "text": text})
    for _ in range(40):
        message = ws.receive()
        if message.get("bytes") is not None:
            continue
        m = json.loads(message["text"])
        if m["type"] == "tts" and m.get("state") == "stop":
            return
    raise AssertionError("turn never finished")


def _await_type(ws, wanted: str) -> dict:
    for _ in range(40):
        message = ws.receive()
        if message.get("bytes") is not None:
            continue
        m = json.loads(message["text"])
        if m["type"] == wanted:
            return m
    raise AssertionError(f"never saw {wanted}")


def _drain_announcement(ws) -> None:
    """A rotation nobody spoke for is followed by a spoken "fresh start" line
    (ConversationSession.announce). Drain its tts start/stop, or the next turn's
    drain loop returns on THIS utterance's stop and closes the socket while that
    turn is still running."""
    for _ in range(40):
        message = ws.receive()
        if message.get("bytes") is not None:
            continue
        m = json.loads(message["text"])
        if m["type"] == "tts" and m.get("state") == "stop":
            return
    raise AssertionError("the rotation announcement never finished")


def _row(session_id: str) -> dict | None:
    return asyncio.run(session_store.get(session_id))


def _messages(session_id: str) -> list[dict]:
    return asyncio.run(session_store.get_messages(session_id))


def test_new_session_mints_a_new_id_and_ends_the_old_one():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        first = _wakeup(ws)
        _one_turn(ws)

        ws.send_json({"type": "new_session"})
        rotated = _await_type(ws, "session_new")

        assert rotated["previous_session_id"] == first
        assert rotated["session_id"] != first

        # The device MUST be told the new id: it persists session_id to disk and
        # resumes it on reconnect, so missing this would take it straight back
        # into the conversation it just asked to leave.
        assert rotated["session_id"]

    old = _row(first)
    assert old is not None and old["ended_at"] is not None


def test_the_new_session_inherits_profile_and_owner():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        _wakeup(ws)
        _one_turn(ws)
        ws.send_json({"type": "new_session"})
        new_id = _await_type(ws, "session_new")["session_id"]
        _drain_announcement(ws)
        # The fresh conversation gets its row when it first has something in it,
        # so give it something -- an id with nothing behind it is not yet a
        # conversation, which is the point of lazy creation.
        _one_turn(ws, "second conversation")

    row = _row(new_id)
    assert row is not None
    assert row["profile_id"] == "dev"
    # No `or profile.owner_id` fallback anywhere on this path (H2): an
    # unauthenticated caller must produce an ownerless row, not one attributed
    # to whoever happens to own the named profile.
    assert row["user_id"] in (None, "")


def test_conversations_do_not_bleed_across_a_rotation():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        first = _wakeup(ws)
        _one_turn(ws, "first conversation")
        ws.send_json({"type": "new_session"})
        second = _await_type(ws, "session_new")["session_id"]
        _drain_announcement(ws)
        _one_turn(ws, "second conversation")

    before = _messages(first)
    after = _messages(second)
    assert before and after
    assert "first conversation" in json.dumps(before)
    assert "first conversation" not in json.dumps(after)
    # Turn numbering restarts, so the new conversation reads as turn 1 rather
    # than continuing a counter the user has no way to see. The spoken "fresh
    # start" line (ConversationSession.announce) sits at turn 0: it belongs to no
    # exchange, having been said before the user's first word.
    exchanges = [m for m in after if m["turn"] > 0]
    assert exchanges, f"only the announcement was stored: {after}"
    assert min(m["turn"] for m in exchanges) == 1


def test_rotating_an_empty_session_is_a_no_op():
    """Pressing the button twice must not litter History with empty rows."""
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        first = _wakeup(ws)
        ws.send_json({"type": "new_session"})
        again = _await_type(ws, "session_new")
        assert again["session_id"] == first

        ws.send_json({"type": "new_session"})
        assert _await_type(ws, "session_new")["session_id"] == first


def _await_event(ws, wanted: str) -> dict:
    """Drain the /v1/conversation/stream wire ({"event": ...}) until `wanted`."""
    for _ in range(60):
        message = ws.receive()
        if message.get("bytes") is not None:
            continue
        m = json.loads(message["text"])
        if m.get("event") == wanted:
            return m
    raise AssertionError(f"never saw {wanted}")


def test_conversation_stream_rotates_too():
    with TestClient(app).websocket_connect("/v1/conversation/stream?profile=dev") as ws:
        first = _await_event(ws, "session_started")["session_id"]
        ws.send_json({"type": "text", "text": "hi"})
        _await_event(ws, "turn_done")

        ws.send_json({"type": "new_session"})
        rotated = _await_event(ws, "session_rotated")
        assert rotated["previous_session_id"] == first
        assert rotated["session_id"] != first


class _SlowTurnSession(ConversationSession):
    """A session whose "turn" is a bare sleep, so a rotation can be requested
    while one is provably still running. Only the turn body is faked -- rotate()
    and the deferral live on the real class."""

    async def start_slow_turn(self, seconds: float = 0.05) -> None:
        self.turn = 1  # a turn's worth of history exists, so rotate mints a new id
        # ...and the row that turn would have written (rotate ends a CONVERSATION,
        # and one only exists once something has been said).
        self._row_exists = True
        await session_store.create(self.cfg.session_id)
        self.current_turn = asyncio.create_task(asyncio.sleep(seconds))

    async def start_and_run(self, seconds: float = 0.05) -> None:
        await self.start()
        await self.start_slow_turn(seconds)


def _rotate_cfg(**over) -> SessionRuntimeConfig:
    base = dict(
        session_id="rotate-defer-1", profile_name=None, stt_engine="stub-rotate-stt",
        language="vi", tts_engine="stub-rotate-tts", voice=None, ref_audio_path=None,
        ref_text=None, tts_instruct=None, tts_speed=None, tts_language=None,
        sample_rate=16000, output_sample_rate=24000, audio_codec="pcm16",
        want_audio=False, want_text=True, audio_out="wav", denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


@pytest.mark.asyncio
async def test_new_session_mid_turn_waits_for_the_turn_to_finish():
    """The voice path asks for this from INSIDE a turn (the self.session.new MCP
    tool fires while the model waits on its result). Rotating right then cancels
    the turn that asked, so the assistant never gets to confirm."""
    events: list[tuple[str, dict]] = []

    async def emit(name, **payload):
        events.append((name, payload))

    session = _SlowTurnSession(_rotate_cfg(), emit, lambda _pkt: None)
    await session.start_and_run()
    events.clear()                                # drop start()'s session_started

    await session.request_rotate("client")
    assert [n for n, _ in events] == []           # parked, not performed

    await session.current_turn
    await asyncio.sleep(0)                        # let the done-callback run
    for _ in range(20):                           # ...and its spawned rotate()
        if any(n == "session_rotated" for n, _ in events):
            break
        await asyncio.sleep(0.01)

    rotated = [p for n, p in events if n == "session_rotated"]
    assert rotated, f"never rotated: {[n for n, _ in events]}"
    assert rotated[0]["previous_session_id"] == "rotate-defer-1"
    assert rotated[0]["session_id"] != "rotate-defer-1"
    # The deferred turn was left alone: no abort anywhere in the sequence.
    assert "aborted" not in [n for n, _ in events]


@pytest.mark.asyncio
async def test_a_deferred_rotation_still_happens_if_the_turn_is_cancelled():
    """Barge-in (or any abort) during the deferred turn must not swallow the
    request: the user asked to start over, and silently staying in the old
    conversation is the failure that matters."""
    events: list[tuple[str, dict]] = []

    async def emit(name, **payload):
        events.append((name, payload))

    session = _SlowTurnSession(_rotate_cfg(session_id="rotate-defer-2"), emit, lambda _pkt: None)
    await session.start_and_run(seconds=5)

    await session.request_rotate("client")
    await session.abort("barge-in")
    for _ in range(20):
        if any(n == "session_rotated" for n, _ in events):
            break
        await asyncio.sleep(0.01)

    assert any(n == "session_rotated" for n, _ in events), [n for n, _ in events]


@pytest.mark.asyncio
async def test_closing_drops_a_rotation_parked_behind_the_last_turn():
    """close() cancels the in-flight turn, which fires the parked rotation. It
    must not mint a session row on the way out -- nothing would ever end it."""
    events: list[tuple[str, dict]] = []

    async def emit(name, **payload):
        events.append((name, payload))

    session = _SlowTurnSession(_rotate_cfg(session_id="rotate-defer-3"), emit, lambda _pkt: None)
    await session.start_and_run(seconds=5)

    await session.request_rotate("client")
    await session.close()
    for _ in range(5):
        await asyncio.sleep(0.01)

    assert "session_rotated" not in [n for n, _ in events]
    assert session.cfg.session_id == "rotate-defer-3"


def test_reset_still_keeps_writing_to_the_same_session():
    """`reset` is documented wire API meaning "clear conversation history", and it
    clears only the in-memory context. Left exactly as it was: changing what an
    existing message means is worse than adding a new one. This pins the
    difference from new_session so the two can't quietly converge."""
    with TestClient(app).websocket_connect("/v1/conversation/stream?profile=dev") as ws:
        session_id = _await_event(ws, "session_started")["session_id"]
        ws.send_json({"type": "text", "text": "before reset"})
        _await_event(ws, "turn_done")

        ws.send_json({"type": "reset"})
        _await_event(ws, "reset")

        ws.send_json({"type": "text", "text": "after reset"})
        _await_event(ws, "turn_done")

    stored = json.dumps(_messages(session_id))
    assert "before reset" in stored
    assert "after reset" in stored
