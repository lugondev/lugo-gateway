import json
import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect

from app.core.actor import current_user_id
from app.core.audio import (
    pcm16_to_float_array,
    preprocess_pcm16,
    preprocess_wav_bytes,
    read_wav,
    wav_duration_seconds,
)
from app.core.auth_guard import resolve_ws_identity, ws_subprotocol
from app.core.errors import AppError
from app.core.identity_watch import build_identity_watchdog, receive_with_watchdog
from app.core.settings import settings
from app.schemas.common import StreamEvent
from app.schemas.stt import STTRequest, STTResult
from app.services.stt.base import STTStream
from app.services.stt.segmented import transcribe_long
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.usage.recorder import record_usage
from app.services.vad import apply_vad
from app.streaming.event_bus import event_bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/stt", tags=["stt"])

# How often the disabled/revoked re-check wakes (test-tunable, same pattern as
# lugo.py's _IDLE_TICK_S).
_IDENTITY_RECHECK_INTERVAL_S = 30.0


def _resolve_flag(value: bool | None, default: bool) -> bool:
    return default if value is None else value


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


@router.post("/transcribe")
async def transcribe(
    request: Request,
    audio: UploadFile = File(...),
    engine: str | None = Form(default=None),
    language: str | None = Form(default=None),
    denoise: bool | None = Form(default=None),
    vad: bool | None = Form(default=None),
    vad_backend: str | None = Form(default=None),
    segment: bool | None = Form(default=None),
) -> dict:
    engine = engine or system_config_store.get().engines.default_stt_engine
    payload = STTRequest(engine=engine, language=language)
    provider = stt_service.get_provider(payload.engine)
    audio_bytes = await audio.read()

    preprocessing = system_config_store.get().preprocessing
    backend = vad_backend or preprocessing.stt_vad_backend
    audio_bytes = preprocess_wav_bytes(
        audio_bytes,
        denoise=_resolve_flag(denoise, preprocessing.stt_noise_reduce_enabled),
        vad=_resolve_flag(vad, preprocessing.stt_vad_enabled),
        amount=preprocessing.stt_noise_reduce_amount,
        vad_fn=lambda s, sr: apply_vad(s, sr, backend),
    )

    # Long clips: split on silence and transcribe segments in parallel (higher
    # throughput). Only when enabled and the clip is at/over the length threshold.
    engines = system_config_store.get().engines
    use_segment = _resolve_flag(segment, engines.stt_segment_long_enabled)
    try:
        if use_segment and wav_duration_seconds(audio_bytes) >= engines.stt_segment_min_seconds:
            pcm, sample_rate, _, _ = read_wav(audio_bytes)
            result = await transcribe_long(
                provider,
                pcm16_to_float_array(pcm),
                sample_rate,
                language=payload.language,
                concurrency=engines.stt_segment_concurrency,
            )
        else:
            # no per-session model here -- falls back to this engine's process-global
            # default by design (see app.services.stt.model_catalog)
            result = await provider.transcribe_bytes(audio_bytes, payload.language)
    except AppError:
        raise  # handled globally -> clean JSON
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - never leak a plain-text 500 to the client
        logger.exception("STT transcribe failed (%s)", payload.engine)
        raise HTTPException(status_code=500, detail=f"STT failed ({payload.engine}): {exc}") from exc
    data = result.model_dump()
    data["duration"] = wav_duration_seconds(audio_bytes)
    try:
        await record_usage(
            user_id=current_user_id(request) or "", profile_id="",
            kind="stt", engine=payload.engine, model_id="",
            unit="seconds", native_amount=data["duration"],
        )
    except Exception as exc:  # noqa: BLE001 - metering must never break the response
        logger.warning("stt usage metering failed: %s", exc)
    return {"success": True, "data": data}


@router.get("/engines")
async def list_stt_engines() -> dict:
    return {"success": True, "data": await stt_service.list_engines()}


@router.get("/models")
async def list_stt_models(engine: str) -> dict:
    from app.services.stt.model_catalog import STT_MODEL_CATALOGS

    registry = STT_MODEL_CATALOGS.get(engine)
    if registry is None:
        return {"success": True, "data": {"engine": engine, "supports_variants": False, "models": []}}
    models = [{**m, "valid": True} for m in registry.list_models()]
    return {"success": True, "data": {"engine": engine, "supports_variants": True, "models": models}}


@router.post("/warm")
async def warm_engine(engine: str | None = None, profile: str | None = None, model: str | None = None) -> dict:
    """Load a heavy STT model into memory ahead of use (e.g. Whisper large ~20s).

    Lets the UI preload before the first conversation turn so it isn't a cold wait.
    Pass ?engine= to warm a specific engine, or ?profile= to warm whichever engine
    (and model, if the profile pins one) that profile resolves to (so a device that
    only knows its profile can pre-warm the right model, using the same resolution
    as the conversation stream). Pass ?model= to override the model explicitly
    regardless of profile. With neither engine nor profile, the server-wide default
    engine is warmed.
    """
    import asyncio

    from app.services.profiles.store import profile_store
    from app.services.stt.model_catalog import apply_stt_model
    from app.services.stt.profile import resolve_stt

    if not engine:
        prof = profile_store.get(profile) if profile else None
        engine, _, resolved_model = resolve_stt(prof)
        model = model or resolved_model
    if model:
        try:
            apply_stt_model(engine, model)
        except AppError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    provider = stt_service.get_provider(engine)
    warm = getattr(provider, "warm", None)
    if callable(warm):
        await asyncio.to_thread(warm)
    return {"success": True, "data": {"engine": engine, "model": model or None, "warmed": callable(warm)}}


async def _emit(websocket: WebSocket, channel: str, event: StreamEvent) -> None:
    await websocket.send_json(event.model_dump(mode="json"))
    await event_bus.publish(channel, event)


@router.websocket("/stream")
async def stt_stream(websocket: WebSocket) -> None:
    identity = await resolve_ws_identity(websocket)
    if identity is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept(subprotocol=ws_subprotocol(websocket))
    session_id = str(uuid.uuid4())
    channel = f"session:{session_id}"
    sequence = 0

    engine = websocket.query_params.get("engine", system_config_store.get().engines.default_stt_engine)
    language = websocket.query_params.get("language")
    sample_rate = int(
        websocket.query_params.get(
            "sample_rate", settings.stt_stream_sample_rate
        )
    )
    preprocessing = system_config_store.get().preprocessing
    denoise = _resolve_flag(
        _parse_bool(websocket.query_params.get("denoise")), preprocessing.stt_noise_reduce_enabled
    )
    vad = _resolve_flag(
        _parse_bool(websocket.query_params.get("vad")), preprocessing.stt_vad_enabled
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

    watchdog = build_identity_watchdog(identity, interval_s=_IDENTITY_RECHECK_INTERVAL_S)
    if watchdog is not None:
        watchdog.start()

    try:
        async for message in receive_with_watchdog(websocket, watchdog):
            if message is None:
                await websocket.close(code=4401, reason="account disabled")
                break

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                frame = message["bytes"]
                if denoise or vad:
                    frame = preprocess_pcm16(
                        frame, sample_rate, denoise=denoise, vad=vad,
                        amount=preprocessing.stt_noise_reduce_amount,
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
        if watchdog is not None:
            watchdog.cancel()
        event_bus.close(channel)
        try:
            await websocket.close()
        except RuntimeError:
            pass
