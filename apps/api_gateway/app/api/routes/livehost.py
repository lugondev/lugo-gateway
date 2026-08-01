import asyncio
import json
import logging
import uuid
from contextlib import aclosing

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.core.actor import scope_user_id
from app.core.audio import (
    parse_sample_rate,
    pcm16_to_wav_bytes,
    wav_bytes_to_pcm16,
    wav_duration_seconds,
)
from app.core.auth_guard import resolve_ws_identity, ws_session_owner_denied, ws_subprotocol
from app.core.errors import AppError
from app.core.identity_watch import build_identity_watchdog, receive_with_watchdog
from app.core.settings import settings
from app.schemas.livehost import TikTokConnectRequest
from app.services.conversation.endpointer import build_endpointer
from app.services.conversation.responder import build_responder_ex
from app.services.conversation.turn_quota import llm_turn_quota_blocked_for_pins
from app.services.conversation.llm_config import resolve_llm_config
from app.services.conversation.tts_params import TtsParams, tts_params_from_profile
from app.services.conversation.turn_tts import build_tts_request_or_degrade
from app.services.conversation.turn_usage import record_llm_turn_usage
from app.services.history.store import session_store
from app.services.livehost.ingestor import TikTokLiveIngestor
from app.services.livehost.orchestrator import LiveHostOrchestrator
from app.services.livehost.registry import LivehostSession, livehost_registry
from app.services.livehost.scheduler import EventScheduler
from app.services.profile_visibility import visible_profile_or_none, visible_tts_profile_or_none
from app.services.profiles.store import profile_store
from app.services.quota.gate import quota_gate
from app.services.stt.model_catalog import resolve_default_stt_model
from app.services.stt.profile import resolve_stt
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.profile_store import tts_profile_store
from app.services.tts.service import tts_service
from app.services.tts.streaming import pacing_delays, prefetch_synthesis
from app.services.usage.recorder import record_usage
from app.services.warmup import is_ready, warm_providers

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/livehost", tags=["livehost"])


async def _quota_blocked_for(
    *, user_id: str, profile_name: str, pinned_engine: str, pinned_model: str
) -> tuple[bool, str]:
    """(blocked, message) for one livehost turn.

    `pinned_engine`/`pinned_model` are the PROFILE's pins, not a resolved pair:
    the pairing rule below has to see whether a model was pinned at all, so
    resolving before the call would hide exactly the fact it needs.

    Thin wrapper (Task 6 dedup) over the shared
    services/conversation/turn_quota helper -- kept here, with this exact
    signature, because test_livehost_quota_gate.py drives it directly and
    monkeypatches THIS module's `quota_gate` name. Passing `quota_gate=`
    below is a late-bound reference to livehost.py's own module global, so
    that reassignment is still honored (see turn_quota.py's docstring).
    """
    return await llm_turn_quota_blocked_for_pins(
        user_id=user_id, profile_name=profile_name,
        pinned_engine=pinned_engine, pinned_model=pinned_model,
        quota_gate=quota_gate,
    )


# How often the disabled/revoked re-check wakes (test-tunable, same pattern as
# lugo.py's _IDLE_TICK_S).
_IDENTITY_RECHECK_INTERVAL_S = 30.0


def _mention_keywords() -> list[str]:
    return [k.strip() for k in settings.livehost_mention_keywords.split(",") if k.strip()]


# Same helper sessions.py exposes under this name; body lives in core/actor.py.
# Kept as a module-level alias so tests can monkeypatch it per-route.
_scope_user_id = scope_user_id


def _get_owned_session(session_id: str, request: Request) -> LivehostSession:
    """H5: connect/disconnect/status previously did only
    `livehost_registry.get(session_id)`, with no owner check at all -- any
    logged-in user could drive/stop/inspect another user's live TikTok
    session by id. 404s uniformly for "doesn't exist" and "exists but isn't
    yours", same as get_session/_scope_user_id in sessions.py, so this isn't
    a new existence oracle."""
    session = livehost_registry.get(session_id)
    scope = _scope_user_id(request)
    if session is None or (scope is not None and session.user_id != scope):
        raise HTTPException(status_code=404, detail=f"livehost session '{session_id}' not found")
    return session


@router.post("/{session_id}/connect")
async def connect_tiktok(session_id: str, payload: TikTokConnectRequest, request: Request) -> dict:
    session = _get_owned_session(session_id, request)
    await session.ingestor.start(payload.unique_id)
    return {"success": True, "data": {"state": session.ingestor.state.value, "unique_id": payload.unique_id}}


