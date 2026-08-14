import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.api.routes.sessions import _scope_user_id
from app.core.actor import current_user_id, require_admin
from app.core.auth_guard import (
    resolve_ws_identity,
    ws_session_owner_denied,
    ws_subprotocol,
)
from app.core.audio import parse_sample_rate
from app.core.errors import AppError
from app.core.opus import ensure_opus_rate
from app.core.params import parse_bool_or
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
    set_active_llm_config,
)
from app.services.conversation.llm_config import resolve_llm_config
from app.services.conversation.session import (
    ConversationSession,
    SessionRuntimeConfig,
    _build_tool_registry,
    _spawn_background,
    _tail,
)
from app.services.conversation.tts_params import TtsParams, tts_params_from_profile
from app.services.conversation.turn_quota import llm_turn_quota_blocked
from app.services.conversation.turn_usage import record_llm_turn_usage
from app.services.health import check_resolved_engines
from app.services.history.store import session_store
from app.services.memory.extractor import memory_extractor
from app.services.memory.retriever import inject_memories, memory_retriever
from app.services.auth.device_profile import resolve_bound_profile
from app.services.profile_visibility import (
    is_shared_template,
    usable_profile_or_none,
    visible_tts_profile_or_none,
)
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


# Shared with stt.py's socket -- body in core/params.py.
_truthy = parse_bool_or


# The /llm routes below mutate the Model Registry's server-wide default LLM row
# (set_active_llm_config), despite living under the /v1/conversation USER
# prefix. Any logged-in user hitting them before this gate could repoint every
# other user's LLM at an attacker endpoint, or drop the whole server to the echo
# responder. Inline check here (rather than moving these three routes to an
# admin-only prefix) so the public path stays exactly what
# static/js/model-recommender.js already calls. Alias, not a copy: same gate
# mcp.py's write surface uses.
_require_admin = require_admin


