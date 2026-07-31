import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.api.routes.sessions import _scope_user_id
from app.core.actor import current_role, current_user_id
from app.core.auth_guard import (
    resolve_ws_identity,
    ws_session_owner_denied,
    ws_subprotocol,
)
from app.core.errors import AppError
from app.core.identity_watch import build_identity_watchdog, receive_with_watchdog
from app.core.settings import settings
from app.schemas.conversation import ChatRequest, LlmConfig
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
from app.services.conversation.turn_quota import llm_turn_quota_blocked
from app.services.health import check_resolved_engines
from app.services.history.store import session_store
from app.services.memory.extractor import memory_extractor
from app.services.memory.retriever import inject_memories, memory_retriever
from app.services.auth.device_profile import resolve_bound_profile
from app.services.profile_visibility import visible_profile_or_none, visible_tts_profile_or_none
from app.services.profiles.store import profile_store
from app.services.stt.profile import resolve_stt
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.profile_store import tts_profile_store
from app.services.tts.service import tts_service
from app.services.usage.attribution import resolve_llm_pair
from app.services.usage.recorder import record_usage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/conversation", tags=["conversation"])

# How often the disabled/revoked re-check wakes (test-tunable, same pattern as
# lugo.py's _IDLE_TICK_S).
_IDENTITY_RECHECK_INTERVAL_S = 30.0


def _truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _require_admin(request: Request) -> None:
    """The /llm routes below mutate the Model Registry's server-wide default
    LLM row (set_active_llm_config), despite living under the /v1/conversation
    USER prefix. Any logged-in user hitting them today can repoint every other
    user's LLM at an attacker endpoint, or drop the whole server to the echo
    responder. Inline check here (rather than moving these three routes to an
    admin-only prefix) so the public path stays exactly what
    static/js/model-recommender.js already calls."""
    if current_role(request) != "admin":
        raise HTTPException(status_code=403, detail="admin only")


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
async def get_llm_config(request: Request) -> dict:
    _require_admin(request)
    return {"success": True, "data": await _llm_config_view()}


@router.post("/llm")
async def set_llm_config(payload: LlmConfig, request: Request) -> dict:
    """Point the conversation LLM at any OpenAI-compatible endpoint (online or local)."""
    _require_admin(request)
    await set_active_llm_config(payload.base_url, payload.api_key, payload.model)
    return {"success": True, "data": await _llm_config_view()}


@router.post("/llm/reset")
async def reset_llm_config(request: Request) -> dict:
    """Turn off the conversation LLM (falls back to the built-in echo responder)."""
    _require_admin(request)
    await reset_active_llm_config()
    return {"success": True, "data": await _llm_config_view()}


