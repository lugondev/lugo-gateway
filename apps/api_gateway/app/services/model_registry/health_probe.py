"""Liveness probe for self-hosted OpenAI-compatible speech services.

Answers one question: is a process actually listening at this base_url? That
is deliberately weaker than "does it implement /health correctly" -- a 404 or
401 still proves something is alive and answering, and the failure this exists
to catch is the model_service process being down entirely, which surfaces as a
connection-level error rather than an HTTP status.
"""

from __future__ import annotations

import httpx

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
