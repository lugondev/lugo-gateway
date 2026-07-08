"""Lugo device protocol route: WS /v1/lugo/stream.

Handshake: the first client message must be a ``wakeup`` declaring a profile
name. The server resolves engines/TTS params from that profile (the device
never sends raw engine/voice choices), builds a protocol-neutral
``ConversationSession``, and replies with ``welcome``. From there, neutral
core events are translated to the Lugo wire format and Opus audio packets are
wrapped with the v3 binary frame header.
"""

import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.settings import settings
from app.services.conversation.lugo_frame import LUGO_FRAME_OPUS, encode_frame
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.profiles.store import profile_store
from app.services.tts.profile_store import tts_profile_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/lugo", tags=["lugo"])

# neutral event -> lugo tts state
_TTS_STATE = {"audio_start": "start", "audio_end": "stop", "response_text": "sentence_start"}


def _resolve(profile_name: str | None):
    """Resolve engines/tts params from a profile (server owns everything)."""
    profile = profile_store.get(profile_name) if profile_name else None
    stt_engine = settings.conversation_stt_engine or settings.default_stt_engine
    language = settings.conversation_language or None
    tts_name = (profile.tts.profile_name if profile else "") or None
    tts_profile = tts_profile_store.get(tts_name) if tts_name else None
    if tts_profile and tts_profile.engine:
        tts = dict(engine=tts_profile.engine, voice=tts_profile.voice or None,
                   ref_audio_path=tts_profile.ref_audio_path or None, ref_text=tts_profile.ref_text or None,
                   instruct=tts_profile.instruct or None, speed=tts_profile.speed, language=tts_profile.language)
    else:
        tts = dict(engine=settings.conversation_tts_engine or settings.default_tts_engine,
                   voice=None, ref_audio_path=None, ref_text=None, instruct=None, speed=None, language=None)
    idle = profile.session.idle_timeout_s if profile else 30
    return profile, stt_engine, language, tts, idle


@router.websocket("/stream")
async def lugo_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    # Handshake: first frame must be a `wakeup`.
    message = await websocket.receive()
    if message.get("type") == "websocket.disconnect":
        return
    if message.get("bytes") is not None:
        # Binary first frame is not a valid wakeup.
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return
    try:
        hello = json.loads(message.get("text") or "")
    except json.JSONDecodeError:
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return
    if hello.get("type") != "wakeup":
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return

    profile_name = hello.get("profile")
    profile, stt_engine, language, tts, idle = _resolve(profile_name)
    if profile_name and not profile:
        await websocket.send_json({"type": "error", "message": f"profile '{profile_name}' not found"})
        await websocket.close()
        return

    session_id = str(uuid.uuid4())
    in_sr = int((hello.get("audio_params") or {}).get("sample_rate", settings.stt_stream_sample_rate))
    out_sr = 24000
    cfg = SessionRuntimeConfig(
        session_id=session_id, profile_name=profile_name, stt_engine=stt_engine, language=language,
        tts_engine=tts["engine"], voice=tts["voice"], ref_audio_path=tts["ref_audio_path"],
        ref_text=tts["ref_text"], tts_instruct=tts["instruct"], tts_speed=tts["speed"],
        tts_language=tts["language"], sample_rate=in_sr, output_sample_rate=out_sr,
        audio_codec="opus", want_audio=True, want_text=True, audio_out="opus",
        denoise=False, resume_sid=None,
    )

    async def emit(event: str, **payload) -> None:
        if event == "user_transcript":
            await websocket.send_json({"type": "stt", "text": payload.get("text", ""), "final": True})
        elif event in _TTS_STATE:
            msg = {"type": "tts", "state": _TTS_STATE[event]}
            if payload.get("text"):
                msg["text"] = payload["text"]
            await websocket.send_json(msg)
        elif event == "command":
            await websocket.send_json({"type": "mcp", **payload})
        elif event == "error":
            await websocket.send_json({"type": "error", "message": payload.get("message", "")})
        # session_started / processing / turn_done / audio_chunk / engines_ready: not on the wire

    async def emit_audio(packet: bytes) -> None:
        await websocket.send_bytes(encode_frame(LUGO_FRAME_OPUS, packet))

    session = ConversationSession(cfg, emit, emit_audio)
    await session.start()
    await websocket.send_json({
        "type": "welcome", "session_id": session_id, "transport": "websocket",
        "audio_params": {"sample_rate": out_sr}, "idle_timeout_s": idle,
    })

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                # Phase 1: device sends raw opus frames (v3 wrapping optional on uplink).
                await session.feed_audio(message["bytes"])
            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "text":
                    await session.feed_text(control.get("text") or "")
                elif ctype == "abort":
                    await session.abort("barge-in")
                elif ctype == "listen":
                    pass  # Phase 1 auto mode: server VAD drives turns
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
