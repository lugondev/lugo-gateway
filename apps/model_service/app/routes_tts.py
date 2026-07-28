"""OpenAI-compatible speech endpoint over a gateway TTS provider.

`response_format` is accepted but ignored: only RenderingTTSProvider engines
are servable and they all produce WAV.
"""

from __future__ import annotations

import base64
import os
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.schemas.tts import TTSRequest
from app.services.artifacts import artifact_store
from app.services.tts.base import RenderingTTSProvider
from model_service.app.auth import make_auth_dependency
from model_service.app.config import ServiceConfig


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
                with open(tmp_ref_path, "wb") as f:
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