@router.post("/chat")
async def chat(
    payload: ChatRequest, request: Request, profile: str | None = None, session_id: str | None = None
) -> dict:
    """Text chat with the configured conversation responder (LLM or echo)."""
    caller_id = current_user_id(request)

    # Ownership check on an explicit ?session_id=: without this, any logged-in
    # user could resume (read AND corrupt) another user's EXISTING session by
    # guessing or brute-forcing its id -- session_store.exists()/.get_messages()
    # below don't check who owns it. Same 404-on-mismatch rule as sessions.py's
    # get_session (a non-owner can't distinguish "not yours" from "doesn't
    # exist"); scope is None for admins (and for the dev-mode/no-auth fallback
    # current_role() already applies), so they still see everything. A
    # session_id that doesn't exist yet isn't an IDOR (there's nothing to
    # read) and falls through to the existing create-on-first-use path below.
    if session_id:
        existing_sess = await session_store.get(session_id)
        if existing_sess:
            scope = _scope_user_id(request)
            if scope is not None and existing_sess.get("user_id") != scope:
                raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    # C2 fix: visible_profile_or_none() collapses "doesn't exist" and "exists
    # but belongs to someone else" to the same None -- caller must never run
    # on another user's llm.api_key/system_prompt/mcp_servers (see
    # docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md).
    active_profile = visible_profile_or_none(profile_store.get(profile) if profile else None, caller_id)
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

    # Quota pre-flight: block BEFORE the responder does any work. Shared with
    # livehost's/session.py's own preflight (Task 6 dedup) -- see
    # turn_quota.py's docstring for the pairing rule (same resolver the
    # record_usage calls below use) and fail-open contract. The responder
    # doesn't exist yet, so this pre-flight only knows the profile.
    blocked, quota_message = await llm_turn_quota_blocked(
        identity_user_id=caller_id, profile=active_profile, profile_name=profile or "",
    )
    if blocked:
        raise HTTPException(status_code=429, detail=quota_message)

    # Session: resume when session_id given (stored messages prefix the context).
    sid = session_id or str(uuid.uuid4())
    stored: list[dict] = []
    session_ready = True
    try:
        if session_id and await session_store.exists(session_id):
            stored = await session_store.get_messages(session_id)
        elif not await session_store.exists(sid):
            # Always the caller -- never a fallback to the named profile's
            # owner (H2: `caller_id or profile.owner_id` let an unauthenticated
            # caller create a row attributed to whatever profile owner they
            # named, an attacker-chosen victim). `caller_id` is None only for
            # the dev-mode/no-auth caller or an unauthenticated device -- those
            # rows are created ownerless by construction, same as the pre-fix
            # legacy rows; they are intentionally admin-only to resume
            # (sessions.py's get_session), since there is no real owner to
            # derive.
            await session_store.create(
                sid, profile_id=profile or "",
                user_id=caller_id,
            )
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
            try:
                last_usage = getattr(responder, "last_usage", None) or {}
                prompt_tokens = last_usage.get("prompt_tokens")
                completion_tokens = last_usage.get("completion_tokens")
                native_amount = (prompt_tokens or 0) + (completion_tokens or 0)
                usage_engine, usage_model_id = resolve_llm_pair(
                    responder,
                    (active_profile.llm.engine if active_profile else "") or "",
                    (active_profile.llm.model if active_profile else "") or "",
                )
                await record_usage(
                    user_id=caller_id or "", profile_id=profile or "",
                    kind="llm", engine=usage_engine, model_id=usage_model_id, unit="tokens",
                    native_amount=native_amount, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - metering must never break the reply
                logger.warning("chat usage metering failed: %s", exc)
        else:
            reply = await responder.reply(history)
            try:
                last_usage = getattr(responder, "last_usage", None) or {}
                prompt_tokens = last_usage.get("prompt_tokens")
                completion_tokens = last_usage.get("completion_tokens")
                native_amount = (prompt_tokens or 0) + (completion_tokens or 0)
                usage_engine, usage_model_id = resolve_llm_pair(
                    responder,
                    (active_profile.llm.engine if active_profile else "") or "",
                    (active_profile.llm.model if active_profile else "") or "",
                )
                await record_usage(
                    user_id=caller_id or "", profile_id=profile or "",
                    kind="llm", engine=usage_engine, model_id=usage_model_id, unit="tokens",
                    native_amount=native_amount, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - metering must never break the reply
                logger.warning("chat usage metering failed: %s", exc)
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
    # Same IDOR the HTTP /chat route guards against: `requested_sid` flows
    # into `resume_sid` below and is consumed by ConversationSession.start()
    # (services/conversation/session.py) with no ownership check, so anyone
    # who could guess or observe another user's session id could resume
    # (read + corrupt) their private conversation over the WS path too.
    # Checked before resume_sid is used for anything.
    if requested_sid and await ws_session_owner_denied(requested_sid, identity):
        await websocket.send_json({
            "event": "error",
            "message": f"Session '{requested_sid}' not found",
        })
        await websocket.close()
        return
    session_id = requested_sid or str(uuid.uuid4())
    q = websocket.query_params

    # --- Profile resolution ---
    profile_name = q.get("profile")
    # A paired device's server-side binding outranks whatever profile its own
    # config asked for, so the control panel is the single source of truth. An
    # unbound device keeps using its own `?profile=` -- that's what stops this
    # from breaking fleets deployed before bindings existed. The override is
    # announced rather than silent: stale firmware config should be visible.
    profile_name, binding_warning, _from_binding = await resolve_bound_profile(
        identity, profile_name
    )
    if binding_warning:
        await websocket.send_json({"event": "warning", "message": binding_warning})
    # C2 fix: same rule as HTTP /chat above -- "exists but not yours" must
    # fall into the exact same not-found warning as "doesn't exist" (no new
    # enumeration oracle). bypass=identity.unauthenticated preserves the
    # pre-existing dev-mode fallback (settings.auth_enabled False -- see
    # WsIdentity.unauthenticated's docstring in auth_guard.py and
    # ws_session_owner_denied, which applies the identical bypass).
    profile = visible_profile_or_none(
        profile_store.get(profile_name) if profile_name else None,
        identity.user_id,
        bypass=identity.unauthenticated,
    )
    if profile_name and not profile:
        await websocket.send_json({
            "event": "warning",
            "message": f"profile '{profile_name}' not found, using defaults",
        })

    # STT engine + language: the active profile's STT config > server default.
    # Devices can connect with just ?profile=<name> and inherit the profile's
    # STT, exactly as LLM/TTS already resolve. (See resolve_stt.)
    stt_engine, language, stt_model = resolve_stt(
        profile, q.get("language"), q.get("stt_model")
    )
    # TTS profile resolution: ?tts_profile= (explicit pin) > the active LLM
    # profile's linked TTS profile > server default.
    tts_profile_name = q.get("tts_profile") or (profile.tts.profile_name if profile else "") or None
    # C2 fix: identical rule for ?tts_profile= (and the profile's own linked
    # tts profile_name, in case it points at a row the caller can't see).
    tts_profile = visible_tts_profile_or_none(
        tts_profile_store.get(tts_profile_name) if tts_profile_name else None,
        identity.user_id,
        bypass=identity.unauthenticated,
    )
    if tts_profile and tts_profile.engine:
        tts_engine = tts_profile.engine
        tts_model = tts_profile.model_id or ""
        voice = tts_profile.voice or q.get("voice") or None
        ref_audio_path = tts_profile.ref_audio_path or None
        ref_text = tts_profile.ref_text or None
        tts_instruct = tts_profile.instruct or None
        tts_speed = tts_profile.speed
        tts_language = tts_profile.language
    else:
        tts_engine = system_config_store.get().engines.default_tts_engine
        tts_model = q.get("tts_model") or ""
        voice = q.get("voice") or None
        ref_audio_path = ref_text = tts_instruct = None
        tts_speed = tts_language = None
    sample_rate = int(q.get("sample_rate", settings.stt_stream_sample_rate))
    # Audio transport: pcm16 (default) or opus (embedded ESP32/RPi + browser WebCodecs;
    # ~10x less bandwidth). Server decodes Opus packets -> PCM16 for the endpointer.
    audio_codec = (q.get("audio_codec") or "pcm16").lower()
    # Output modalities the client wants back (text/audio). Enables the full matrix:
    # audio→audio, text→audio, audio→text, text→text. Input is audio frames OR a
    # {"type":"text"} control message.
    out_modalities = {m.strip() for m in (q.get("output") or "audio,text").lower().split(",") if m.strip()}
    want_audio = "audio" in out_modalities
    want_text = "text" in out_modalities
    # How reply audio is delivered: "wav" (one complete container per sentence,
    # pushed as a binary frame) or "opus" (60ms frames, ~10x less bandwidth --
    # ESP32/RPi and WebCodecs browsers). Nothing is ever written to disk.
    audio_out = (q.get("audio_out") or "wav").lower()
    if audio_out != "opus":
        audio_out = "wav"
    output_sample_rate = int(q.get("output_sample_rate", 24000))
    # Per-connection override of Opus playback pacing (None = inherit the
    # global system_config default -- what api/routes/lugo.py always gets).
    # Web sends opus_pace=0: see
    # docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md.
    _opus_pace_raw = q.get("opus_pace")
    opus_pace = _truthy(_opus_pace_raw, True) if _opus_pace_raw is not None else None
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

    # Fail fast on a dead engine here rather than after the user has already
    # spoken an utterance and the first transcribe/synthesize call blows up.
    stt_health, tts_health = await check_resolved_engines(
        stt_engine, stt_model, tts_engine, tts_model
    )
    # STT always matters (every session transcribes). TTS only matters when the
    # client actually requested audio output -- a text-only connect (?output=text,
    # e.g. the voice-to-text chat UI) never calls into TTS at all
    # (services/conversation/session.py short-circuits synthesis when want_audio
    # is False), so refusing it over a dead/misconfigured TTS engine would be a
    # regression of currently-shipped behavior.
    for health, required in ((stt_health, True), (tts_health, want_audio)):
        if required and health.blocks_session:
            await websocket.send_json({
                "event": "error",
                "message": f"{health.engine} is unavailable: {health.detail}",
            })
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
        # One web thread per person: every browser they use continues the same
        # conversation, and none of them adopts the speaker's. A per-browser id can
        # replace this later without touching the schema.
        source="web" if identity.user_id else "",
        client_id=identity.user_id or "",
        tts_model=tts_model,
        identity_user_id=identity.user_id,
        identity_unauthenticated=identity.unauthenticated,
        opus_pace=opus_pace,
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
                elif ctype == "new_session":
                    # Distinct from `reset` on purpose: reset clears the context
                    # but keeps writing to the same stored session, this starts a
                    # genuinely new conversation. See ConversationSession.rotate.
                    # A turn still in flight finishes first (request_rotate);
                    # clients that want it cut short send `abort` beforehand.
                    await session.request_rotate("client")
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
