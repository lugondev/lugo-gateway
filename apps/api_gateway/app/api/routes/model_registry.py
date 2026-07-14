from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.schemas.tts import TTSRequest
from app.services.conversation.responder import OpenAICompatResponder
from app.services.model_registry.store import model_registry_store
from app.services.stt.providers.openrouter_provider import OpenRouterSttProvider
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service

router = APIRouter(prefix="/v1/model_registry", tags=["model_registry"])

# Short silence buffer, same shape as other STT test fixtures in this codebase
# (raw 16-bit PCM, mono) -- enough to exercise the provider without needing a
# real recorded sample.
_SAMPLE_PCM16 = b"\x00\x00" * 1600

# OpenRouter-backed STT engines: each Model Registry entry carries its own
# api_key (no system-wide OpenRouter key), so the add-time test call must use
# the key the admin is submitting right now rather than the fixed singleton
# provider (which looks its key up from an entry that doesn't exist yet).
_OPENROUTER_STT_ENGINES = {"qwen3_asr_or", "whisper_or"}


def _mask_api_key(key: str) -> str:
    """Partial reveal (e.g. 'sk-or-v1-cae...363') so an admin managing several
    models/keys can tell which key is attached to which entry at a glance --
    unlike the full '***' mask used for the single system-wide secrets."""
    if not key:
        return ""
    if len(key) <= 15:
        return "***"
    return f"{key[:12]}...{key[-3:]}"


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
    api_key: str | None = None
    base_url: str | None = None


@router.get("")
async def list_entries() -> dict:
    entries = await model_registry_store.list_all()
    for e in entries:
        e["api_key"] = _mask_api_key(e["api_key"])
    return {"success": True, "data": entries}


@router.post("")
async def create_entry(payload: CreateEntryRequest) -> dict:
    try:
        if payload.kind == "stt":
            if payload.engine in _OPENROUTER_STT_ENGINES:
                provider = OpenRouterSttProvider(
                    name=payload.engine, model=payload.model_id, api_key=payload.api_key
                )
            else:
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

    # Persist api_key for every kind (stt: read by openrouter_provider.py;
    # llm: read by responder.py's resolve_llm_override_from_registry; tts: no
    # engine reads it yet -- stored for UI/schema consistency and ready for a
    # future key-requiring TTS engine). base_url only matters for llm (an
    # OpenAI-compatible endpoint paired with this entry's model/key).
    created = await model_registry_store.create(
        payload.kind, payload.engine, payload.model_id, payload.label, stage=payload.stage,
        api_key=payload.api_key,
        base_url=payload.base_url if payload.kind == "llm" else "",
    )
    created["api_key"] = _mask_api_key(created["api_key"])
    return {"success": True, "data": created}


@router.patch("/{entry_id}")
async def update_entry(entry_id: str, payload: UpdateEntryRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "api_key" in fields and not fields["api_key"]:
        # Blank means "keep the existing key" -- same convention as every other
        # secret field in this app (the UI never pre-fills a real key, so a
        # blank submit can only mean "didn't type a new one").
        del fields["api_key"]
    updated = await model_registry_store.set_fields(entry_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"model registry entry '{entry_id}' not found")
    updated["api_key"] = _mask_api_key(updated["api_key"])
    return {"success": True, "data": updated}
