import difflib
import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_user_id
from app.core.audio import pcm16_to_wav_bytes
from app.services.auth.users import user_store
from app.schemas.model_registry import BulkPriceRequest, CreateEntryRequest, UpdateEntryRequest
from app.schemas.tts import TTSRequest
from app.services.conversation.responder import OpenAICompatResponder
from app.services.model_registry.availability import is_artifact_installed
from app.services.model_registry.cascade import clear_bindings_for
from app.services.model_registry.config_schema import config_schema_for
from app.services.model_registry.store import model_registry_store
from app.services.providers.resolve import resolve_credentials
from app.services.usage.price_schema import PRICE_UNIT_BY_KIND, apply_price_to_config
from app.services.usage.recorder import record_usage
from app.services.stt.providers.http_stt_provider import HttpSttProvider
from app.services.stt.providers.openrouter_provider import OpenRouterSttProvider
from app.services.stt.providers.qwencloud_provider import QwenCloudSttProvider
from app.services.stt.service import stt_service
from app.services.tts.providers.http_tts_provider import HttpTtsProvider
from app.services.tts.service import tts_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/model_registry", tags=["model_registry"])

# Short silence WAV, same shape as other STT test fixtures in this codebase --
# enough to exercise the provider without needing a real recorded sample. Must
# be a real WAV container (not bare PCM): remote HTTP providers (OpenRouter,
# whisper_service) upload/declare this as an actual .wav file, which a strict
# backend will reject if it's just headerless PCM.
_SAMPLE_WAV = pcm16_to_wav_bytes(b"\x00\x00" * 1600, sample_rate=16000)

# OpenRouter-backed STT engines: each Model Registry entry carries its own
# api_key (no system-wide OpenRouter key), so the add-time test call must use
# the key the admin is submitting right now rather than the fixed singleton
# provider (which looks its key up from an entry that doesn't exist yet).
_OPENROUTER_STT_ENGINES = {"qwen3_asr_or", "whisper_or"}

# STT engines that ARE services but hit a fixed vendor endpoint with a default
# base_url -- api_key only, no admin-supplied base_url required (like OpenRouter).
_FIXED_ENDPOINT_STT_ENGINES = _OPENROUTER_STT_ENGINES | {"qwencloud"}

# Service engines whose config lives entirely on the entry being submitted
# (base_url + api_key). Like the OpenRouter engines, the singleton provider
# would look up a row that doesn't exist yet, so the add-time test call gets an
# explicit entry built from the payload.
_SERVICE_STT_ENGINES = {"http_stt"}
_SERVICE_TTS_ENGINES = {"http_tts"}

# base_url-driven remote STT services that read their endpoint from the entry
# but use RemoteWhisperProvider (rebuilt via reinit_remote_providers on edit),
# not the payload-built HttpSttProvider -- so they're "service" for
# locality/base_url purposes but stay OUT of _SERVICE_STT_ENGINES, which only
# selects the add-time test-call provider.
_BASE_URL_STT_ENGINES = {"whisper_service", "eventlab"}


def _mask_api_key(key: str) -> str:
    """Partial reveal (e.g. 'sk-or-v1-cae...363') so an admin managing several
    models/keys can tell which key is attached to which entry at a glance --
    unlike the full '***' mask used for the single system-wide secrets."""
    if not key:
        return ""
    if len(key) <= 15:
        return "***"
    return f"{key[:12]}...{key[-3:]}"


def _location(kind: str, engine: str) -> str:
    """Two-state locality, surfaced so the admin UI can label each entry:

    - "local": runs in-process, no network call at all (whisper, vosk,
      qwen3_asr, omnivoice, vieneu, edge_tts, qwen3_tts_*, voxcpm2, ...).
    - "service": calls out to an external HTTP API -- http_stt/http_tts,
      whisper_service/eventlab, the OpenRouter-backed STT engines
      (qwen3_asr_or/whisper_or), every kind="llm" entry, and every
      kind="embed" entry (an OpenAI-compatible `/embeddings` host). OpenRouter,
      OpenAI, Together, ... are all just "service"; there is no third bucket.

    Whether a service needs a *configurable* base_url is a separate axis --
    see _requires_base_url (OpenRouter hits a fixed endpoint, api_key only).
    """
    if (
        kind in ("llm", "embed")
        or engine in _SERVICE_STT_ENGINES
        or engine in _SERVICE_TTS_ENGINES
        or engine in _BASE_URL_STT_ENGINES
        or engine in _OPENROUTER_STT_ENGINES
        or engine == "qwencloud"
    ):
        return "service"
    return "local"


