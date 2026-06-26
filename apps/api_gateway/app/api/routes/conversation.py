import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.audio import pcm16_to_wav_bytes
from app.core.errors import AppError
from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.conversation.endpointer import VadEndpointer
from app.services.conversation.responder import build_responder, get_active_llm_model
from app.services.stt.service import stt_service
from app.services.tts.segmenter import segment_text
from app.services.tts.service import tts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/conversation", tags=["conversation"])


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
    sample_rate = int(q.get("sample_rate", settings.stt_stream_sample_rate))

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

        wav = pcm16_to_wav_bytes(audio_pcm, sample_rate=sample_rate)
        try:
            stt_result = await stt_provider.transcribe_bytes(wav)
        except RuntimeError as exc:
            await send("error", message=f"STT failed: {exc}")
            return
        user_text = (stt_result.text or "").strip()
        await send("user_transcript", turn=turn, text=user_text, engine=stt_engine)
        if not user_text:
            await send("turn_done", turn=turn, skipped="empty transcript")
            return

        history.append({"role": "user", "content": user_text})
        reply = await responder.reply(history)
        history.append({"role": "assistant", "content": reply})
        await send("response_text", turn=turn, text=reply, responder=responder.name)

        # Long replies are split into sentences and synthesized chunk-by-chunk
        # so audio starts playing before the whole reply is rendered.
        segments = segment_text(reply)
        for index, seg in enumerate(segments):
            req = TTSRequest(text=seg, engine=tts_engine, voice=voice)
            result = await tts_provider.synthesize(req)
            await send(
                "audio_chunk",
                turn=turn,
                chunk_index=index,
                total_chunks=len(segments),
                text=seg,
                audio_url=result.audio_url,
                sample_rate=result.sample_rate,
                mock=result.mock,
            )
        await send("turn_done", turn=turn)

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
                    await send("speech_start")
                elif event["event"] == "endpoint":
                    await send("speech_end", speech_ms=round(event["speech_ms"]))
                    await handle_turn(event["audio"])

            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "reset":
                    history.clear()
                    endpointer.reset()
                    await send("reset")
                elif ctype in {"flush", "end"}:
                    audio = endpointer.flush()
                    if audio:
                        await send("speech_end", speech_ms=0)
                        await handle_turn(audio)
                    if ctype == "end":
                        await send("done")
                        break
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass
