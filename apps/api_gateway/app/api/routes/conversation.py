import json
import logging
import uuid

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.actor import current_user_id
from app.core.auth_guard import resolve_ws_identity, ws_subprotocol
from app.core.errors import AppError
from app.core.identity_watch import build_identity_watchdog, receive_with_watchdog
from app.core.settings import settings
from app.services.conversation.responder import (
    build_responder,
    build_responder_ex,
    get_active_llm_api_key,
    get_active_llm_base_url,
    get_active_llm_model,
    reset_active_llm_config,
    resolve_llm_override_from_registry,
    set_active_llm_config,
)
from app.services.conversation.session import (
    ConversationSession,
    SessionRuntimeConfig,
    _build_tool_registry,
    _spawn_background,
)
from app.services.history.store import session_store
from app.services.memory.extractor import memory_extractor
from app.services.memory.retriever import inject_memories, memory_retriever
from app.services.profiles.store import profile_store
from app.services.stt.profile import resolve_stt
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.profile_store import tts_profile_store
from app.services.tts.service import tts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/conversation", tags=["conversation"])

# How often the disabled/revoked re-check wakes (test-tunable, same pattern as
# lugo.py's _IDLE_TICK_S).
_IDENTITY_RECHECK_INTERVAL_S = 30.0


def _truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)


class LlmConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


async def _llm_config_view() -> dict:
    """Current conversation LLM config (api key masked, never echoed back)."""
    responder = await build_responder()
    try:
        responder_name = responder.name
    finally:
        await responder.aclose()
    return {
        "base_url": await get_active_llm_base_url(),
        "model": await get_active_llm_model(),
        "api_key_set": bool(await get_active_llm_api_key()),
        "responder": responder_name,
    }


@router.get("/llm")
async def get_llm_config() -> dict:
    return {"success": True, "data": await _llm_config_view()}


@router.post("/llm")
async def set_llm_config(payload: LlmConfig) -> dict:
    """Point the conversation LLM at any OpenAI-compatible endpoint (online or local)."""
    await set_active_llm_config(payload.base_url, payload.api_key, payload.model)
    return {"success": True, "data": await _llm_config_view()}


@router.post("/llm/reset")
async def reset_llm_config() -> dict:
    """Turn off the conversation LLM (falls back to the built-in echo responder)."""
    await reset_active_llm_config()
    return {"success": True, "data": await _llm_config_view()}


