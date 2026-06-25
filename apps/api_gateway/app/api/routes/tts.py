import asyncio
import logging
import uuid

from fastapi import APIRouter

from app.schemas.common import StreamEvent
from app.schemas.tts import TTSRequest
from app.services.tts.segmenter import segment_text
from app.services.tts.service import tts_service
from app.streaming.event_bus import event_bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/tts", tags=["tts"])


@router.get("/engines")
async def list_tts_engines() -> dict:
    return {"success": True, "data": tts_service.list_engines()}


@router.get("/voices")
async def list_tts_voices(engine: str = "vieneu") -> dict:
    provider = tts_service.get_provider(engine)
    voices = provider.list_voices() if hasattr(provider, "list_voices") else []
    return {"success": True, "data": voices}


@router.post("/synthesize")
async def synthesize(payload: TTSRequest) -> dict:
    provider = tts_service.get_provider(payload.engine)
    result = await provider.synthesize(payload)
    return {"success": True, "data": result.model_dump()}


@router.post("/stream")
async def create_stream_job(payload: TTSRequest) -> dict:
    job_id = str(uuid.uuid4())
    # Resolve the provider eagerly so an unknown engine returns 400 synchronously.
    provider = tts_service.get_provider(payload.engine)
    channel = f"job:{job_id}"

    async def _run() -> None:
        segments = segment_text(payload.text)
        sequence = 1
        await event_bus.publish(
            channel,
            StreamEvent(
                event_type="queued",
                job_id=job_id,
                sequence=sequence,
                payload={"text": payload.text, "total_chunks": len(segments)},
            ),
        )

        try:
            for index, segment in enumerate(segments):
                chunk_request = payload.model_copy(update={"text": segment})
                result = await provider.synthesize(chunk_request)
                sequence += 1
                await event_bus.publish(
                    channel,
                    StreamEvent(
                        event_type="audio_chunk",
                        job_id=job_id,
                        sequence=sequence,
                        payload={
                            "chunk_index": index,
                            "text": segment,
                            "audio_url": result.audio_url,
                            "duration_seconds": result.duration_seconds,
                            "mock": result.mock,
                        },
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - surface failure to subscriber
            logger.exception("TTS stream job %s failed", job_id)
            sequence += 1
            await event_bus.publish(
                channel,
                StreamEvent(
                    event_type="error",
                    job_id=job_id,
                    sequence=sequence,
                    payload={"message": str(exc)},
                ),
            )

        await event_bus.publish(
            channel,
            StreamEvent(
                event_type="done",
                job_id=job_id,
                sequence=sequence + 1,
                payload={"message": "tts completed"},
            ),
        )

    asyncio.create_task(_run())
    return {"success": True, "data": {"job_id": job_id}}
