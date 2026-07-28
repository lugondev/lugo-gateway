import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile

from app.core.actor import current_user_id
from app.core.audio import wav_duration_seconds
from app.schemas.common import StreamEvent
from app.schemas.tts import TTSRequest
from app.services.artifacts import artifact_store
from app.services.tts.segmenter import segment_text
from app.services.tts.service import tts_service
from app.services.usage.recorder import record_usage
from app.streaming.event_bus import event_bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/tts", tags=["tts"])

# Strong references to running stream jobs: asyncio only keeps a weak ref to
# tasks, so a fire-and-forget create_task can be GC'd mid-synthesis.
_stream_jobs: set[asyncio.Task] = set()

# Who created each stream job -- checked by GET /v1/events/jobs/{job_id} so one
# user cannot subscribe to another user's TTS result stream. There is no
# persistent job store (a job is just an asyncio task + an event-bus channel),
# so this in-memory map is the only record of ownership. Bounded FIFO eviction,
# same shape as InMemoryEventBus._closed, since a job's relevant lifetime is
# about the same as its channel's.
_job_owners: dict[str, str] = {}
_JOB_OWNERS_LIMIT = 4096


def _record_job_owner(job_id: str, user_id: str) -> None:
    _job_owners[job_id] = user_id
    while len(_job_owners) > _JOB_OWNERS_LIMIT:
        _job_owners.pop(next(iter(_job_owners)))


def get_job_owner(job_id: str) -> str | None:
    return _job_owners.get(job_id)


@router.get("/engines")
async def list_tts_engines() -> dict:
    return {"success": True, "data": tts_service.list_engines()}


@router.get("/voices")
async def list_tts_voices(engine: str = "vieneu") -> dict:
    provider = tts_service.get_provider(engine)
    voices = await provider.list_voices()
    supports_clone = await provider.supports_voice_clone()
    return {"success": True, "data": {"voices": voices, "supports_clone": supports_clone}}


@router.post("/reference-audio")
async def upload_reference_audio(audio: UploadFile = File(...)) -> dict:
    """Save a voice-clone reference clip; the returned ref_audio_path is what
    a TtsProfile's voice_mode="clone" stores (see profile_models.py). Persists
    like OmniVoice's pinned reference -- not swept by artifact prune, see
    ArtifactStore.save_reference_audio."""
    data = await audio.read()
    ref_id, _url = artifact_store.save_reference_audio(data)
    return {"success": True, "data": {"ref_audio_path": str(artifact_store.path_for(ref_id))}}


