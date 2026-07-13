from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.schemas.tts import TTSRequest
from app.services.conversation.responder import OpenAICompatResponder
from app.services.model_registry.store import model_registry_store
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service

router = APIRouter(prefix="/v1/model_registry", tags=["model_registry"])

# Short silence buffer, same shape as other STT test fixtures in this codebase
# (raw 16-bit PCM, mono) -- enough to exercise the provider without needing a
# real recorded sample.
_SAMPLE_PCM16 = b"\x00\x00" * 1600


class CreateEntryRequest(BaseModel):
    kind: str
    engine: str
    model_id: str
    label: str
    stage: str = "stable"
    base_url: str = ""
    api_key: str = ""
    sample_text: str = "xin chào"


class UpdateEntryRequest(BaseModel):
    enabled: bool | None = None
    stage: str | None = None


@router.get("")
async def list_entries() -> dict:
    return {"success": True, "data": await model_registry_store.list_all()}


@router.post("")
async def create_entry(payload: CreateEntryRequest) -> dict:
    try:
        if payload.kind == "stt":
            provider = stt_service.get_provider(payload.engine)
            await provider.transcribe_bytes(_SAMPLE_PCM16)
        elif payload.kind == "tts":
            provider = tts_service.get_provider(payload.engine)
            await provider.synthesize(TTSRequest(text=payload.sample_text, engine=payload.engine))
        elif payload.kind == "llm":
            responder = OpenAICompatResponder(
                base_url=payload.base_url, api_key=payload.api_key, model=payload.model_id,
                system_prompt="", timeout=30.0,
            )
            await responder.reply([{"role": "user", "content": payload.sample_text}])
        else:
            raise HTTPException(status_code=400, detail=f"unknown kind '{payload.kind}'")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface the provider's own error to the admin
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    created = await model_registry_store.create(
        payload.kind, payload.engine, payload.model_id, payload.label, stage=payload.stage
    )
    return {"success": True, "data": created}


@router.patch("/{entry_id}")
async def update_entry(entry_id: str, payload: UpdateEntryRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = await model_registry_store.set_fields(entry_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"model registry entry '{entry_id}' not found")
    return {"success": True, "data": updated}