@router.post("/chat")
async def chat(
    payload: ChatRequest, request: Request, profile: str | None = None, session_id: str | None = None
) -> dict:
    """Text chat with the configured conversation responder (LLM or echo)."""
    caller_id = current_user_id(request)
    active_profile = profile_store.get(profile) if profile else None
    llm_base_url = (active_profile.llm.base_url or None) if (active_profile and active_profile.llm.base_url) else None
    llm_api_key = active_profile.llm.api_key if (active_profile and active_profile.llm.base_url) else None
    llm_model = (active_profile.llm.model or None) if (active_profile and active_profile.llm.model) else None
    if active_profile and active_profile.llm.engine and active_profile.llm.model:
        registry_override = await resolve_llm_override_from_registry(
            active_profile.llm.engine, active_profile.llm.model
        )
        if registry_override:
            llm_base_url, llm_api_key = registry_override
            llm_model = active_profile.llm.model
    system_prompt = (active_profile.system_prompt or None) if (active_profile and active_profile.system_prompt) else None

    # Session: resume when session_id given (stored messages prefix the context).
    sid = session_id or str(uuid.uuid4())
    stored: list[dict] = []
    session_ready = True
    try:
        if session_id and await session_store.exists(session_id):
            stored = await session_store.get_messages(session_id)
        elif not await session_store.exists(sid):
            await session_store.create(sid, profile_id=profile or "", user_id=active_profile.owner_id if active_profile else None)
    except Exception as exc:  # noqa: BLE001 - session setup must not block the reply
        logger.warning("session setup failed for %s: %s", sid, exc)
        stored = []
        session_ready = False

    new_msgs = [{"role": m.role, "content": m.content} for m in payload.messages]
    history = [{"role": m["role"], "content": m["content"]} for m in stored] + new_msgs

    # Memory injection: prepend the profile's memories to the system prompt.
    last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    try:
        block = await memory_retriever.get_context(active_profile, query=last_user, user_id=caller_id)
    except Exception as exc:  # noqa: BLE001 - memory retrieval must not block the reply
        logger.warning("memory retrieval failed for %s: %s", sid, exc)
        block = ""
    system_prompt = (
        inject_memories(
            system_prompt or system_config_store.get().conversation.conversation_system_prompt, block
        )
        if block
        else system_prompt
    )

    responder = await build_responder_ex(
        base_url=llm_base_url,
        api_key=llm_api_key,
        model=llm_model,
        system_prompt=system_prompt,
        voice_optimized=bool(active_profile and active_profile.voice_optimized),
    )
    tool_registry = await _build_tool_registry(active_profile)
    try:
        if tool_registry:
            parts = [
                chunk
                async for chunk in responder.reply_stream(
                    history, registry=tool_registry, max_iters=settings.conversation_tool_max_iters
                )
            ]
            reply = " ".join(parts).strip()
        else:
            reply = await responder.reply(history)
    finally:
        await responder.aclose()

    if session_ready:
        try:
            turn = (len(stored) // 2) + 1
            for m in new_msgs:
                await session_store.append_message(sid, turn, m["role"], m["content"])
            await session_store.append_message(sid, turn, "assistant", reply)
            if active_profile and active_profile.memory.enabled and active_profile.llm.base_url:
                _spawn_background(memory_extractor.extract_and_upsert(sid, active_profile, user_id=caller_id))
        except Exception as exc:  # noqa: BLE001 - persistence must not fail a successful reply
            logger.warning("chat persistence failed for %s: %s", sid, exc)

    return {
        "success": True,
        "data": {
            "reply": reply,
            "responder": responder.name,
            "model": await get_active_llm_model(),
            "profile": profile,
            "session_id": sid,
        },
    }


@router.websocket("/stream")
async def conversation_stream(websocket: WebSocket) -> None:
    identity = await resolve_ws_identity(websocket)
    if identity is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept(subprotocol=ws_subprotocol(websocket))
    requested_sid = websocket.query_params.get("session_id")
    session_id = requested_sid or str(uuid.uuid4())
    q = websocket.query_params

    # --- Profile resolution ---
    profile_name = q.get("profile")
    profile = profile_store.get(profile_name) if profile_name else None
    if profile_name and not profile:
        await websocket.send_json({
            "event": "warning",
            "message": f"profile '{profile_name}' not found, using defaults",
        })

    # STT engine + language: explicit query param > the active profile's STT config
    # > server default. Devices can connect with just ?profile=<name> and inherit
    # the profile's STT, exactly as LLM/TTS already resolve. (See resolve_stt.)
    stt_engine, language, stt_model = resolve_stt(
        profile, q.get("stt_engine"), q.get("language"), q.get("stt_model")
    )
    # TTS profile resolution: ?tts_profile= (explicit pin) > the active LLM
    # profile's linked TTS profile > legacy tts_engine/voice query params.
    tts_profile_name = q.get("tts_profile") or (profile.tts.profile_name if profile else "") or None
    tts_profile = tts_profile_store.get(tts_profile_name) if tts_profile_name else None
    if tts_profile and tts_profile.engine:
        tts_engine = tts_profile.engine
        voice = tts_profile.voice or q.get("voice") or None
        ref_audio_path = tts_profile.ref_audio_path or None
        ref_text = tts_profile.ref_text or None
        tts_instruct = tts_profile.instruct or None
        tts_speed = tts_profile.speed
        tts_language = tts_profile.language
    else:
        conv_cfg = system_config_store.get().conversation
        tts_engine = (
            q.get("tts_engine")
            or conv_cfg.conversation_tts_engine
            or system_config_store.get().engines.default_tts_engine
        )
        voice = q.get("voice") or None
        ref_audio_path = ref_text = tts_instruct = None
        tts_speed = tts_language = None
    sample_rate = int(q.get("sample_rate", system_config_store.get().stt_local.stt_stream_sample_rate))
    # Audio transport: pcm16 (default) or opus (embedded ESP32/RPi + browser WebCodecs;
    # ~10x less bandwidth). Server decodes Opus packets -> PCM16 for the endpointer.
    audio_codec = (q.get("audio_codec") or "pcm16").lower()
    # Output modalities the client wants back (text/audio). Enables the full matrix:
    # audio→audio, text→audio, audio→text, text→text. Input is audio frames OR a
    # {"type":"text"} control message.
    out_modalities = {m.strip() for m in (q.get("output") or "audio,text").lower().split(",") if m.strip()}
    want_audio = "audio" in out_modalities
    want_text = "text" in out_modalities
    # How reply audio is delivered: "url" (browser fetches /artifacts) or "opus"/"pcm"
    # binary frames pushed over the socket (for ESP32/RPi that can't fetch mid-stream).
    audio_out = (q.get("audio_out") or "url").lower()
    output_sample_rate = int(q.get("output_sample_rate", 24000))
    # Only optional noise reduction here — the endpointer already does VAD
    # segmentation and Whisper has its own vad_filter, so an extra VAD gate on the
    # utterance would clip speech and hurt recognition.
    denoise = _truthy(q.get("denoise"), system_config_store.get().preprocessing.stt_noise_reduce_enabled)

    try:
        stt_service.get_provider(stt_engine)
        tts_service.get_provider(tts_engine)
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return

    cfg = SessionRuntimeConfig(
        session_id=session_id, profile_name=profile_name, stt_engine=stt_engine,
        language=language, tts_engine=tts_engine, voice=voice,
        ref_audio_path=ref_audio_path, ref_text=ref_text, tts_instruct=tts_instruct,
        tts_speed=tts_speed, tts_language=tts_language, sample_rate=sample_rate,
        output_sample_rate=output_sample_rate, audio_codec=audio_codec,
        want_audio=want_audio, want_text=want_text, audio_out=audio_out,
        denoise=denoise, resume_sid=requested_sid, stt_model=stt_model,
        identity_user_id=identity.user_id,
    )

    async def emit(event: str, **payload) -> None:
        await websocket.send_json({"event": event, **payload})

    async def emit_audio(packet: bytes) -> None:
        await websocket.send_bytes(packet)

    session = ConversationSession(cfg, emit, emit_audio)
    watchdog = build_identity_watchdog(identity, interval_s=_IDENTITY_RECHECK_INTERVAL_S)
    if watchdog is not None:
        watchdog.start()
    try:
        await session.start()
        async for message in receive_with_watchdog(websocket, watchdog):
            if message is None:
                await websocket.close(code=4401, reason="account disabled")
                break
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
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
                    await session.abort("user")
                elif ctype == "reset":
                    await session.reset()
                elif ctype in {"flush", "end"}:
                    await session.flush()
                    if ctype == "end":
                        await emit("done")
                        break
    except WebSocketDisconnect:
        pass
    finally:
        if watchdog is not None:
            watchdog.cancel()
        await session.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
