import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.providers.resolve import PROVIDER_PRESETS
from app.services.providers.store import provider_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/providers", tags=["providers"])


def _mask_api_key(key: str) -> str:
    """Partial reveal so an admin can tell which key is which at a glance
    (same convention as routes/model_registry.py::_mask_api_key)."""
    if not key:
        return ""
    if len(key) <= 15:
        return "***"
    return f"{key[:12]}...{key[-3:]}"


def _masked(entry: dict) -> dict:
    entry = dict(entry)
    entry["api_key"] = _mask_api_key(entry["api_key"])
    return entry


class CreateProviderRequest(BaseModel):
    name: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    enabled: bool = True
    config: dict = {}


class UpdateProviderRequest(BaseModel):
    name: str | None = None
    label: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    config: dict | None = None


@router.get("")
async def list_providers() -> dict:
    return {"success": True, "data": [_masked(p) for p in await provider_store.list_all()]}


@router.get("/presets")
async def list_presets() -> dict:
    return {"success": True, "data": PROVIDER_PRESETS}


@router.post("")
async def create_provider(payload: CreateProviderRequest) -> dict:
    created = await provider_store.create(
        name=payload.name, label=payload.label, base_url=payload.base_url,
        api_key=payload.api_key, enabled=payload.enabled, config=payload.config,
    )
    return {"success": True, "data": _masked(created)}


@router.patch("/{provider_id}")
async def update_provider(provider_id: str, payload: UpdateProviderRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    # Blank api_key means "keep existing" -- same convention as the secret
    # fields in routes/model_registry.py (UI never pre-fills a real key).
    if "api_key" in fields and not fields["api_key"]:
        del fields["api_key"]
    updated = await provider_store.set_fields(provider_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' not found")
    return {"success": True, "data": _masked(updated)}


@router.delete("/{provider_id}")
async def delete_provider(provider_id: str) -> dict:
    if not await provider_store.delete(provider_id):
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' not found")
    return {"success": True, "data": {"id": provider_id, "deleted": True}}


def _parse_models(payload) -> list[str]:
    """Extract model ids from an OpenAI-compatible /models response.
    Accepts {"data":[{"id":...}]}, {"models":[...]}, or a bare list. Dedupes,
    preserves order, drops blanks/non-str."""
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None:
            items = payload.get("models", [])
    else:
        items = payload
    out, seen = [], set()
    for it in items or []:
        mid = it.get("id") if isinstance(it, dict) else it
        if isinstance(mid, str) and mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


async def _fetch_provider_models(base_url: str, api_key: str) -> tuple[list[str], str | None]:
    """GET {base_url}/models (OpenAI-compat). Best-effort: never raises."""
    if not base_url:
        return [], "provider has no base_url"
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return [], f"provider returned HTTP {resp.status_code}"
        return _parse_models(resp.json()), None
    except Exception as exc:  # noqa: BLE001 - best-effort discovery
        logger.warning("provider /models fetch failed (%s): %s", url, exc)
        return [], str(exc)


@router.get("/{provider_id}/models")
async def list_provider_models(provider_id: str) -> dict:
    """Best-effort list of the provider's available model ids (from its
    OpenAI-compatible /models). Never 500s: on any fetch error returns an empty
    list + an error message so the UI falls back to manual entry."""
    provider = await provider_store.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' not found")
    models, error = await _fetch_provider_models(provider["base_url"], provider["api_key"])
    return {"success": True, "data": {"models": models, "error": error}}