def _requires_base_url(kind: str, engine: str) -> bool:
    """Whether the admin must configure a base_url. True for the service
    engines whose endpoint is admin-supplied (http_stt/http_tts,
    whisper_service/eventlab, every kind="llm" entry). False for the
    OpenRouter-backed STT engines -- "service", but they hit a fixed endpoint
    with api_key only -- and for every "local" engine. Surfaced in the list
    response so the admin UI can tell "blank because it's genuinely not needed"
    from "blank because it's misconfigured"."""
    return _location(kind, engine) == "service" and engine not in _FIXED_ENDPOINT_STT_ENGINES


def _validate_known_engine(kind: str, engine: str) -> None:
    """Reject an unknown stt/tts engine before the network test-call, with a
    "did you mean" hint. The Engine field is free text (kind="llm" genuinely
    needs that -- any label is valid there), so nothing upstream stops an
    admin from typing e.g. "OR" meaning "OpenRouter" when the real engine
    strings are the full compound names qwen3_asr_or / whisper_or. Without
    this, that typo used to surface only as a raw provider-dict KeyError from
    deep inside get_provider(), which named the bad value but not the fix."""
    valid = {"stt": stt_service.providers, "tts": tts_service.providers}.get(kind)
    if valid is None or engine in valid:
        return
    engine_lower = engine.strip().lower()
    substr_matches = sorted(name for name in valid if engine_lower and engine_lower in name.lower())
    if substr_matches:
        suggestions = substr_matches
    else:
        lower_to_name = {name.lower(): name for name in valid}
        close = difflib.get_close_matches(engine_lower, lower_to_name.keys(), n=3, cutoff=0.5)
        suggestions = [lower_to_name[c] for c in close]
    hint = f" -- did you mean {', '.join(repr(s) for s in suggestions)}?" if suggestions else ""
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported {kind} engine '{engine}'{hint} (valid: {', '.join(sorted(valid))})",
    )


def _engine_config_available(kind: str, engine: str, model_id: str) -> bool | None:
    """Whether the underlying package/binary/model is actually present, for a
    model_id="" engine-config sentinel row -- None for everything else (real
    model rows use artifact_installed instead).

    vosk/whisper have no `available()` on their provider -- list_engines()
    computes their availability inline from module/path checks instead, so
    those two are special-cased here with the exact same checks rather than
    left as None (which used to leave their toggle looking enabled/clickable
    even when genuinely not installed, same as every other local engine)."""
    if model_id:
        return None
    if kind == "stt" and engine == "vosk":
        import os

        from app.core.deps import module_available
        from app.services.stt.providers.vosk_provider import get_active_vosk_path

        return module_available("vosk") and os.path.isdir(get_active_vosk_path())
    if kind == "stt" and engine in ("whisper", "whisper_local"):
        from app.core.deps import module_available

        return module_available("faster_whisper")
    providers = {"stt": stt_service.providers, "tts": tts_service.providers}.get(kind)
    provider = providers.get(engine) if providers else None
    available_fn = getattr(provider, "available", None)
    if available_fn is None:
        return None
    try:
        return bool(available_fn())
    except Exception:  # noqa: BLE001 -- one bad engine must not 500 the whole list
        return False


@router.get("")
async def list_entries() -> dict:
    entries = await model_registry_store.list_all()
    for e in entries:
        e["api_key"] = _mask_api_key(e["api_key"])
        e["location"] = _location(e["kind"], e["engine"])
        e["requires_base_url"] = _requires_base_url(e["kind"], e["engine"])
        e["artifact_installed"] = is_artifact_installed(e["kind"], e["engine"], e["model_id"])
        e["engine_config_available"] = _engine_config_available(e["kind"], e["engine"], e["model_id"])
    return {"success": True, "data": entries}


