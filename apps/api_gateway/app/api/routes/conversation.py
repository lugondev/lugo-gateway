import asyncio
import json
import logging
import uuid
from contextlib import aclosing

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.audio import pcm16_to_wav_bytes, preprocess_pcm16, wav_file_to_pcm16
from app.core.errors import AppError
from app.core.settings import settings
from app.services.conversation.endpointer import VadEndpointer
from app.services.conversation.tools.base import ToolContext, ToolRegistry
from app.services.conversation.tools.local import LocalToolSource
from app.services.conversation.responder import (
    build_responder,
    build_responder_ex,
    get_active_llm_api_key,
    get_active_llm_base_url,
    get_active_llm_model,
    reset_active_llm_config,
    set_active_llm_config,
)
from app.services.conversation.tools.mcp import McpToolSource
from app.services.history.store import session_store
from app.services.mcp.pool import mcp_pool
from app.services.mcp.server_store import mcp_server_store
from app.services.memory.extractor import memory_extractor
from app.services.memory.retriever import inject_memories, memory_retriever
from app.services.profiles.store import profile_store
from app.services.stt.profile import resolve_stt_profile
from app.services.stt.providers.whisper_provider import get_active_whisper_model
from app.services.stt.routing import select_stt_engine
from app.services.stt.service import stt_service
from app.schemas.tts import TTSRequest
from app.services.tts.segmenter import segment_text
from app.services.tts.service import tts_service
from app.services.tts.streaming import pacing_delays, prefetch_synthesis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/conversation", tags=["conversation"])


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


def _llm_config_view() -> dict:
    """Current conversation LLM config (api key masked, never echoed back)."""
    return {
        "base_url": get_active_llm_base_url(),
        "model": get_active_llm_model(),
        "api_key_set": bool(get_active_llm_api_key()),
        "responder": build_responder().name,
    }


@router.get("/llm")
async def get_llm_config() -> dict:
    return {"success": True, "data": _llm_config_view()}


@router.post("/llm")
async def set_llm_config(payload: LlmConfig) -> dict:
    """Point the conversation LLM at any OpenAI-compatible endpoint (online or local)."""
    set_active_llm_config(payload.base_url, payload.api_key, payload.model)
    return {"success": True, "data": _llm_config_view()}


@router.post("/llm/reset")
async def reset_llm_config() -> dict:
    """Revert to the .env-configured conversation LLM."""
    reset_active_llm_config()
    return {"success": True, "data": _llm_config_view()}


