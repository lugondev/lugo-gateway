"""Quota pre-flight for the batch STT/TTS routes.

Both /v1/stt/transcribe and /v1/tts/synthesize have to answer the same question
before the provider does any work: resolve the caller's (engine, model) to the
pair the registry actually holds, look up which provider that row bills to, and
let the gate refuse the request. They carried the block twice, differing only in
`kind` and which field the model arrives in.

Fail-open is the contract, not an accident: a registry hiccup resolves to blank
identifiers, which still leaves the user/global quotas enforced but can no
longer 500 a request over metering.
"""

from __future__ import annotations

from fastapi import HTTPException

from app.services.model_registry.store import model_registry_store
from app.services.usage.attribution import resolve_usage_model


async def quota_preflight(*, kind: str, engine: str, model: str, user_id: str) -> None:
    """Raise HTTPException(429) if this call is over quota; return otherwise.

    `kind` is "stt" or "tts" -- it selects both the registry namespace and the
    quota bucket, so the two always agree.
    """
    provider_id = ""
    try:
        # Inside the guard, not before it: resolve_usage_model() never raises
        # from its own logic, but its function-level import of the registry
        # store sits outside that promise, and an ImportError there would 500 a
        # request this gate is required to fail open on.
        usage_engine, usage_model_id = await resolve_usage_model(kind, engine, model or "")
        entry = await model_registry_store.find(kind, usage_engine, usage_model_id)
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
            user_id=user_id, provider_id=provider_id,
            kind=kind, engine=usage_engine, model_id=usage_model_id,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
