"""Where a remote OpenAI-compatible engine lives, and how long to wait for it.

Companion to http_errors.py: that module owns what a remote provider does when
a call FAILS, this one owns what it needs before the call is made. Both
http_stt_provider and http_tts_provider resolved a Model Registry entry to
(base_url, api_key, timeout) with the same four steps and the same
not-configured message, differing only in the example URL they suggest.
"""

from __future__ import annotations

from typing import NamedTuple

from app.services.providers.resolve import resolve_credentials


class RemoteEndpoint(NamedTuple):
    base_url: str
    api_key: str
    timeout: float


async def resolve_remote_endpoint(
    entry, *, name: str, example_url: str, default_timeout: float
) -> RemoteEndpoint:
    """Resolve a registry `entry` (possibly None) to a usable endpoint.

    Raises RuntimeError naming the engine when no base URL is configured --
    that is the message an operator sees in the UI when the row exists but
    points nowhere, so it has to say what to add and give a shape for it.
    """
    if entry:
        base_url, api_key = await resolve_credentials(entry)
    else:
        base_url, api_key = "", ""
    base_url = base_url.strip()
    if not base_url:
        raise RuntimeError(
            f"{name} is not configured. Add a Model Registry entry with the "
            f"service's base URL (e.g. {example_url})."
        )
    configured_timeout = (entry.get("config") or {}).get("timeout_seconds")
    return RemoteEndpoint(
        base_url=base_url,
        api_key=api_key.strip(),
        timeout=configured_timeout if configured_timeout is not None else default_timeout,
    )