@router.get("/config_schema")
async def get_config_schema(kind: str, engine: str) -> dict:
    """Fields the Config form should render for this (kind, engine). Describes a
    schema, not a stored entry -- never reads the DB. Empty for engines with no
    known config shape (the UI falls back to raw JSON)."""
    return {"fields": config_schema_for(kind, engine)}


# NOTE: /prices must stay ABOVE the "/{entry_id}" routes -- FastAPI matches in
# declaration order, so a later PATCH /{entry_id} would swallow this as
# entry_id="prices".
@router.get("/prices")
async def list_prices() -> dict:
    """Every registry entry with its pricing, for the admin Pricing tab.
    Unpriced entries are included with price=null -- "which models are still
    uncosted" is the main question this table answers."""
    data = []
    for entry in await model_registry_store.list_all():
        config = entry.get("config") or {}
        data.append({
            "id": entry["id"],
            "kind": entry["kind"],
            "engine": entry["engine"],
            "model_id": entry["model_id"],
            "label": entry["label"],
            "provider_id": config.get("provider_id", ""),
            "unit": PRICE_UNIT_BY_KIND.get(entry["kind"], ""),
            "price": config.get("price"),
        })
    return {"success": True, "data": data}


@router.patch("/prices")
async def update_prices(payload: BulkPriceRequest) -> dict:
    """Bulk price save. Validates EVERY item before writing ANY of them: a
    half-applied price table leaves the admin unable to tell which rows landed."""
    entries = {e["id"]: e for e in await model_registry_store.list_all()}
    planned: list[tuple[str, dict]] = []
    for item in payload.prices:
        entry = entries.get(item.id)
        if entry is None:
            raise HTTPException(
                status_code=404, detail=f"model registry entry '{item.id}' not found"
            )
        try:
            planned.append((
                item.id,
                apply_price_to_config(entry["kind"], entry.get("config") or {}, item.price),
            ))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"{entry['label'] or item.id}: {exc}"
            ) from exc
    for entry_id, config in planned:
        await model_registry_store.set_fields(entry_id, config=config)
    return {"success": True, "data": {"updated": len(planned)}}


_VALID_KINDS = {"stt", "tts", "llm", "embed"}


@router.get("/options")
async def list_options(kind: str, request: Request) -> dict:
    """Selectable models for a dropdown, filtered to what this user may pick.
    The single source of truth every profile/service select reads."""
    if kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown kind '{kind}'")
    user_id = current_user_id(request)
    user = await user_store.get_by_id(user_id) if user_id else None
    can_use_testing = bool(user and user.can_use_testing)
    options = await model_registry_store.list_options(kind, can_use_testing)
    return {"success": True, "data": options}


@router.get("/defaults")
async def get_defaults() -> dict:
    """The server's resolved default STT/TTS/LLM -- what a session uses when a
    profile/selection doesn't pin one. Read-only; lets the UI show the actual
    model behind "server default"."""
    from app.services.stt.model_catalog import resolve_default_stt_model
    from app.services.system_config import system_config_store

    async def _label(kind: str, engine: str, model_id: str) -> str:
        if not engine:
            return ""
        entry = await model_registry_store.find(kind, engine, model_id or "")
        if entry and entry.get("label"):
            return entry["label"]
        return f"{engine}/{model_id}" if model_id else engine

    eng = system_config_store.get().engines
    stt_engine = eng.default_stt_engine
    stt_model = resolve_default_stt_model(stt_engine) or ""
    tts_engine = eng.default_tts_engine
    llm = await model_registry_store.find_default("llm")
    return {
        "success": True,
        "data": {
            "stt": {"engine": stt_engine, "model_id": stt_model, "label": await _label("stt", stt_engine, stt_model)},
            "tts": {"engine": tts_engine, "label": await _label("tts", tts_engine, "")},
            "llm": (
                {"engine": llm["engine"], "model_id": llm["model_id"],
                 "label": llm.get("label") or await _label("llm", llm["engine"], llm["model_id"])}
                if llm else None
            ),
        },
    }


