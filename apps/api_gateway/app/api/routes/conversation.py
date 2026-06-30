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
from app.services.conversation.responder import (
    build_responder,
    get_active_llm_api_key,
    get_active_llm_base_url,
    get_active_llm_model,
    reset_active_llm_config,
    set_active_llm_config,
)
from app.services.stt.providers.whisper_provider import get_active_whisper_model
from app.services.stt.service import stt_service
from app.schemas.tts import TTSRequest
from app.services.tts.segmenter import segment_text
from app.services.tts.service import tts_service
from app.services.tts.streaming import prefetch_synthesis

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
async def chat(payload: ChatRequest) -> dict:
    """Text chat with the configured conversation responder (LLM or echo)."""
    responder = build_responder()
    history = [{"role": m.role, "content": m.content} for m in payload.messages]
    reply = await responder.reply(history)
    return {
        "success": True,
        "data": {"reply": reply, "responder": responder.name, "model": get_active_llm_model()},
    }


@router.websocket("/stream")
async def conversation_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    session_id = str(uuid.uuid4())
    q = websocket.query_params

    stt_engine = q.get("stt_engine") or settings.conversation_stt_engine or settings.default_stt_engine
    tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
    voice = q.get("voice") or None
    language = q.get("language") or settings.conversation_language or None
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

    responder = build_responder()

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
    )
    history: list[dict] = []
    turn = 0

    await websocket.send_json(
        {
            "event": "session_started",
            "session_id": session_id,
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

    async def handle_turn(audio_pcm: bytes | None = None, text_input: str | None = None) -> None:
        try:
            await _run_turn(audio_pcm=audio_pcm, text_input=text_input)
        except Exception as exc:  # noqa: BLE001 - keep the conversation alive
            logger.exception("conversation turn failed")
            await send("error", message=str(exc))
            await send("turn_done", turn=turn)

    async def _run_turn(audio_pcm: bytes | None = None, text_input: str | None = None) -> None:
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
                        for pkt in packets:
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
            parts = await _stream_to_tts(responder.reply_stream(history), responder.name)
            history.append({"role": "assistant", "content": " ".join(parts)})
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

                async def _reply_sentences():
                    for sentence in segment_text(reply) or [reply]:
                        yield sentence

                parts = await _stream_to_tts(_reply_sentences(), stt_engine)
                history.append({"role": "assistant", "content": " ".join(parts)})
                await send("turn_done", turn=turn)
                return
            logger.info("qwen_omni audio-native reply empty; falling back to transcribe + LLM")

        try:
            stt_result = await stt_provider.transcribe_bytes(wav, language)
        except RuntimeError as exc:
            await send("error", message=f"STT failed: {exc}")
            return
        user_text = (stt_result.text or "").strip()
        await send("user_transcript", turn=turn, text=user_text, engine=stt_engine)
        if not user_text:
            await send("turn_done", turn=turn, skipped="empty transcript")
            return

        history.append({"role": "user", "content": user_text})
        parts = await _stream_to_tts(responder.reply_stream(history), responder.name)
        history.append({"role": "assistant", "content": " ".join(parts)})
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
                    current_turn = asyncio.create_task(handle_turn(event["audio"]))

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
        try:
            await websocket.close()
        except RuntimeError:
            pass
