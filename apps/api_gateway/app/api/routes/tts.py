import logging
import time

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile

from app.core.actor import current_user_id
from app.core.audio import wav_duration_seconds
from app.core.upload_limits import REFERENCE_AUDIO_MAX_BYTES
from app.schemas.tts import TTSRequest
from app.services.artifacts import artifact_store
from app.services.model_registry.store import model_registry_store
from app.services.tts.service import tts_service
from app.services.usage.attribution import resolve_usage_model
from app.services.usage.recorder import record_usage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/tts", tags=["tts"])

# Size number + reasoning live in upload_limits.py (shared with main.py's
# UploadSizeLimitMiddleware, which enforces the same cap at the ASGI layer,
# before Starlette's multipart parser ever runs -- see that middleware's
# docstring for why this route can't rely on the chunked read below alone).
# Kept as a module attribute here (not inlined) so it stays monkeypatchable
# by tests without reaching into main.py's middleware stack.
_MAX_REFERENCE_AUDIO_BYTES = REFERENCE_AUDIO_MAX_BYTES
_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


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
    ArtifactStore.save_reference_audio.

    The real ceiling for this route lives one layer up: main.py's
    UploadSizeLimitMiddleware caps the body at the ASGI level, before
    Starlette's multipart parser (which drives `audio: UploadFile`) ever
    runs -- because by the time THIS function starts executing, the whole
    multipart body has already been received and spooled by that parser
    regardless of anything done here. Read in bounded chunks (not a single
    `await audio.read()`) and the running total checked against
    _MAX_REFERENCE_AUDIO_BYTES as a defense-in-depth backstop -- it still
    guards against a single unbounded in-memory `bytes` blowup for whatever
    made it through the middleware. See H3 in
    docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md and
    upload_size_limit.py's module docstring for the full reasoning."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await audio.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_REFERENCE_AUDIO_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    "reference audio exceeds the "
                    f"{_MAX_REFERENCE_AUDIO_BYTES // (1024 * 1024)}MB limit"
                ),
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    ref_id, _url = artifact_store.save_reference_audio(data)
    return {"success": True, "data": {"ref_audio_path": str(artifact_store.path_for(ref_id))}}


@router.post("/synthesize")
async def synthesize(payload: TTSRequest, request: Request, profile: str | None = None) -> Response:
    # Quota pre-flight: block BEFORE the provider does any work. See the STT
    # route for why the model is resolved before the provider lookup.

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
        # function-local: tests monkeypatch app.services.quota.gate.quota_gate by
        # reassigning the module attribute (see test_stt_stream_metering.py's
        # counting_gate); a top-level `from ... import quota_gate` binds the name
        # once at import time and never observes that reassignment.
        from app.services.quota.gate import QuotaExceededError, quota_gate

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
