"""Lugo device protocol route: WS /v1/lugo/stream.

Handshake: the first client message must be a ``wakeup`` declaring a profile
name. The server resolves engines/TTS params from that profile (the device
never sends raw engine/voice choices), builds a protocol-neutral
``ConversationSession``, and replies with ``welcome``. From there, neutral
core events are translated to the Lugo wire format and Opus audio packets are
wrapped with the v3 binary frame header.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.settings import settings
from app.services.conversation.lugo_frame import LUGO_FRAME_OPUS, encode_frame
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.conversation.tools.device_mcp import (
    DeviceMcpToolSource, DeviceMcpTransport, discover_device_tools,
)
from app.services.profiles.store import profile_store
from app.services.stt.profile import resolve_stt
from app.services.tts.profile_store import tts_profile_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/lugo", tags=["lugo"])

# How often the idle watchdog wakes to check for inactivity (test-tunable).
_IDLE_TICK_S = 1.0

def _resolve(profile_name: str | None):
    """Resolve engines/tts params from a profile (server owns everything)."""
    profile = profile_store.get(profile_name) if profile_name else None
    # Resolve STT from the profile's SttConfig (engine/language or a language
    # preset), falling back to server defaults — same single source of truth the
    # conversation stream uses, so a device that sends only a profile id streams
    # against that profile's STT. No query params on the Lugo wire.
    stt_engine, language, stt_model = resolve_stt(profile)
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
    return profile, stt_engine, language, stt_model, tts, idle


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
    if not isinstance(hello, dict):
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return
    if hello.get("type") != "wakeup":
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return

    profile_name = hello.get("profile")
    profile, stt_engine, language, stt_model, tts, idle = _resolve(profile_name)
    if profile_name and not profile:
        await websocket.send_json({"type": "error", "message": f"profile '{profile_name}' not found"})
        await websocket.close()
        return

    requested_sid = hello.get("session_id")
    if not isinstance(requested_sid, str) or not requested_sid:
        requested_sid = None
    session_id = requested_sid or str(uuid.uuid4())
    try:
        in_sr = int((hello.get("audio_params") or {}).get("sample_rate", settings.stt_stream_sample_rate))
    except (TypeError, ValueError):
        in_sr = settings.stt_stream_sample_rate
    try:
        out_sr = int((hello.get("audio_params") or {}).get("output_sample_rate", 24000))
    except (TypeError, ValueError):
        out_sr = 24000
    cfg = SessionRuntimeConfig(
        session_id=session_id, profile_name=profile_name, stt_engine=stt_engine, language=language,
        tts_engine=tts["engine"], voice=tts["voice"], ref_audio_path=tts["ref_audio_path"],
        ref_text=tts["ref_text"], tts_instruct=tts["instruct"], tts_speed=tts["speed"],
        tts_language=tts["language"], sample_rate=in_sr, output_sample_rate=out_sr,
        audio_codec="opus", want_audio=True, want_text=True, audio_out="opus",
        denoise=False, resume_sid=requested_sid, stt_model=stt_model,
    )

    speaking = False  # one tts{start} on first response/audio, one tts{stop} at turn end/abort
    # Idle countdown baseline. Refreshed on every server-side event (esp. the
    # turn's final turn_done/aborted) so the idle timer only starts AFTER the bot
    # finishes replying — a slow LLM/TTS or slow network never counts toward idle
    # (that window is also covered by session.is_turn_active() in the watchdog).
    last_activity = time.monotonic()
    engine_status = {"stt_ready": True, "tts_ready": True}

    async def emit(event: str, **payload) -> None:
        nonlocal speaking, last_activity
        last_activity = time.monotonic()  # any turn progress/end = activity
        if event == "user_transcript":
            await websocket.send_json({"type": "stt", "text": payload.get("text", ""), "final": True})
        elif event in ("response_text", "audio_start"):
            # First sign the bot is responding (text or audio) opens the turn.
            # response_text always precedes audio_start in the core, so tts{start}
            # is the first tts frame; this also works in the no-opus fallback
            # path where audio_start never fires (only response_text/audio_chunk).
            if not speaking:
                speaking = True
                await websocket.send_json({"type": "tts", "state": "start"})
            if event == "response_text":
                await websocket.send_json({"type": "tts", "state": "sentence_start", "text": payload.get("text", "")})
        elif event in ("turn_done", "aborted"):
            if speaking:
                speaking = False
                await websocket.send_json({"type": "tts", "state": "stop"})
        elif event == "command":
            await websocket.send_json({"type": "command", **payload})
        elif event == "error":
            await websocket.send_json({"type": "error", "message": payload.get("message", "")})
        elif event == "session_started":
            engine_status["stt_ready"] = bool(payload.get("stt_ready", True))
            engine_status["tts_ready"] = bool(payload.get("tts_ready", True))
        # processing / audio_end / reset: not on the wire (speech_start/speech_end/
        # processing/engines_ready are handled in Task 3/4 below)

    async def emit_audio(packet: bytes) -> None:
        await websocket.send_bytes(encode_frame(LUGO_FRAME_OPUS, packet))

    session = ConversationSession(cfg, emit, emit_audio)
    await session.start()
    await websocket.send_json({
        "type": "welcome", "session_id": session_id, "transport": "websocket",
        "audio_params": {"sample_rate": out_sr}, "idle_timeout_s": idle,
        "stt_ready": engine_status["stt_ready"], "tts_ready": engine_status["tts_ready"],
    })

    device_mcp = bool((hello.get("features") or {}).get("mcp"))
    transport: DeviceMcpTransport | None = None
    discovery_task: asyncio.Task | None = None
    if device_mcp and settings.device_mcp_enabled:
        transport = DeviceMcpTransport(
            websocket.send_json,
            request_timeout=settings.device_mcp_request_timeout_s,
        )

        async def _discover() -> None:
            defs = await discover_device_tools(
                transport, discovery_timeout=settings.device_mcp_discovery_timeout_s
            )
            if defs:
                session.add_tool_source(DeviceMcpToolSource(defs, transport))
                logger.info("device mcp: registered %d tool(s)", len(defs))

        discovery_task = asyncio.create_task(_discover())

    closing = False

    async def _watchdog() -> None:
        nonlocal closing
        while True:
            await asyncio.sleep(_IDLE_TICK_S)
            if session.is_turn_active():
                continue
            if time.monotonic() - last_activity >= idle:
                # Commit to closing synchronously, before the await below, so
                # the main loop cannot process/emit a message that raced in
                # concurrently with this goodbye send (ASGI sends aren't
                # mutually exclusive across concurrent awaits).
                closing = True
                try:
                    # Say a short farewell (in the bot's voice) before disconnecting.
                    # Paced in real time; give the device a moment to finish playing
                    # it out of its jitter buffer before the goodbye/close.
                    if settings.conversation_goodbye_text:
                        await session.speak(settings.conversation_goodbye_text)
                        await asyncio.sleep(0.5)
                    await websocket.send_json({"type": "goodbye", "reason": "idle_timeout"})
                except RuntimeError:
                    pass
                return

    # idle <= 0 means "never disconnect": skip scheduling the watchdog task
    # entirely rather than having it return immediately, since a completed
    # task would make `wd.done()` true on the very next loop check below and
    # tear down the connection mid-turn.
    wd = asyncio.create_task(_watchdog()) if idle > 0 else None
    try:
        while True:
            if wd is not None and wd.done():
                break
            recv = asyncio.create_task(websocket.receive())
            waitables = {recv, wd} if wd is not None else {recv}
            done, _pending = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
            if closing:
                # Watchdog has already committed to (or sent) goodbye; do not
                # process or emit on this connection even if `recv` also
                # completed concurrently with the goodbye send.
                recv.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recv
                break
            if wd is not None and wd in done:
                recv.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recv
                break
            message = recv.result()
            last_activity = time.monotonic()
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
                elif ctype == "mcp":
                    if transport is not None:
                        transport.on_message(control.get("payload") or {})
    except WebSocketDisconnect:
        pass
    finally:
        if wd is not None:
            wd.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wd
        if discovery_task is not None:
            discovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await discovery_task
        if transport is not None:
            transport.close()
        await session.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
