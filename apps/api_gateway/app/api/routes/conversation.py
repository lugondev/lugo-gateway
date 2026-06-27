import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.audio import pcm16_to_wav_bytes, preprocess_pcm16
from app.core.errors import AppError
from app.core.settings import settings
from app.services.artifacts import artifact_store
from app.services.conversation.endpointer import VadEndpointer
from app.services.conversation.responder import build_responder, get_active_llm_model
from app.services.stt.service import stt_service
from app.schemas.tts import TTSRequest
from app.services.tts.service import tts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/conversation", tags=["conversation"])


def _truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_audio(audio_url: str | None) -> bytes:
    if not audio_url:
        return b""
    path = artifact_store.base_dir / audio_url.rsplit("/", 1)[-1]
    return path.read_bytes() if path.is_file() else b""


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)


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

    responder = build_responder()
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
            "tts_engine": tts_engine,
            "responder": responder.name,
            "sample_rate": sample_rate,
        }
    )

    # Warm the TTS model in the background while the user speaks their first turn,
    # so the first reply isn't delayed by a cold model load.
    async def _warm_tts() -> None:
        try:
            await asyncio.to_thread(tts_provider.warm)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTS warm failed: %s", exc)

    asyncio.create_task(_warm_tts())

    async def send(event: str, **payload) -> None:
        await websocket.send_json({"event": event, **payload})

    async def handle_turn(audio_pcm: bytes) -> None:
        try:
            await _run_turn(audio_pcm)
        except Exception as exc:  # noqa: BLE001 - keep the conversation alive
            logger.exception("conversation turn failed")
            await send("error", message=str(exc))
            await send("turn_done", turn=turn)

    async def _run_turn(audio_pcm: bytes) -> None:
        nonlocal turn
        turn += 1
        await send("processing", turn=turn)

        pcm = audio_pcm
        if denoise:
            pcm = preprocess_pcm16(
                audio_pcm, sample_rate, denoise=True, vad=False,
                amount=settings.stt_noise_reduce_amount,
            )
        wav = pcm16_to_wav_bytes(pcm, sample_rate=sample_rate)
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

        # Stream the reply sentence-by-sentence: synthesize + send each sentence the
        # moment the LLM finishes it, so audio starts long before the full reply.
        parts: list[str] = []
        index = 0
        async for sentence in responder.reply_stream(history):
            parts.append(sentence)
            await send("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder.name)
            result = await tts_provider.synthesize(
                TTSRequest(text=sentence, engine=tts_engine, voice=voice)
            )
            await send(
                "audio_chunk",
                turn=turn,
                chunk_index=index,
                text=sentence,
                sample_rate=result.sample_rate,
                mock=result.mock,
            )
            # Send the WAV inline as a binary frame so the client doesn't pay an
            # HTTP fetch per sentence (lower latency, smoother for remote clients).
            audio = _read_audio(result.audio_url)
            if audio:
                await websocket.send_bytes(audio)
            index += 1

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
                event = endpointer.accept(message["bytes"])
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
