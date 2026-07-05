import asyncio
import json
import logging
import uuid
from contextlib import aclosing

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core.audio import pcm16_to_wav_bytes, wav_file_to_pcm16
from app.core.errors import AppError
from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.conversation.endpointer import VadEndpointer
from app.services.conversation.responder import build_responder_ex
from app.services.history.store import session_store
from app.services.livehost.ingestor import TikTokLiveIngestor
from app.services.livehost.orchestrator import LiveHostOrchestrator
from app.services.livehost.registry import LivehostSession, livehost_registry
from app.services.livehost.scheduler import EventScheduler
from app.services.profiles.store import profile_store
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service
from app.services.tts.streaming import pacing_delays, prefetch_synthesis
from app.services.warmup import is_ready, warm_providers

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/livehost", tags=["livehost"])


def _mention_keywords() -> list[str]:
    return [k.strip() for k in settings.livehost_mention_keywords.split(",") if k.strip()]


class TikTokConnectRequest(BaseModel):
    unique_id: str


@router.post("/{session_id}/connect")
async def connect_tiktok(session_id: str, payload: TikTokConnectRequest) -> dict:
    session = livehost_registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"livehost session '{session_id}' not found")
    await session.ingestor.start(payload.unique_id)
    return {"success": True, "data": {"state": session.ingestor.state.value, "unique_id": payload.unique_id}}


@router.post("/{session_id}/disconnect")
async def disconnect_tiktok(session_id: str) -> dict:
    session = livehost_registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"livehost session '{session_id}' not found")
    await session.ingestor.stop()
    return {"success": True, "data": {"state": session.ingestor.state.value}}