async def _cross_profile_resume_message(session_id: str | None, profile_name: str | None) -> str | None:
    """Why `session_id` must not be resumed under `profile_name`, or None when
    resuming is fine.

    A session keeps the `profile_id`, `source` and `client_id` it was created
    with -- resuming never rewrites them. Appending a conversation to a session
    created under a different profile therefore files those turns under an
    assistant the caller isn't looking at, and History reads per assistant
    (`GET /v1/sessions?profile=...`), so they vanish from view. That is exactly
    what happened on 2026-08-02: a browser talking as `dev-copy` was handed the
    id of an ESP32 session under `esp32-assistant` (same user, so the ownership
    check passed) and its turns landed in the speaker's transcript.

    An id that doesn't exist yet is not a mismatch -- that's the ordinary
    create-on-first-use path, not a resume.
    """
    if not session_id:
        return None
    stored = await session_store.get(session_id)
    if not stored:
        return None
    stored_profile = stored.get("profile_id") or ""
    if stored_profile == (profile_name or ""):
        return None
    return (
        f"session '{session_id}' belongs to profile '{stored_profile or '(none)'}', "
        f"not '{profile_name or '(none)'}' — starting a new session instead so this "
        "conversation is filed under the profile you asked for."
    )


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

    # C2 fix: usable_profile_or_none() collapses "doesn't exist" and "exists
    # but belongs to someone else" to the same None -- caller must never run
    # on another user's llm.api_key/system_prompt/mcp_servers (see
    # docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md). It also
    # excludes shared templates, which nobody runs on (2026-08-14 design).
    active_profile = usable_profile_or_none(profile_store.get(profile) if profile else None, caller_id)
    # Same precedence the WS paths apply -- services/conversation/llm_config.py.
    llm_base_url, llm_api_key, llm_model, system_prompt = await resolve_llm_config(active_profile)

    # Quota pre-flight: block BEFORE the responder does any work. Shared with
    # session.py's own preflight (Task 6 dedup) -- the livehost plugin's
    # turns go through that same preflight now, over /v1/conversation/stream
    # -- see turn_quota.py's docstring for the pairing rule (same resolver
    # the record_usage calls below use) and fail-open contract. The
    # responder doesn't exist yet, so this pre-flight only knows the profile.
    blocked, quota_message = await llm_turn_quota_blocked(
        identity_user_id=caller_id, profile=active_profile, profile_name=profile or "",
    )
    if blocked:
        raise HTTPException(status_code=429, detail=quota_message)

    # Cross-profile resume: drop the id rather than append this turn to another
    # assistant's history -- see _cross_profile_resume_message. The caller learns
    # about it from the session_id in the response, which is the new one.
    mismatch = await _cross_profile_resume_message(session_id, profile)
    if mismatch:
        logger.info("chat: %s", mismatch)
        session_id = None

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
    # Capped by the same conversation_history_max_messages the WS path applies
    # (_tail). Resuming a long-lived session used to replay its ENTIRE stored
    # transcript on every request: cost per request grew without bound and a
    # session old enough eventually just overflowed the context window. The full
    # transcript stays in the DB for History either way.
    history = _tail([{"role": m["role"], "content": m["content"]} for m in stored] + new_msgs)

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
        # Same best-effort "last_usage -> resolve_llm_pair -> record_usage" row
        # the WS paths write, via the helper that already exists for it
        # (services/conversation/turn_usage.py). Both branches above meter
        # identically, so it sits after the if/else rather than inside each --
        # and `llm_model` is deliberately NOT passed: the pinned model here has
        # always been read straight off the profile.
        await record_llm_turn_usage(
            responder, identity_user_id=caller_id, profile=active_profile, profile_name=profile,
        )
    finally:
        await responder.aclose()

    if session_ready:
        try:
            # From the stored turn numbers, not `len(stored) // 2`: that assumed
            # every turn contributes exactly two messages, so a single turn that
            # stored only a user message (an empty/failed reply) shifted the
            # count by half and made the next turn collide with the previous one.
            turn = max((m.get("turn") or 0) for m in stored) + 1 if stored else 1
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
    profile_name, binding_warning, _from_binding, hard_denied = await resolve_bound_profile(
        identity, profile_name
    )
    if hard_denied:
        # Ordinary close, NOT 4401/403 -- see lugo.py's identical gate for why:
        # the token is valid, only the profile assignment is missing.
        await websocket.send_json({
            "event": "error",
            "message": "this device is not assigned to a profile; assign one in the admin console",
        })
        await websocket.close()
        return
    if binding_warning:
        await websocket.send_json({"event": "warning", "message": binding_warning})
    # C2 fix: same rule as HTTP /chat above -- "exists but not yours" must
    # fall into the exact same not-found warning as "doesn't exist" (no new
    # enumeration oracle). bypass=identity.unauthenticated preserves the
    # pre-existing dev-mode fallback (settings.auth_enabled False -- see
    # WsIdentity.unauthenticated's docstring in auth_guard.py and
    # ws_session_owner_denied, which applies the identical bypass).
    requested_row = profile_store.get(profile_name) if profile_name else None
    profile = usable_profile_or_none(
        requested_row,
        identity.user_id,
        bypass=identity.unauthenticated,
    )
    if profile_name and not profile:
        await websocket.send_json({
            "event": "warning",
            "message": (
                f"profile '{profile_name}' is a shared template; clone it before using it"
                if is_shared_template(requested_row)
                else f"profile '{profile_name}' not found, using defaults"
            ),
        })

    # Cross-profile resume: checked here rather than next to the ownership check
    # above because the profile isn't final until resolve_bound_profile() has had
    # its say (a paired device's binding overrides ?profile=). Announced, not
    # silent -- a client that resumes the wrong session should be able to see it
    # did. See _cross_profile_resume_message.
    mismatch = await _cross_profile_resume_message(requested_sid, profile_name)
    if mismatch:
        await websocket.send_json({"event": "warning", "message": mismatch})
        requested_sid = None
        session_id = str(uuid.uuid4())

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
    # Shared with lugo.py (and the livehost plugin's own traffic, which
    # reaches this same route) -- see services/conversation/tts_params.py.
    # The fallback on the right of `or` is only built when the profile pins no
    # engine, so the config store is still read exactly as lazily as before.
    (
        tts_engine, tts_model, voice, ref_audio_path,
        ref_text, tts_instruct, tts_speed, tts_language,
    ) = tts_params_from_profile(tts_profile, fallback_voice=q.get("voice")) or TtsParams(
        engine=system_config_store.get().engines.default_tts_engine,
        model_id=q.get("tts_model") or "", voice=q.get("voice") or None,
        ref_audio_path=None, ref_text=None, instruct=None, speed=None, language=None,
    )
    # Refused rather than cast blindly: sample_rate feeds VadEndpointer, which
    # divides by it (?sample_rate=0 was a ZeroDivisionError inside the handler,
    # after accept()), and a non-numeric value was a bare ValueError. Same
    # contract routes/stt.py already enforced on its own socket.
    try:
        sample_rate = parse_sample_rate(
            q.get("sample_rate"), settings.stt_stream_sample_rate
        )
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return
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
    try:
        output_sample_rate = parse_sample_rate(
            q.get("output_sample_rate"), 24000, name="output_sample_rate"
        )
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return
    # parse_sample_rate above only bounds the value to a plausible range, which
    # is right for the PCM/WAV paths. Opus is stricter: libopus accepts exactly
    # OPUS_SAMPLE_RATES and its encoder/decoder constructors RAISE on anything
    # else -- from inside session.start(), after accept(), with no error on the
    # wire. Checked here, per direction, only when that direction is Opus, via
    # the same membership rule + message api/routes/lugo.py gets through
    # parse_opus_sample_rate.
    try:
        if audio_codec == "opus":
            ensure_opus_rate(sample_rate, name="sample_rate")
        if want_audio and audio_out == "opus":
            ensure_opus_rate(output_sample_rate, name="output_sample_rate")
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return
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

    # Do not tag the session/memories with a shared template's name: it can
    # never run (profile_usable() forbids it), so `profile` above already
    # resolved to None and this turn is running on server defaults. Passing
    # the raw name through here would still record sessions/memories under
    # that template's name, orphaning them (the template can't run, and a
    # clone gets a different name). Same is_shared_template(requested_row)
    # check the warning branch above already uses. An unresolvable-for-any-
    # other-reason name (typo, someone else's private profile) is left as-is
    # -- that shape predates this feature.
    session_profile_name = None if is_shared_template(requested_row) else profile_name
    cfg = SessionRuntimeConfig(
        session_id=session_id, profile_name=session_profile_name, stt_engine=stt_engine,
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
        # Caller-supplied persona for this session only -- see
        # SessionRuntimeConfig.persona_override. Only affects the caller's
        # own conversation; base_context (admin guardrails) still applies on
        # top regardless.
        persona_override=q.get("system_prompt") or None,
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
