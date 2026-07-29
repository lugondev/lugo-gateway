"""OpenAI-compatible speech endpoint over a gateway TTS provider.

`response_format` is accepted but ignored: only RenderingTTSProvider engines
are servable and they all produce WAV.
"""

from __future__ import annotations

import base64
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.upload_limits import REFERENCE_AUDIO_MAX_BYTES
from app.schemas.tts import TTSRequest
from app.services.artifacts import artifact_store
from app.services.tts.base import RenderingTTSProvider
from model_service.app.auth import make_auth_dependency
from model_service.app.config import ServiceConfig

# base64 expands 3 raw bytes into 4 encoded chars; bound the ENCODED string
# length (rather than decoding first and checking the result) so an
# oversized payload never gets fully materialized in memory just to be
# rejected. This endpoint sits behind the service bearer token (only the
# gateway or a token holder reaches it -- see make_auth_dependency below),
# so this is defense-in-depth, not the primary control; it reuses the same
# reference-audio cap the gateway enforces on the multipart upload path
# (app/core/upload_limits.py) so the two limits can't silently drift apart.
# +4 for base64 padding slop rather than computing the exact ceil().
_REF_AUDIO_BASE64_MAX_CHARS = (REFERENCE_AUDIO_MAX_BYTES * 4 // 3) + 4


class SpeechRequest(BaseModel):
    input: str = Field(..., min_length=1)
    model: str = ""
    voice: str | None = None
    speed: float | None = None
    language: str | None = None
    response_format: str = "wav"
    # Voice-clone reference clip. A filesystem path is meaningless across the
    # gateway<->model_service network hop, so the caller (HttpTtsProvider)
    # sends the actual bytes instead; create_speech below decodes them to a
    # local temp file before handing off to the (in-process, same-host) provider.
    ref_audio_base64: str | None = None
    ref_text: str | None = None


def build_tts_router(config: ServiceConfig, provider: RenderingTTSProvider) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(make_auth_dependency(config.api_token))])

    @router.get("/models")
    async def list_models() -> dict:
        return {"object": "list", "data": [{"id": config.engine, "object": "model", "owned_by": "local"}]}

    @router.get("/voices")
    async def list_voices() -> dict:
        """The self-described capability schema HttpTtsProvider fetches
        so the gateway knows what this deployed engine supports without any
        per-engine hardcoding on the gateway side."""
        return {
            "object": "list",
            "data": await provider.list_voices(),
            "supports_clone": await provider.supports_voice_clone(),
        }

    @router.post("/audio/speech")
    async def create_speech(payload: SpeechRequest) -> Response:
        tmp_ref_path = None
        try:
            if payload.ref_audio_base64:
                if len(payload.ref_audio_base64) > _REF_AUDIO_BASE64_MAX_CHARS:
                    raise HTTPException(
                        status_code=413,
                        detail="ref_audio_base64 exceeds the reference-audio size limit",
                    )
                ref_bytes = base64.b64decode(payload.ref_audio_base64)
                # TTSRequest.ref_audio_path is validated against
                # artifact_store's containment check (2026-07-28
                # critical-authz-fixes task 5) since six providers feed it
                # straight into Path(...).read_bytes(). This decode target is
                # server-generated, not caller-supplied, but it still has to
                # pass that check -- write it inside the artifacts dir
                # (rather than the system temp dir) so it does.
                #
                # Named `<uuid4 hex>.wav`, matching ArtifactStore's own
                # _ARTIFACT_FILENAME pattern, rather than tempfile's
                # `tmpXXXXXXXX.wav` -- a NamedTemporaryFile-style name is
                # never swept by prune(), so a crash between this write and
                # the os.unlink() below would leak the file forever (the
                # four local engines run natively from the repo root, where
                # artifacts_dir defaults to the real "artifacts" dir, not a
                # container-private /tmp). A random hex name is equally
                # unguessable if briefly fetchable at /artifacts/<name>.wav
                # in that native deployment, but collectable.
                tmp_ref_path = str(artifact_store.base_dir / f"{uuid.uuid4().hex}.wav")
                # os.open + O_EXCL, mode 0o600: a plain open(path, "wb") creates
                # the file at the umask default (usually 0644) -- readable by
                # anyone on the box -- for however long it sits in this
                # HTTP-served artifacts dir before the finally block's unlink.
                # NamedTemporaryFile (what this replaced) always created 0600;
                # match that instead of widening exposure. O_EXCL also means
                # this can never silently overwrite an existing file at a
                # colliding uuid4 name.
                fd = os.open(tmp_ref_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(ref_bytes)
            wav = await provider.render_wav(
                TTSRequest(
                    text=payload.input,
                    engine=config.engine,
                    voice=payload.voice,
                    speed=payload.speed,
                    language=payload.language,
                    ref_audio_path=tmp_ref_path,
                    ref_text=payload.ref_text,
                )
            )
        finally:
            if tmp_ref_path and os.path.isfile(tmp_ref_path):
                os.unlink(tmp_ref_path)
        return Response(content=wav, media_type="audio/wav")

    return router