@router.get("/{session_id}/status")
async def livehost_status(session_id: str) -> dict:
    session = livehost_registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"livehost session '{session_id}' not found")
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
    await websocket.accept()
    session_id = websocket.query_params.get("session_id") or str(uuid.uuid4())
    q = websocket.query_params

    profile_name = q.get("profile")
    profile = profile_store.get(profile_name) if profile_name else None
    llm_base_url = (profile.llm.base_url or None) if (profile and profile.llm.base_url) else None
    llm_api_key = profile.llm.api_key if (profile and profile.llm.base_url) else None
    llm_model = (profile.llm.model or None) if (profile and profile.llm.model) else None
    system_prompt = (profile.system_prompt or None) if (profile and profile.system_prompt) else None

    stt_engine = q.get("stt_engine") or settings.conversation_stt_engine or settings.default_stt_engine
    language = q.get("language") or settings.conversation_language or None
    if profile and profile.tts.engine:
        tts_engine = profile.tts.engine
        voice = profile.tts.voice or q.get("voice") or None
    else:
        tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
        voice = q.get("voice") or None
    sample_rate = int(q.get("sample_rate", settings.stt_stream_sample_rate))
    audio_codec = (q.get("audio_codec") or "pcm16").lower()
    out_modalities = {m.strip() for m in (q.get("output") or "audio,text").lower().split(",") if m.strip()}
    want_audio = "audio" in out_modalities
    want_text = "text" in out_modalities
    audio_out = (q.get("audio_out") or "url").lower()
    output_sample_rate = int(q.get("output_sample_rate", 24000))

    try:
        stt_provider = stt_service.get_provider(stt_engine)
        tts_provider = tts_service.get_provider(tts_engine)
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return

    opus_decoder = None
    if audio_codec == "opus":
        from app.core.opus import OpusFrameDecoder, opus_available

        if opus_available():
            opus_decoder = OpusFrameDecoder(sample_rate=sample_rate, channels=1)
        else:
            audio_codec = "pcm16"
            logger.warning("client requested opus but server has no libopus; using pcm16")

    opus_encoder = None
    if want_audio and audio_out == "opus":
        from app.core.opus import OpusFrameEncoder, opus_available

        if opus_available():
            opus_encoder = OpusFrameEncoder(sample_rate=output_sample_rate, channels=1)
        else:
            audio_out = "url"
            logger.warning("client requested opus output but server has no libopus; using url")

    responder = build_responder_ex(
        base_url=llm_base_url, api_key=llm_api_key, model=llm_model, system_prompt=system_prompt,
    )

    endpointer = VadEndpointer(
        sample_rate,
        silence_ms=settings.conversation_silence_ms,
        min_speech_ms=settings.conversation_min_speech_ms,
        rms_threshold=settings.conversation_rms_threshold,
        max_utterance_ms=settings.conversation_max_utterance_ms,
        min_silence_ms=settings.conversation_min_silence_ms,
        adaptive_full_ms=settings.conversation_adaptive_full_ms,
    )

    history: list[dict] = []
    session_ready = True
    try:
        await session_store.create(
            session_id, profile_id=profile_name or "",
            meta={"stt_engine": stt_engine, "tts_engine": tts_engine, "livehost": True},
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
    livehost_registry.register(session_id, LivehostSession(scheduler=scheduler, ingestor=ingestor))
    orchestrator = LiveHostOrchestrator(scheduler)
    try:
        current_turn: asyncio.Task | None = None
        turn_lock = asyncio.Lock()
        drain_task: asyncio.Task | None = None
        poll_task: asyncio.Task | None = None
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
            "output_sample_rate": output_sample_rate if want_audio and audio_out != "url" else None,
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

        async def _stream_to_tts(sentence_aiter, responder_name: str) -> list[str]:
            parts: list[str] = []
            if not want_audio:
                index = 0
                async for sentence in sentence_aiter:
                    parts.append(sentence)
                    if want_text:
                        await send("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                    index += 1
                return parts

            async def _synth(sentence: str):
                result = await tts_provider.synthesize(TTSRequest(text=sentence, engine=tts_engine, voice=voice))
                if opus_encoder is not None:
                    path = result.audio_url.lstrip("/")
                    pcm = await asyncio.to_thread(wav_file_to_pcm16, path, output_sample_rate)
                    packets = await asyncio.to_thread(opus_encoder.encode_pcm16, pcm)
                    return result, packets
                return result, None

            async with aclosing(
                prefetch_synthesis(sentence_aiter, _synth, lookahead=settings.conversation_tts_lookahead)
            ) as pipeline:
                async for index, sentence, (result, packets) in pipeline:
                    parts.append(sentence)
                    if want_text:
                        await send("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                    if packets is not None:
                        await send(
                            "audio_start", turn=turn, chunk_index=index,
                            text=sentence if want_text else None,
                            codec="opus", sample_rate=output_sample_rate, frames=len(packets),
                        )
                        if settings.conversation_opus_pace and packets:
                            frame_s = opus_encoder.frame / opus_encoder.sample_rate
                            delays = pacing_delays(len(packets), settings.conversation_opus_prebuffer_frames, frame_s)
                        else:
                            delays = [0.0] * len(packets)
                        for delay, pkt in zip(delays, packets):
                            if delay:
                                await asyncio.sleep(delay)
                            await websocket.send_bytes(pkt)
                        await send("audio_end", turn=turn, chunk_index=index)
                    else:
                        await send(
                            "audio_chunk", turn=turn, chunk_index=index, text=sentence,
                            audio_url=result.audio_url, sample_rate=result.sample_rate, mock=result.mock,
                        )
            return parts

        async def _run_voice_turn(audio_pcm: bytes) -> None:
            nonlocal turn
            turn += 1
            await send("processing", turn=turn)
            wav = pcm16_to_wav_bytes(audio_pcm, sample_rate=sample_rate)
            try:
                stt_result = await stt_provider.transcribe_bytes(wav, language)
            except RuntimeError as exc:
                await send("error", message=f"STT failed: {exc}")
                await send("turn_done", turn=turn)
                return
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
            history.append({"role": "user", "content": formatted_text})
            await persist("user", formatted_text)
            parts = await _stream_to_tts(responder.reply_stream(history), responder.name)
            history.append({"role": "assistant", "content": " ".join(parts)})
            await persist("assistant", " ".join(parts))
            await send("turn_done", turn=turn)

        async def run_social_turn(social_turn, formatted_text: str) -> None:
            # Per the spec: a social-turn failure is logged and the event is dropped,
            # never surfaced as a hard error to the streamer — unlike run_voice_turn.
            try:
                await _run_social_turn(social_turn, formatted_text)
            except Exception:  # noqa: BLE001 - drop this social turn, keep the session alive
                logger.exception("livehost social turn failed, dropping event")

        async def _abort_turn_locked(reason: str) -> None:
            # Cancel the in-flight turn and clear current_turn. Caller must
            # already hold turn_lock.
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

        while True:
            message = await websocket.receive()
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
        if current_turn and not current_turn.done():
            current_turn.cancel()
        if drain_task:
            drain_task.cancel()
        if poll_task:
            poll_task.cancel()
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
