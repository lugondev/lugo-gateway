"""Liveness probe for self-hosted OpenAI-compatible speech services.

Answers one question: is a process actually listening at this base_url? That
is deliberately weaker than "does it implement /health correctly" -- a 404 or
401 still proves something is alive and answering, and the failure this exists
to catch is the model_service process being down entirely, which surfaces as a
connection-level error rather than an HTTP status.
"""

from __future__ import annotations

from typing import Awaitable, Callable

import httpx

from app.schemas.health import EngineHealth

DEFAULT_PROBE_TIMEOUT = 3.0


def _health_url(base_url: str) -> str:
    """apps/model_service serves /health at the root, while registry base_urls
    point at the OpenAI-compatible /v1 prefix -- strip it before appending."""
    trimmed = base_url.strip().rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return f"{trimmed}/health"


async def probe_service_health(
    base_url: str, api_key: str, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> tuple[bool, str | None]:
    """(reachable, reason). reason is None when reachable."""
    if not base_url.strip():
        return False, "no base_url configured"

    headers = {"Authorization": f"Bearer {api_key.strip()}"} if api_key.strip() else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.get(_health_url(base_url), headers=headers)
    except httpx.HTTPError as exc:
        return False, str(exc) or type(exc).__name__
    return True, None


async def check_remote_engine_health(
    kind: str,
    engine: str,
    model_id: str,
    *,
    probe: Callable[[str, str], Awaitable[tuple[bool, str | None]]] = probe_service_health,
) -> EngineHealth:
    """Shared by STTService.check_engine / TTSService.check_engine for the one
    engine per kind that is backed by a self-hosted OpenAI-compatible HTTP
    service (http_stt / http_tts): resolve the Model Registry entry, resolve
    its credentials, and probe liveness.

    `probe` is injectable rather than always calling the module-level
    `probe_service_health` above directly: each caller passes its OWN
    module's `probe_service_health` binding (e.g.
    `app.services.stt.service.probe_service_health`), which is what tests
    monkeypatch -- passing it through here, evaluated at call time, is what
    makes the patched version actually take effect instead of this module's
    unpatched original.
    """
    from app.services.model_registry.store import model_registry_store
    from app.services.providers.resolve import resolve_credentials

    entry = (
        await model_registry_store.find(kind, engine, model_id)
        if model_id
        else await model_registry_store.find_enabled(kind, engine)
    )
    if not entry:
        return EngineHealth(
            engine=engine, status="unavailable",
            detail="not configured: no enabled Model Registry entry",
        )
    base_url, api_key = await resolve_credentials(entry)
    reachable, reason = await probe(base_url, api_key)
    if not reachable:
        return EngineHealth(
            engine=engine, status="unavailable",
            detail=f"unreachable at {base_url or '(no base_url)'}: {reason}",
        )
    return EngineHealth(engine=engine, status="ok", detail=base_url)
