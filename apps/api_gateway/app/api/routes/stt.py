import json
import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from app.core.audio import preprocess_pcm16, preprocess_wav_bytes
from app.core.errors import AppError
from app.core.settings import settings
from app.schemas.common import StreamEvent
from app.schemas.stt import STTRequest, STTResult
from app.services.stt.base import STTStream
from app.services.stt.service import stt_service
from app.services.vad import apply_vad
from app.streaming.event_bus import event_bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/stt", tags=["stt"])


def _resolve_flag(value: bool | None, default: bool) -> bool:
    return default if value is None else value


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    engine: str = Form(default=settings.default_stt_engine),
    language: str | None = Form(default=None),
    denoise: bool | None = Form(default=None),
    vad: bool | None = Form(default=None),
    vad_backend: str | None = Form(default=None),
) -> dict:
    payload = STTRequest(engine=engine, language=language)
    provider = stt_service.get_provider(payload.engine)
    audio_bytes = await audio.read()

    backend = vad_backend or settings.stt_vad_backend
    audio_bytes = preprocess_wav_bytes(
        audio_bytes,
        denoise=_resolve_flag(denoise, settings.stt_noise_reduce_enabled),
        vad=_resolve_flag(vad, settings.stt_vad_enabled),
        amount=settings.stt_noise_reduce_amount,
        vad_fn=lambda s, sr: apply_vad(s, sr, backend),
    )

    try:
        result = await provider.transcribe_bytes(audio_bytes, payload.language)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "data": result.model_dump()}


@router.get("/engines")
async def list_stt_engines() -> dict:
    return {"success": True, "data": stt_service.list_engines()}


async def _emit(websocket: WebSocket, channel: str, event: StreamEvent) -> None:
    await websocket.send_json(event.model_dump(mode="json"))
    await event_bus.publish(channel, event)


@router.websocket("/stream")
async def stt_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    session_id = str(uuid.uuid4())
    channel = f"session:{session_id}"
    sequence = 0

    engine = websocket.query_params.get("engine", settings.default_stt_engine)
    language = websocket.query_params.get("language")
    sample_rate = int(
        websocket.query_params.get("sample_rate", settings.stt_stream_sample_rate)
    )
    denoise = _resolve_flag(
        _parse_bool(websocket.query_params.get("denoise")), settings.stt_noise_reduce_enabled
    )
    vad = _resolve_flag(
        _parse_bool(websocket.query_params.get("vad")), settings.stt_vad_enabled
    )

    try:
        provider = stt_service.get_provider(engine)
        stream: STTStream = provider.open_stream(sample_rate, language)
    except (AppError, RuntimeError) as exc:
        await websocket.send_json(
            {"event_type": "error", "session_id": session_id, "payload": {"message": str(exc)}}
        )
        await websocket.close()
        return

    await websocket.send_json(
        {"event_type": "session_started", "session_id": session_id, "sample_rate": sample_rate}
    )

    async def _emit_result(result: STTResult) -> None:
        nonlocal sequence
        sequence += 1
        await _emit(
            websocket,
            channel,
            StreamEvent(
                event_type="final" if result.is_final else "partial",
                session_id=session_id,
                sequence=sequence,
                payload=result.model_dump(),
            ),
        )

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                frame = message["bytes"]
                if denoise or vad:
                    frame = preprocess_pcm16(
                        frame, sample_rate, denoise=denoise, vad=vad,
                        amount=settings.stt_noise_reduce_amount,
                    )
                try:
                    results = await stream.accept(frame)
                except RuntimeError as exc:
                    sequence += 1
                    await _emit(
                        websocket,
                        channel,
                        StreamEvent(
                            event_type="error",
                            session_id=session_id,
                            sequence=sequence,
                            payload={"message": str(exc)},
                        ),
                    )
                    continue
                for result in results:
                    await _emit_result(result)

            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {"type": "unknown"}

                control_type = control.get("type")
                if control_type in {"flush", "end"}:
                    try:
                        final = await stream.finalize()
                    except RuntimeError as exc:
                        sequence += 1
                        await _emit(
                            websocket,
                            channel,
                            StreamEvent(
                                event_type="error",
                                session_id=session_id,
                                sequence=sequence,
                                payload={"message": str(exc)},
                            ),
                        )
                        final = None
                    if final is not None:
                        await _emit_result(final)

                if control_type == "end":
                    sequence += 1
                    await _emit(
                        websocket,
                        channel,
                        StreamEvent(
                            event_type="done",
                            session_id=session_id,
                            sequence=sequence,
                            payload={"message": "stream ended"},
                        ),
                    )
                    break

    except WebSocketDisconnect:
        pass
    finally:
        event_bus.close(channel)
        try:
            await websocket.close()
        except RuntimeError:
            pass