@router.post("/{session_id}/disconnect")
async def disconnect_tiktok(session_id: str, request: Request) -> dict:
    session = _get_owned_session(session_id, request)
    await session.ingestor.stop()
    return {"success": True, "data": {"state": session.ingestor.state.value}}


@router.get("/{session_id}/status")
async def livehost_status(session_id: str, request: Request) -> dict:
    session = _get_owned_session(session_id, request)
    return {
        "success": True,
        "data": {
            "state": session.ingestor.state.value,
            "unique_id": session.ingestor.unique_id,
            "pending_social_events": session.scheduler.pending_count(),
        },
    }


@router.websocket("/stream")
async def livehost_stream(websocket: WebSocket) -> None:
    identity = await resolve_ws_identity(websocket)
    if identity is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept(subprotocol=ws_subprotocol(websocket))
    session_id = websocket.query_params.get("session_id") or str(uuid.uuid4())
    # H5: mirrors conversation.py's/lugo.py's WS resume-ownership check.
    # Without this, a caller-supplied ?session_id= flowed straight into
    # livehost_registry.register() below, which unconditionally OVERWRITES
    # any existing entry for that key -- so any logged-in user could hijack
    # (overwrite the ingestor/scheduler of) another user's already-live
    # session, and unregister() on close would then orphan it. Checked
    # before session_id is used for anything. session_store (consulted by
    # ws_session_owner_denied) is the shared ownership source of truth this
    # route's own session_store.create() call below populates for a
    # legitimate first connect under this id.
    if await ws_session_owner_denied(session_id, identity):
        # livehost's own wire error shape ({"event": "error", ...}), same as
        # every other error send in this handler (see the AppError except
        # branch below) -- NOT lugo.py's {"type": "error", ...}.
        await websocket.send_json({
            "event": "error",
            "message": f"Session '{session_id}' not found",
        })
        await websocket.close()
        return
    q = websocket.query_params

    profile_name = q.get("profile")
    # C2 fix: same rule as conversation.py -- "exists but not yours" must
    # behave identically to "doesn't exist" (silent fallback to defaults,
    # same as today's not-found path -- no distinct warning here to leak).
    profile = visible_profile_or_none(
        profile_store.get(profile_name) if profile_name else None,
        identity.user_id,
        bypass=identity.unauthenticated,
    )
    # Same precedence conversation.py/session.py apply -- llm_config.py.
    llm_base_url, llm_api_key, llm_model, system_prompt = await resolve_llm_config(profile)

    # STT resolves from the profile (else server default), same as TTS/LLM above.
    stt_engine, language, stt_model = resolve_stt(
        profile, q.get("language"), q.get("stt_model")
    )
    # TTS profile resolution: ?tts_profile= (explicit pin) > the active LLM
    # profile's linked TTS profile > server default.
    tts_profile_name = q.get("tts_profile") or (profile.tts.profile_name if profile else "") or None
    tts_profile = visible_tts_profile_or_none(
        tts_profile_store.get(tts_profile_name) if tts_profile_name else None,
        identity.user_id,
        bypass=identity.unauthenticated,
    )
    # Same mapping conversation.py/lugo.py use -- services/conversation/tts_params.py.
    (
        tts_engine, tts_model, voice, ref_audio_path,
        ref_text, tts_instruct, tts_speed, tts_language,
    ) = tts_params_from_profile(tts_profile, fallback_voice=q.get("voice")) or TtsParams(
        engine=system_config_store.get().engines.default_tts_engine,
        model_id=q.get("tts_model") or "", voice=q.get("voice") or None,
        ref_audio_path=None, ref_text=None, instruct=None, speed=None, language=None,
    )
    # Same guard as conversation.py/stt.py: a client-supplied rate reaches
    # VadEndpointer, which divides by it. Refused at connect, not crashed on
    # after accept(). Error shape is livehost's own {"event": "error", ...}.
    try:
        sample_rate = parse_sample_rate(
            q.get("sample_rate"), settings.stt_stream_sample_rate
        )
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return
    audio_codec = (q.get("audio_codec") or "pcm16").lower()
    out_modalities = {m.strip() for m in (q.get("output") or "audio,text").lower().split(",") if m.strip()}
    want_audio = "audio" in out_modalities
    want_text = "text" in out_modalities
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

    resolved_stt_model = stt_model or resolve_default_stt_model(stt_engine)

    try:
        stt_provider = stt_service.get_provider(stt_engine)
        tts_provider = tts_service.get_provider(tts_engine)
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return

    # Same negotiate-or-downgrade both directions get in
    # services/conversation/session.py; imported inside the handler so a test
    # monkeypatching app.core.opus.opus_available still takes effect.
    from app.core.opus import make_decoder_or_downgrade, make_encoder_or_downgrade

    opus_decoder = None
    if audio_codec == "opus":
        opus_decoder = make_decoder_or_downgrade(sample_rate)
        if opus_decoder is None:
            audio_codec = "pcm16"

    opus_encoder = None
    if want_audio and audio_out == "opus":
        opus_encoder = make_encoder_or_downgrade(output_sample_rate)
        if opus_encoder is None:
            audio_out = "wav"

    responder = await build_responder_ex(
        base_url=llm_base_url, api_key=llm_api_key, model=llm_model, system_prompt=system_prompt,
        voice_optimized=bool(profile and profile.voice_optimized),
    )

    conv_cfg = system_config_store.get().conversation
    endpointer = build_endpointer(sample_rate, conv_cfg)

    history: list[dict] = []
    session_ready = True
    try:
        await session_store.create(
            session_id, profile_id=profile_name or "",
            meta={"stt_engine": stt_engine, "tts_engine": tts_engine, "livehost": True},
            # No `or profile.owner_id` fallback (H2): an identity with no
            # user_id (fleet/dev caller) must create an ownerless row, not one
            # attributed to the named profile's owner.
            user_id=identity.user_id,
        )
    except Exception as exc:  # noqa: BLE001 - session setup must not drop the connection
        logger.warning("livehost session setup failed for %s: %s", session_id, exc)
        session_ready = False
    turn = 0

    raw_social_queue: asyncio.Queue = asyncio.Queue()
    scheduler = EventScheduler(
        mention_keywords=_mention_keywords(),
        individual_threshold=settings.livehost_individual_threshold,
        batch_top_k=settings.livehost_batch_top_k,
        max_queue_size=settings.livehost_queue_max_size,
    )
    ingestor = TikTokLiveIngestor(
        client_factory=_default_tiktok_client_factory,
        queue=raw_social_queue,
        backoff_initial=settings.livehost_backoff_initial_seconds,
        backoff_max=settings.livehost_backoff_max_seconds,
        offline_poll_interval=settings.livehost_offline_poll_interval_seconds,
        watchdog_idle_seconds=settings.livehost_watchdog_idle_seconds,
    )
    livehost_registry.register(
        session_id, LivehostSession(scheduler=scheduler, ingestor=ingestor, user_id=identity.user_id)
    )
    orchestrator = LiveHostOrchestrator(scheduler)
    try:
        current_turn: asyncio.Task | None = None
        turn_lock = asyncio.Lock()
        drain_task: asyncio.Task | None = None
        poll_task: asyncio.Task | None = None
        watchdog = None
        stt_ready = is_ready(stt_provider)
        tts_ready = is_ready(tts_provider)
        await websocket.send_json({
            "event": "session_started",
            "session_id": session_id,
            "profile": profile_name,
            "stt_engine": stt_engine,
            "tts_engine": tts_engine,
            "responder": responder.name,
            "sample_rate": sample_rate,
            "audio_codec": audio_codec,
            "output": sorted(out_modalities),
            "audio_out": audio_out,
            "output_sample_rate": output_sample_rate if want_audio and audio_out == "opus" else None,
            "stt_ready": stt_ready,
            "tts_ready": tts_ready,
        })

        async def _warm_and_notify() -> None:
            await warm_providers(tts_provider, stt_provider)
            if not (stt_ready and tts_ready):
                try:
                    await websocket.send_json({"event": "engines_ready"})
                except Exception:  # noqa: BLE001 - socket may already be closed/gone
                    pass

        asyncio.create_task(_warm_and_notify())

        async def send(event: str, **payload) -> None:
            await websocket.send_json({"event": event, **payload})

        async def persist(role: str, content: str) -> None:
            if not session_ready:
                return
            try:
                await session_store.append_message(session_id, turn, role, content)
            except Exception as exc:  # noqa: BLE001 - persistence must not kill the turn
                logger.warning("livehost history persist failed: %s", exc)

        async def _record_llm_usage(responder_obj) -> None:
            # Thin wrapper (Task 6 dedup) over the shared
            # services/conversation/turn_usage helper -- closure keeps the
            # local `responder_obj` param shape both call sites below already
            # use, and reads profile/llm_model/profile_name/identity off the
            # enclosing livehost_stream() scope, same as before this refactor.
            await record_llm_turn_usage(
                responder_obj, identity_user_id=identity.user_id,
                profile=profile, profile_name=profile_name, llm_model=llm_model,
            )

        async def _stream_to_tts(sentence_aiter, responder_name: str) -> list[str]:
            parts: list[str] = []
            if not want_audio:
                index = 0
                async for sentence in sentence_aiter:
                    parts.append(sentence)
                    if want_text:
                        await send("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                    index += 1
                await _record_llm_usage(responder)
                return parts

            async def _synth(sentence: str):
                # (result, packets, error) mirrors
                # services/conversation/session.py's _synth: a TTS failure
                # (including TTSRequest construction itself -- e.g. a stored
                # profile's ref_audio_path that fails the artifacts-dir
                # containment check) is caught HERE and returned as the
                # third element instead of raised, so prefetch_synthesis
                # doesn't propagate it and unwind the whole turn -- which
                # would drop every not-yet-sent sentence's response_text
                # along with it. The consumer below emits this sentence's
                # response_text regardless, then a `tts_error` for audio only.
                #
                # Request construction + its degrade decision (Task 6 dedup)
                # is shared with session.py's _synth via turn_tts.py; the
                # provider call, metering, encode, and pacing below stay here.
                request, build_exc = build_tts_request_or_degrade(
                    text=sentence, engine=tts_engine, model_id=tts_model, voice=voice,
                    ref_audio_path=ref_audio_path, ref_text=ref_text,
                    instruct=tts_instruct, speed=tts_speed, language=tts_language,
                )
                if build_exc is not None:
                    logger.warning(
                        "livehost TTS synth failed (engine=%s) for %r: %s", tts_engine, sentence, build_exc
                    )
                    return None, None, build_exc
                try:
                    audio, media_type = await tts_provider.render_audio(request)
                    try:
                        await record_usage(
                            user_id=identity.user_id or "", profile_id=profile_name or "",
                            kind="tts", engine=tts_engine, model_id=tts_model or "",
                            unit="chars", native_amount=len(sentence or ""),
                        )
                    except Exception as exc:  # noqa: BLE001 - metering must never break the turn
                        logger.warning("livehost tts usage metering failed: %s", exc)
                    if opus_encoder is not None:
                        pcm = await asyncio.to_thread(wav_bytes_to_pcm16, audio, output_sample_rate)
                        packets = await asyncio.to_thread(opus_encoder.encode_pcm16, pcm)
                        return None, packets, None
                    return (audio, media_type), None, None
                except asyncio.CancelledError:
                    raise  # barge-in / turn supersede -- must propagate to unwind the turn
                except Exception as exc:  # noqa: BLE001 - degrade to text-only, don't lose the reply
                    logger.warning("livehost TTS synth failed (engine=%s) for %r: %s", tts_engine, sentence, exc)
                    return None, None, exc

            tts_error_reported = False
            async with aclosing(
                prefetch_synthesis(
                    sentence_aiter, _synth,
                    lookahead=system_config_store.get().conversation.conversation_tts_lookahead,
                )
            ) as pipeline:
                async for index, sentence, (audio, packets, tts_error) in pipeline:
                    parts.append(sentence)
                    if want_text:
                        await send("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                    if tts_error is not None:
                        # Synth failed for this sentence: text already went out above.
                        # Report the TTS failure once per turn (a fully-down engine
                        # would otherwise emit one per sentence) and skip audio -- the
                        # client (livehost.js) already handles `tts_error` by leaving
                        # the turn running and flagging the bubble text-only.
                        if not tts_error_reported:
                            tts_error_reported = True
                            await send(
                                "tts_error", turn=turn, chunk_index=index,
                                engine=tts_engine, message=str(tts_error),
                            )
                        continue
                    if packets is not None:
                        await send(
                            "audio_start", turn=turn, chunk_index=index,
                            text=sentence if want_text else None,
                            codec="opus", sample_rate=output_sample_rate, frames=len(packets),
                        )
                        conv_cfg = system_config_store.get().conversation
                        if conv_cfg.conversation_opus_pace and packets:
                            frame_s = opus_encoder.frame / opus_encoder.sample_rate
                            delays = pacing_delays(
                                len(packets), conv_cfg.conversation_opus_prebuffer_frames, frame_s
                            )
                        else:
                            delays = [0.0] * len(packets)
                        for delay, pkt in zip(delays, packets):
                            if delay:
                                await asyncio.sleep(delay)
                            await websocket.send_bytes(pkt)
                        await send("audio_end", turn=turn, chunk_index=index)
                    else:
                        audio_bytes, media_type = audio
                        await send(
                            "audio_start", turn=turn, chunk_index=index,
                            text=sentence if want_text else None,
                            codec="mp3" if media_type == "audio/mpeg" else "wav",
                        )
                        await websocket.send_bytes(audio_bytes)
                        await send("audio_end", turn=turn, chunk_index=index)
            await _record_llm_usage(responder)
            return parts

        async def _run_voice_turn(audio_pcm: bytes) -> None:
            nonlocal turn
            turn += 1
            await send("processing", turn=turn)
            blocked, quota_message = await _quota_blocked_for(
                user_id=identity.user_id or "", profile_name=profile_name or "",
                pinned_engine=(profile.llm.engine if profile else "") or "",
                pinned_model=llm_model or (profile.llm.model if profile else "") or "",
            )
            if blocked:
                await send("error", message=quota_message)
                await send("turn_done", turn=turn)
                return
            wav = pcm16_to_wav_bytes(audio_pcm, sample_rate=sample_rate)
            try:
                stt_result = await stt_provider.transcribe_bytes(wav, language, model=resolved_stt_model)
            except RuntimeError as exc:
                await send("error", message=f"STT failed: {exc}")
                await send("turn_done", turn=turn)
                return
            try:
                await record_usage(
                    user_id=identity.user_id or "", profile_id=profile_name or "",
                    kind="stt", engine=stt_engine, model_id=resolved_stt_model or "",
                    unit="seconds", native_amount=wav_duration_seconds(wav),
                )
            except Exception as exc:  # noqa: BLE001 - metering must never break the turn
                logger.warning("livehost stt usage metering failed: %s", exc)
            user_text = (stt_result.text or "").strip()
            await send("user_transcript", turn=turn, text=user_text, engine=stt_engine)
            if not user_text:
                await send("turn_done", turn=turn, skipped="empty transcript")
                return

            history.append({"role": "user", "content": user_text})
            await persist("user", user_text)
            parts = await _stream_to_tts(responder.reply_stream(history), responder.name)
            history.append({"role": "assistant", "content": " ".join(parts)})
            await persist("assistant", " ".join(parts))
            await send("turn_done", turn=turn)

        async def run_voice_turn(audio_pcm: bytes) -> None:
            # Per the spec, a voice-turn failure must surface to the streamer directly
            # (unlike a social-turn failure, which is just logged and dropped — see
            # run_social_turn) so the session never hangs waiting for a turn_done that
            # was lost to an uncaught exception inside the background task.
            try:
                await _run_voice_turn(audio_pcm)
            except Exception as exc:  # noqa: BLE001 - keep the session alive
                logger.exception("livehost voice turn failed")
                await send("error", message=str(exc))
                await send("turn_done", turn=turn)

        async def _run_social_turn(social_turn, formatted_text: str) -> None:
            nonlocal turn
            turn += 1
            await send(
                "social_reply", turn=turn,
                event_count=len(social_turn.events), overflow_count=social_turn.overflow_count,
            )
            blocked, quota_message = await _quota_blocked_for(
                user_id=identity.user_id or "", profile_name=profile_name or "",
                pinned_engine=(profile.llm.engine if profile else "") or "",
                pinned_model=llm_model or (profile.llm.model if profile else "") or "",
            )
            if blocked:
                await send("error", message=quota_message)
                await send("turn_done", turn=turn)
                return
            history.append({"role": "user", "content": formatted_text})
            await persist("user", formatted_text)
            parts = await _stream_to_tts(responder.reply_stream(history), responder.name)
            history.append({"role": "assistant", "content": " ".join(parts)})
            await persist("assistant", " ".join(parts))
            await send("turn_done", turn=turn)

        async def run_social_turn(social_turn, formatted_text: str) -> None:
            # Per the spec: a social-turn failure is logged and the event is dropped,
            # never surfaced as a hard error to the streamer — unlike run_voice_turn —
            # except a quota block, which is surfaced via _run_social_turn's own error send.
            try:
                await _run_social_turn(social_turn, formatted_text)
            except Exception:  # noqa: BLE001 - drop this social turn, keep the session alive
                logger.exception("livehost social turn failed, dropping event")

        async def _abort_turn_locked(reason: str) -> None:
            # Cancel the in-flight turn and clear current_turn. Caller must
            # already hold turn_lock -- fail fast instead of silently
            # reopening the exact race this lock exists to close.
            assert turn_lock.locked(), "_abort_turn_locked requires turn_lock to be held"
            nonlocal current_turn
            if current_turn and not current_turn.done():
                current_turn.cancel()
                try:
                    await current_turn
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                await send("aborted", reason=reason)
            current_turn = None

        async def abort_turn(reason: str) -> None:
            # Public entry point: acquires turn_lock itself. Do not call this
            # from a call site that already holds turn_lock (asyncio.Lock is
            # not re-entrant -- it would deadlock). Call _abort_turn_locked
            # directly instead in that case.
            async with turn_lock:
                await _abort_turn_locked(reason)

        async def _drain_social_events() -> None:
            while True:
                event = await raw_social_queue.get()
                scheduler.enqueue(event)
                try:
                    await send(
                        "social_event", kind=event.kind, user_name=event.user_name,
                        user_avatar_url=event.user_avatar_url, text=event.text,
                        gift_name=event.gift_name, gift_value=event.gift_value,
                    )
                except Exception:  # noqa: BLE001 - socket may already be closed
                    return

        async def _poll_social_turns() -> None:
            nonlocal current_turn
            while True:
                await asyncio.sleep(0.5)
                async with turn_lock:
                    voice_active = endpointer.speaking or (current_turn is not None and not current_turn.done())
                    if voice_active:
                        continue
                    result = orchestrator.poll_social_turn(voice_active=False)
                    if result is None:
                        continue
                    social_turn, formatted_text = result
                    current_turn = asyncio.create_task(run_social_turn(social_turn, formatted_text))

        drain_task = asyncio.create_task(_drain_social_events())
        poll_task = asyncio.create_task(_poll_social_turns())

        watchdog = build_identity_watchdog(identity, interval_s=_IDENTITY_RECHECK_INTERVAL_S)
        if watchdog is not None:
            watchdog.start()

        async for message in receive_with_watchdog(websocket, watchdog):
            if message is None:
                await websocket.close(code=4401, reason="account disabled")
                break
            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                frame = message["bytes"]
                if opus_decoder is not None:
                    try:
                        frame = opus_decoder.decode(frame)
                    except Exception as exc:  # noqa: BLE001 - skip a bad packet, keep going
                        logger.warning("livehost opus decode failed: %s", exc)
                        continue
                event = endpointer.accept(frame)
                if not event:
                    continue
                if event["event"] == "speech_start":
                    await abort_turn("barge-in")
                    await send("speech_start")
                elif event["event"] == "endpoint":
                    async with turn_lock:
                        await _abort_turn_locked("superseded")
                        await send("speech_end", speech_ms=round(event["speech_ms"]))
                        current_turn = asyncio.create_task(run_voice_turn(event["audio"]))

            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "abort":
                    await abort_turn("user")
                elif ctype == "reset":
                    await abort_turn("reset")
                    history.clear()
                    endpointer.reset()
                    await send("reset")
                elif ctype in {"flush", "end"}:
                    audio = endpointer.flush()
                    if audio:
                        async with turn_lock:
                            await _abort_turn_locked("superseded")
                            await send("speech_end", speech_ms=0)
                            current_turn = asyncio.create_task(run_voice_turn(audio))
                    if ctype == "end":
                        await abort_turn("end")
                        await send("done")
                        break
    except WebSocketDisconnect:
        pass
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if current_turn and not current_turn.done():
            current_turn.cancel()
        if drain_task:
            drain_task.cancel()
        if poll_task:
            poll_task.cancel()
        try:
            await responder.aclose()
        except Exception as exc:  # noqa: BLE001 - teardown must not fail
            logger.warning("responder aclose failed for %s: %s", session_id, exc)
        await ingestor.stop()
        livehost_registry.unregister(session_id)
        if session_ready:
            try:
                await session_store.mark_ended(session_id)
            except Exception as exc:  # noqa: BLE001 - teardown must not fail
                logger.warning("livehost mark_ended failed for %s: %s", session_id, exc)
        try:
            await websocket.close()
        except RuntimeError:
            pass


def _default_tiktok_client_factory(unique_id: str):
    from app.services.livehost.tiktok_adapter import TikTokLiveClientAdapter

    return TikTokLiveClientAdapter(unique_id)