@router.post("/chat")
async def chat(payload: ChatRequest, profile: str | None = None, session_id: str | None = None) -> dict:
    """Text chat with the configured conversation responder (LLM or echo)."""
    active_profile = profile_store.get(profile) if profile else None
    llm_base_url = (active_profile.llm.base_url or None) if (active_profile and active_profile.llm.base_url) else None
    llm_api_key = active_profile.llm.api_key if (active_profile and active_profile.llm.base_url) else None
    llm_model = (active_profile.llm.model or None) if (active_profile and active_profile.llm.model) else None
    system_prompt = (active_profile.system_prompt or None) if (active_profile and active_profile.system_prompt) else None

    # Session: resume when session_id given (stored messages prefix the context).
    sid = session_id or str(uuid.uuid4())
    stored: list[dict] = []
    if session_id and await session_store.exists(session_id):
        stored = await session_store.get_messages(session_id)
    elif not await session_store.exists(sid):
        await session_store.create(sid, profile_id=profile or "")

    new_msgs = [{"role": m.role, "content": m.content} for m in payload.messages]
    history = [{"role": m["role"], "content": m["content"]} for m in stored] + new_msgs

    # Memory injection: prepend the profile's memories to the system prompt.
    last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    block = await memory_retriever.get_context(active_profile, query=last_user)
    system_prompt = inject_memories(system_prompt or settings.conversation_system_prompt, block) if block else system_prompt

    responder = build_responder_ex(
        base_url=llm_base_url,
        api_key=llm_api_key,
        model=llm_model,
        system_prompt=system_prompt,
    )
    reply = await responder.reply(history)

    turn = (len(stored) // 2) + 1
    for m in new_msgs:
        await session_store.append_message(sid, turn, m["role"], m["content"])
    await session_store.append_message(sid, turn, "assistant", reply)
    if active_profile and active_profile.memory.enabled and active_profile.llm.base_url:
        asyncio.create_task(memory_extractor.extract_and_upsert(sid, active_profile))

    return {
        "success": True,
        "data": {
            "reply": reply,
            "responder": responder.name,
            "model": get_active_llm_model(),
            "profile": profile,
            "session_id": sid,
        },
    }


@router.websocket("/stream")
async def conversation_stream(websocket: WebSocket) -> None:
    await websocket.accept()
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

    # LLM config: profile overrides global state / .env
    llm_base_url = (profile.llm.base_url or None) if (profile and profile.llm.base_url) else None
    llm_api_key = profile.llm.api_key if (profile and profile.llm.base_url) else None
    llm_model = (profile.llm.model or None) if (profile and profile.llm.model) else None
    system_prompt = (profile.system_prompt or None) if (profile and profile.system_prompt) else None

    # Language profile (vi|en|multi|en_vi) sets the engine + language default; an
    # explicit stt_engine/language query param still wins over it.
    stt_profile_res = resolve_stt_profile(q.get("profile") or settings.stt_profile)
    if stt_profile_res:
        prof_engine, prof_lang = stt_profile_res
        stt_engine = q.get("stt_engine") or prof_engine
        language = q.get("language") or prof_lang
    else:
        stt_engine = q.get("stt_engine") or settings.conversation_stt_engine or settings.default_stt_engine
        language = q.get("language") or settings.conversation_language or None
    if profile and profile.tts.engine:
        tts_engine = profile.tts.engine
        voice = profile.tts.voice or q.get("voice") or None
    else:
        tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
        voice = q.get("voice") or None
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
    # How reply audio is delivered: "url" (browser fetches /artifacts) or "opus"/"pcm"
    # binary frames pushed over the socket (for ESP32/RPi that can't fetch mid-stream).
    audio_out = (q.get("audio_out") or "url").lower()
    output_sample_rate = int(q.get("output_sample_rate", 24000))
    # Only optional noise reduction here — the endpointer already does VAD
    # segmentation and Whisper has its own vad_filter, so an extra VAD gate on the
    # utterance would clip speech and hurt recognition.
    denoise = _truthy(q.get("denoise"), settings.stt_noise_reduce_enabled)

    try:
        stt_provider = stt_service.get_provider(stt_engine)
        tts_provider = tts_service.get_provider(tts_engine)
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return

    # Set up Opus decoding if the client negotiated it; fall back to PCM16 with a
    # warning if the server lacks libopus (so the connection still works).
    opus_decoder = None
    if audio_codec == "opus":
        from app.core.opus import OpusFrameDecoder, opus_available

        if opus_available():
            opus_decoder = OpusFrameDecoder(sample_rate=sample_rate, channels=1)
        else:
            audio_codec = "pcm16"
            logger.warning("client requested opus but server has no libopus; using pcm16")

    # Set up Opus encoding for pushed audio output (devices). Fall back to url.
    opus_encoder = None
    if want_audio and audio_out == "opus":
        from app.core.opus import OpusFrameEncoder, opus_available

        if opus_available():
            opus_encoder = OpusFrameEncoder(sample_rate=output_sample_rate, channels=1)
        else:
            audio_out = "url"
            logger.warning("client requested opus output but server has no libopus; using url")

    responder = build_responder_ex(
        base_url=llm_base_url,
        api_key=llm_api_key,
        model=llm_model,
        system_prompt=system_prompt,
    )

    # Tool registry: merge global MCP servers + per-profile MCP servers (profile wins on name collision).
    global_servers = mcp_server_store.list()
    profile_specific = {s.name: s for s in (profile.mcp_servers if profile else [])}
    merged_servers = {**global_servers, **profile_specific}

    tool_sources: list = []
    if settings.conversation_tools_enabled:
        tool_sources.append(LocalToolSource())
    for srv in merged_servers.values():
        tools = await mcp_pool.get_tools(srv.url)
        if tools:
            tool_sources.append(
                McpToolSource(tools, invoker=lambda n, a, u=srv.url: mcp_pool.invoke(u, n, a))
            )
    tool_registry: ToolRegistry | None = ToolRegistry(tool_sources) if tool_sources else None

    # Detail strings so the UI can show exactly WHICH models are active this session.
    if hasattr(stt_provider, "detail"):
        stt_detail = stt_provider.detail()  # whisper_mlx / whisper_gemma expose this
    elif stt_engine in {"whisper", "whisper_local"}:
        stt_detail = get_active_whisper_model()
    else:
        stt_detail = stt_engine
    tts_detail = tts_provider.detail() if hasattr(tts_provider, "detail") else tts_engine

    # Audio-native: an STT engine that can answer the audio itself (Qwen-Omni) replies
    # directly, skipping the separate text LLM.
    audio_native = (
        settings.conversation_audio_native
        and stt_engine == "qwen_omni"
        and hasattr(stt_provider, "converse")
    )
    llm_model = "qwen_omni (audio-native)" if audio_native else (
        get_active_llm_model() if responder.name == "llm" else responder.name
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
    # Session persistence: resume seeds history from the DB; new sessions are recorded.
    history: list[dict] = []
    if requested_sid and await session_store.exists(requested_sid):
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in await session_store.get_messages(requested_sid)
        ]
    else:
        await session_store.create(
            session_id,
            profile_id=profile_name or "",
            meta={"stt_engine": stt_engine, "tts_engine": tts_engine},
        )
    turn = 0

    active_tools = list(tool_registry._tools.keys()) if tool_registry else []
    await websocket.send_json(
        {
            "event": "session_started",
            "session_id": session_id,
            "profile": profile_name,
            "active_tools": active_tools,
            "stt_engine": stt_engine,
            "stt_detail": stt_detail,
            "tts_engine": tts_engine,
            "tts_detail": tts_detail,
            "responder": responder.name,
            "llm_model": llm_model,
            "sample_rate": sample_rate,
            "audio_codec": audio_codec,
            "output": sorted(out_modalities),
            "audio_out": audio_out,
            "output_sample_rate": output_sample_rate if want_audio and audio_out != "url" else None,
        }
    )

    # Warm TTS (and STT if it supports it, e.g. MLX graph compile) in the background
    # while the user speaks their first turn, so the first turn isn't delayed by a
    # cold model load / compile.
    async def _warm() -> None:
        for provider in (tts_provider, stt_provider):
            warm = getattr(provider, "warm", None)
            if not callable(warm):
                continue
            try:
                await asyncio.to_thread(warm)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s warm failed: %s", type(provider).__name__, exc)

    asyncio.create_task(_warm())

    async def send(event: str, **payload) -> None:
        await websocket.send_json({"event": event, **payload})

    base_system_prompt = system_prompt or settings.conversation_system_prompt

    async def persist(role: str, content: str) -> None:
        try:
            await session_store.append_message(session_id, turn, role, content)
        except Exception as exc:  # noqa: BLE001 - persistence must not kill the turn
            logger.warning("history persist failed: %s", exc)

    async def refresh_memory(query: str) -> None:
        """Per-turn memory injection (mutates the responder's system prompt)."""
        if not hasattr(responder, "system_prompt"):
            return
        try:
            block = await memory_retriever.get_context(profile, query=query)
            responder.system_prompt = inject_memories(base_system_prompt, block)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory retrieval failed: %s", exc)

    async def _emit_command(cmd_payload: dict) -> None:
        await websocket.send_json(cmd_payload)

    tool_ctx = ToolContext(emit_command=_emit_command, language=language or None)

    async def handle_turn(
        audio_pcm: bytes | None = None, text_input: str | None = None, speech_ms: float = 0.0
    ) -> None:
        try:
            await _run_turn(audio_pcm=audio_pcm, text_input=text_input, speech_ms=speech_ms)
        except Exception as exc:  # noqa: BLE001 - keep the conversation alive
            logger.exception("conversation turn failed")
            await send("error", message=str(exc))
            await send("turn_done", turn=turn)

    async def _run_turn(
        audio_pcm: bytes | None = None, text_input: str | None = None, speech_ms: float = 0.0
    ) -> None:
        nonlocal turn
        turn += 1
        await send("processing", turn=turn)

        # Stream a sentence iterator through TTS. For audio output we synthesize up to
        # `conversation_tts_lookahead` sentences AHEAD of sending (prefetch_synthesis),
        # so the next sentence's audio is usually ready before the current finishes ->
        # gapless playback. Text-only just emits sentences as the LLM streams them.
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
                result = await tts_provider.synthesize(
                    TTSRequest(text=sentence, engine=tts_engine, voice=voice)
                )
                if opus_encoder is not None:
                    # Decode the TTS wav -> PCM16 @ output_sr -> Opus packets (off-loop).
                    path = result.audio_url.lstrip("/")
                    pcm = await asyncio.to_thread(wav_file_to_pcm16, path, output_sample_rate)
                    packets = await asyncio.to_thread(opus_encoder.encode_pcm16, pcm)
                    return result, packets
                return result, None

            async with aclosing(
                prefetch_synthesis(
                    sentence_aiter, _synth, lookahead=settings.conversation_tts_lookahead
                )
            ) as pipeline:
                async for index, sentence, (result, packets) in pipeline:
                    parts.append(sentence)
                    if want_text:
                        await send("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                    if packets is not None:
                        # Push Opus binary frames bracketed by audio_start/audio_end (devices).
                        await send(
                            "audio_start", turn=turn, chunk_index=index,
                            text=sentence if want_text else None,
                            codec="opus", sample_rate=output_sample_rate, frames=len(packets),
                        )
                        # Pace packets at real-time after an initial burst so the
                        # device buffer isn't flooded on long replies (xiaozhi-style).
                        if settings.conversation_opus_pace and packets:
                            frame_s = opus_encoder.frame / opus_encoder.sample_rate
                            delays = pacing_delays(
                                len(packets), settings.conversation_opus_prebuffer_frames, frame_s
                            )
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

        # Text input: skip STT, reply via the text responder (text→text / text→audio).
        if text_input is not None:
            user_text = (text_input or "").strip()
            await send("user_transcript", turn=turn, text=user_text, engine="text")
            if not user_text:
                await send("turn_done", turn=turn, skipped="empty text")
                return
            history.append({"role": "user", "content": user_text})
            await persist("user", user_text)
            await refresh_memory(user_text)
            parts = await _stream_to_tts(
                responder.reply_stream(
                    history,
                    registry=tool_registry,
                    ctx=tool_ctx,
                    max_iters=settings.conversation_tool_max_iters,
                ),
                responder.name,
            )
            history.append({"role": "assistant", "content": " ".join(parts)})
            await persist("assistant", " ".join(parts))
            await send("turn_done", turn=turn)
            return

        # Audio input: build the wav for STT / audio-native.
        pcm = audio_pcm
        if denoise:
            pcm = preprocess_pcm16(
                audio_pcm, sample_rate, denoise=True, vad=False,
                amount=settings.stt_noise_reduce_amount,
            )
        wav = pcm16_to_wav_bytes(pcm, sample_rate=sample_rate)

        # Audio-native: Qwen-Omni answers the audio directly (one pass), no separate
        # text LLM. Otherwise: transcribe -> text responder (gemma/online/echo).
        if audio_native:
            try:
                transcript, reply = await stt_provider.converse(
                    wav, history, settings.conversation_system_prompt
                )
            except RuntimeError as exc:
                await send("error", message=f"Qwen-Omni failed: {exc}")
                return
            reply = (reply or "").strip()
            # The audio-native pass occasionally returns an empty reply on some clips
            # — fall back to the transcribe -> text-LLM cascade so the turn still answers.
            if reply:
                await send("user_transcript", turn=turn, text=transcript or "🎤", engine=stt_engine)
                if transcript:
                    history.append({"role": "user", "content": transcript})
                    await persist("user", transcript)

                async def _reply_sentences():
                    for sentence in segment_text(reply):
                        yield sentence

                parts = await _stream_to_tts(_reply_sentences(), stt_engine)
                history.append({"role": "assistant", "content": " ".join(parts)})
                await persist("assistant", " ".join(parts))
                await send("turn_done", turn=turn)
                return
            logger.info("qwen_omni audio-native reply empty; falling back to transcribe + LLM")

        # Fast-path routing: short commands can go to a lower-latency engine.
        turn_provider = stt_provider
        turn_engine = stt_engine
        if speech_ms and settings.conversation_fast_stt_engine:
            chosen = select_stt_engine(
                speech_ms,
                stt_engine,
                settings.conversation_fast_stt_engine,
                settings.conversation_fast_stt_max_ms,
            )
            if chosen != stt_engine:
                try:
                    turn_provider = stt_service.get_provider(chosen)
                    turn_engine = chosen
                except AppError:
                    logger.info("fast STT engine %s unavailable; using %s", chosen, stt_engine)

        try:
            stt_result = await turn_provider.transcribe_bytes(wav, language)
        except RuntimeError as exc:
            await send("error", message=f"STT failed: {exc}")
            return
        user_text = (stt_result.text or "").strip()
        await send("user_transcript", turn=turn, text=user_text, engine=turn_engine)
        if not user_text:
            await send("turn_done", turn=turn, skipped="empty transcript")
            return

        history.append({"role": "user", "content": user_text})
        await persist("user", user_text)
        await refresh_memory(user_text)
        parts = await _stream_to_tts(
            responder.reply_stream(
                history,
                registry=tool_registry,
                ctx=tool_ctx,
                max_iters=settings.conversation_tool_max_iters,
            ),
            responder.name,
        )
        history.append({"role": "assistant", "content": " ".join(parts)})
        await persist("assistant", " ".join(parts))
        await send("turn_done", turn=turn)

    current_turn: asyncio.Task | None = None

    async def abort_turn(reason: str) -> None:
        nonlocal current_turn
        if current_turn and not current_turn.done():
            current_turn.cancel()
            try:
                await current_turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await send("aborted", reason=reason)
        current_turn = None

    try:
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
                        logger.warning("opus decode failed: %s", exc)
                        continue
                event = endpointer.accept(frame)
                if not event:
                    continue
                if event["event"] == "speech_start":
                    # Barge-in: user starts talking -> cancel the assistant's turn.
                    await abort_turn("barge-in")
                    await send("speech_start")
                elif event["event"] == "endpoint":
                    await abort_turn("superseded")
                    await send("speech_end", speech_ms=round(event["speech_ms"]))
                    current_turn = asyncio.create_task(
                        handle_turn(event["audio"], speech_ms=event["speech_ms"])
                    )

            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "text":
                    # Text input turn (text→text / text→audio). Supersedes any current turn.
                    await abort_turn("superseded")
                    current_turn = asyncio.create_task(handle_turn(text_input=control.get("text") or ""))
                elif ctype == "abort":
                    await abort_turn("user")
                elif ctype == "reset":
                    await abort_turn("reset")
                    history.clear()
                    endpointer.reset()
                    await send("reset")
                elif ctype in {"flush", "end"}:
                    audio = endpointer.flush()
                    if audio:
                        await abort_turn("superseded")
                        await send("speech_end", speech_ms=0)
                        current_turn = asyncio.create_task(handle_turn(audio))
                    if ctype == "end":
                        await abort_turn("end")
                        await send("done")
                        break
    except WebSocketDisconnect:
        pass
    finally:
        if current_turn and not current_turn.done():
            current_turn.cancel()
        await session_store.mark_ended(session_id)
        if profile is not None:
            asyncio.create_task(memory_extractor.extract_and_upsert(session_id, profile))
        try:
            await websocket.close()
        except RuntimeError:
            pass