@router.post("")
async def create_entry(payload: CreateEntryRequest, request: Request) -> dict:
    # Validate/normalize the price before anything else: the add-time test call
    # is a real network round-trip, and a typo'd price shouldn't cost one.
    try:
        payload.config = apply_price_to_config(
            payload.kind, payload.config, (payload.config or {}).get("price")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _validate_known_engine(payload.kind, payload.engine)

    # A create() always defaults to enabled=True (no explicit `enabled` field
    # on this request), so the same not-installed guard as the PATCH path
    # applies here too -- reject before the network/provider test-call runs.
    if is_artifact_installed(payload.kind, payload.engine, payload.model_id) is False:
        raise HTTPException(
            status_code=400,
            detail=f"{payload.engine}/{payload.model_id} is not installed -- download it via the Models page first",
        )

    # If the entry links a provider (config.provider_id), the add-time test-call
    # and the persisted lookup path both use the provider's shared base_url/api_key
    # so the admin need not retype credentials per model.
    eff_base_url, eff_api_key = await resolve_credentials(payload.model_dump())

    try:
        if payload.kind == "stt":
            if payload.engine in _OPENROUTER_STT_ENGINES:
                provider = OpenRouterSttProvider(
                    name=payload.engine, model=payload.model_id, api_key=eff_api_key
                )
            elif payload.engine in _SERVICE_STT_ENGINES:
                provider = HttpSttProvider(
                    name=payload.engine,
                    entry={**payload.model_dump(), "base_url": eff_base_url, "api_key": eff_api_key},
                )
            elif payload.engine == "qwencloud":
                provider = QwenCloudSttProvider(
                    name=payload.engine,
                    entry={**payload.model_dump(), "base_url": eff_base_url, "api_key": eff_api_key},
                )
            else:
                provider = stt_service.get_provider(payload.engine)
            await provider.transcribe_bytes(_SAMPLE_WAV)
        elif payload.kind == "tts":
            if payload.engine in _SERVICE_TTS_ENGINES:
                provider = HttpTtsProvider(
                    name=payload.engine,
                    entry={**payload.model_dump(), "base_url": eff_base_url, "api_key": eff_api_key},
                )
            else:
                provider = tts_service.get_provider(payload.engine)
            await provider.synthesize(TTSRequest(text=payload.sample_text, engine=payload.engine))
        elif payload.kind == "llm":
            responder = OpenAICompatResponder(
                base_url=eff_base_url, api_key=eff_api_key, model=payload.model_id,
                system_prompt="", timeout=30.0,
            )
            try:
                await responder.reply([{"role": "user", "content": payload.sample_text}])
            finally:
                await responder.aclose()
        elif payload.kind == "embed":
            # Same "prove it works before we store it" contract as the other
            # kinds: one tiny embed call validates endpoint + key + model id.
            from app.services.memory.embedder import embed_texts

            await embed_texts([payload.sample_text], eff_base_url, eff_api_key, payload.model_id)
        else:
            raise HTTPException(status_code=400, detail=f"unknown kind '{payload.kind}'")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface the provider's own error to the admin
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # The add-time test call above really hit the provider, so it really cost
    # money -- record it. Deliberately NOT gated: an admin over quota must still
    # be able to validate the provider they need in order to fix the config that
    # put them over. request_id marks it as validation, not serving traffic.
    try:
        await record_usage(
            user_id=current_user_id(request) or "", profile_id="",
            kind=payload.kind, engine=payload.engine, model_id=payload.model_id,
            unit={"llm": "tokens", "embed": "tokens", "stt": "seconds", "tts": "chars"}
                 .get(payload.kind, ""),
            native_amount=0.0, request_id="registry-test-call",
        )
    except Exception as exc:  # noqa: BLE001 - metering must never break an admin action
        logger.warning("registry test-call metering failed: %s", exc)

    # Persist api_key for every kind (stt: read by openrouter_provider.py and
    # http_stt_provider.py; llm: read by responder.py's
    # resolve_llm_override_from_registry; tts: read by http_tts_provider.py).
    # base_url is meaningful for every kind now: llm and the http_stt/http_tts
    # service engines all pair a model with an OpenAI-compatible endpoint.
    created = await model_registry_store.create(
        payload.kind, payload.engine, payload.model_id, payload.label, stage=payload.stage,
        api_key=payload.api_key,
        base_url=payload.base_url,
        config=payload.config,
        is_default=payload.is_default,
    )
    created["api_key"] = _mask_api_key(created["api_key"])
    return {"success": True, "data": created}


@router.patch("/{entry_id}")
async def update_entry(entry_id: str, payload: UpdateEntryRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "config" in fields and "price" in (fields["config"] or {}):
        # The kind isn't in the payload -- it comes from the stored row, which is
        # also what tells us which unit this price must use.
        existing = await model_registry_store.get(entry_id)
        if existing is None:
            raise HTTPException(
                status_code=404, detail=f"model registry entry '{entry_id}' not found"
            )
        try:
            fields["config"] = apply_price_to_config(
                existing["kind"], fields["config"], fields["config"]["price"]
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    was_enabled = None
    if fields.get("enabled") is False:
        # Snapshot before the write: only a True -> False transition is the admin
        # taking the row out of service, and only that clears the profiles pinned
        # to it (see clear_bindings_for). Re-sending enabled=False on an
        # already-disabled row -- what saving an edit form does -- must not wipe a
        # binding the admin has re-pinned since.
        before = await model_registry_store.get(entry_id)
        was_enabled = bool(before and before["enabled"])
    if "api_key" in fields and not fields["api_key"]:
        # Blank means "keep the existing key" -- same convention as every other
        # secret field in this app (the UI never pre-fills a real key, so a
        # blank submit can only mean "didn't type a new one").
        del fields["api_key"]

    if fields.get("enabled") is True:
        existing = await model_registry_store.get(entry_id)
        # existing is None -> pre-existing entry, let the set_fields()-returns-None
        # 404 below handle it; don't duplicate the raise here.
        if existing is not None and is_artifact_installed(
            existing["kind"], existing["engine"], existing["model_id"]
        ) is False:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{existing['engine']}/{existing['model_id']} is not installed -- "
                    "download it via the Models page first"
                ),
            )

    updated = await model_registry_store.set_fields(entry_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"model registry entry '{entry_id}' not found")

    if updated["kind"] == "stt" and updated["engine"] in ("whisper_service", "eventlab"):
        stt_service.reinit_remote_providers()
    elif updated["kind"] == "stt" and updated["engine"] == "qwen3_asr" and "config" in fields:
        from app.services.stt.providers.qwen3_asr_provider import clear_model_cache

        clear_model_cache()
    elif updated["kind"] == "tts" and updated["engine"] == "omnivoice":
        from app.services.tts.providers.omnivoice_provider import reset_voice_ref_and_respawn

        reset_voice_ref_and_respawn()

    cleared: list[str] = []
    if was_enabled:
        cleared = await clear_bindings_for(
            updated["kind"], updated["engine"], updated["model_id"])

    updated["api_key"] = _mask_api_key(updated["api_key"])
    return {"success": True, "data": {**updated, "cleared": cleared}}


@router.delete("/{entry_id}")
async def delete_entry(entry_id: str) -> dict:
    existing = await model_registry_store.get(entry_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"model registry entry '{entry_id}' not found")
    if existing["enabled"]:
        raise HTTPException(
            status_code=400,
            detail=f"disable '{existing['engine']}/{existing['model_id']}' before deleting it",
        )
    deleted = await model_registry_store.delete(entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"model registry entry '{entry_id}' not found")
    # Normally a no-op: the disable this route insists on already cleared the
    # profiles pinned to the row. Kept as the safety net for a row still pinned at
    # delete time (disabled before this feature shipped, or re-pinned since).
    cleared = await clear_bindings_for(existing["kind"], existing["engine"], existing["model_id"])
    return {"success": True, "data": {"id": entry_id, "deleted": True, "cleared": cleared}}