@router.post("/synthesize")
async def synthesize(payload: TTSRequest, request: Request, profile: str | None = None) -> Response:
    # Quota pre-flight: block BEFORE the provider does any work. See the STT
    # route for why the model is resolved before the provider lookup.
    from app.services.model_registry.store import model_registry_store
    from app.services.quota.gate import quota_gate, QuotaExceededError
    from app.services.usage.attribution import resolve_usage_model

    provider_id = ""
    try:
        # Inside the guard for the same reason as the STT route: resolve_usage_model()
        # never raises from its own logic, but its function-level import of the
        # registry store isn't covered by that, and an ImportError there would 500
        # a request this gate is required to fail open on.
        usage_engine, usage_model_id = await resolve_usage_model("tts", payload.engine, payload.model_id or "")
        entry = await model_registry_store.find("tts", usage_engine, usage_model_id)
        provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
    except Exception:  # noqa: BLE001 - a registry hiccup must never block a request
        usage_engine, usage_model_id, provider_id = "", "", ""
    try:
        await quota_gate(
            user_id=current_user_id(request) or "", provider_id=provider_id,
            kind="tts", engine=usage_engine, model_id=usage_model_id,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    provider = tts_service.get_provider(payload.engine)
    started = time.perf_counter()
    audio_bytes, media_type = await provider.render_audio(payload)
    process_seconds = round(time.perf_counter() - started, 3)
    try:
        # model_id may be "" when the caller omits it: cost resolution then finds
        # no pricing row and resolves to $0, but the usage event is still
        # recorded/attributed -- $0 here is expected, not a bug.
        await record_usage(
            user_id=current_user_id(request) or "", profile_id=profile or "",
            kind="tts", engine=payload.engine, model_id=payload.model_id or "",
            unit="chars", native_amount=len(payload.text or ""),
        )
    except Exception as exc:  # noqa: BLE001 - metering must never break the response
        logger.warning("tts usage metering failed: %s", exc)

    headers = {
        "X-TTS-Engine": provider.name,
        "X-TTS-Sample-Rate": str(getattr(provider, "sample_rate", 0)),
        "X-TTS-Process-Seconds": str(process_seconds),
    }
    # Duration is computed exactly for WAV; for other containers (e.g. edge_tts's
    # MP3) omit the header rather than guessing -- a wrong number is worse than
    # a missing one.
    if media_type == "audio/wav":
        headers["X-TTS-Duration-Seconds"] = str(round(wav_duration_seconds(audio_bytes), 3))
    return Response(content=audio_bytes, media_type=media_type, headers=headers)


@router.post("/stream")
async def create_stream_job(payload: TTSRequest, request: Request, profile: str | None = None) -> dict:
    # Quota pre-flight, synchronous: this endpoint returns a job_id and streams
    # over SSE, so a refusal has to happen here -- reporting it through the event
    # channel would make every client learn a second failure path. Same 429
    # contract as /v1/tts/synthesize.
    from app.services.model_registry.store import model_registry_store
    from app.services.quota.gate import quota_gate, QuotaExceededError
    from app.services.usage.attribution import resolve_usage_model

    usage_engine, usage_model_id = "", ""
    provider_id = ""
    try:
        usage_engine, usage_model_id = await resolve_usage_model(
            "tts", payload.engine, payload.model_id or ""
        )
        entry = await model_registry_store.find("tts", usage_engine, usage_model_id)
        provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
    except Exception:  # noqa: BLE001 - a registry hiccup must never block a request
        usage_engine, usage_model_id, provider_id = "", "", ""
    # Read the identity HERE: the background job outlives the request, and the
    # Request object must not be touched from inside it.
    caller_id = current_user_id(request) or ""
    profile_id = profile or ""
    try:
        await quota_gate(
            user_id=caller_id, provider_id=provider_id,
            kind="tts", engine=usage_engine, model_id=usage_model_id,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    job_id = str(uuid.uuid4())
    _record_job_owner(job_id, caller_id)
    # Resolve the provider eagerly so an unknown engine returns 400 synchronously.
    provider = tts_service.get_provider(payload.engine)
    channel = f"job:{job_id}"

    async def _run() -> None:
        sequence = 1
        try:
            segments = segment_text(payload.text)
            await event_bus.publish(
                channel,
                StreamEvent(
                    event_type="queued",
                    job_id=job_id,
                    sequence=sequence,
                    payload={"text": payload.text, "total_chunks": len(segments)},
                ),
            )

            for index, segment in enumerate(segments):
                chunk_request = payload.model_copy(update={"text": segment})
                started = time.perf_counter()
                result = await provider.synthesize(chunk_request)
                process_seconds = round(time.perf_counter() - started, 3)
                try:
                    await record_usage(
                        user_id=caller_id, profile_id=profile_id,
                        kind="tts", engine=payload.engine, model_id=payload.model_id or "",
                        unit="chars", native_amount=len(segment or ""),
                    )
                except Exception as exc:  # noqa: BLE001 - metering must never break the job
                    logger.warning("tts stream usage metering failed: %s", exc)
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
                            "process_seconds": process_seconds,
                        },
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
                    payload={"message": "tts failed"},
                ),
            )
        finally:
            # Idempotent backstop for paths no publish above covers --
            # cancellation mid-synthesis, a crash inside the error handler --
            # so the channel's replay history never leaks and late SSE
            # subscribers always get end-of-stream instead of hanging.
            event_bus.close(channel)

    task = asyncio.create_task(_run(), name=f"tts-stream-{job_id}")
    _stream_jobs.add(task)
    task.add_done_callback(_stream_jobs.discard)
    return {"success": True, "data": {"job_id": job_id}}
