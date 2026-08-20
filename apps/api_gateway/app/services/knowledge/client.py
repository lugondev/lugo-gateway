"""The one call the gateway makes into kbase: search.

One `AsyncClient` for the process, not one per call. This runs inside a
conversational turn, and rebuilding the connection pool each time pays a fresh
TCP and TLS handshake on the one path where latency is audible.
"""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 10.0


class KnowledgeUnavailable(Exception):
    """The lookup could not be performed.

    Carries a message for the log, never for the model: an httpx error text
    holds the base URL, and a tool result may be spoken aloud.
    """


class KnowledgeClient:
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    def configure(self, *, base_url: str, api_key: str, timeout: float) -> None:
        """Re-point at the configured service. The admin can change these at
        runtime, so they are read per call rather than frozen at import."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client.timeout = httpx.Timeout(timeout)

    async def search_with_usage(
        self, collection: str, query: str, *, limit: int, min_score: float
    ) -> tuple[list[dict], int]:
        """Hits and the tokens kbase spent embedding the query.

        `*_with_usage` is this codebase's marker for "spends money, reports the
        count" -- see memory's `embed_texts_with_usage`. The paid-call-site
        inventory scans for that name.
        """
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            resp = await self._client.post(
                f"{self._base_url}/v1/search",
                headers=headers,
                json={
                    "collection": collection,
                    "query": query,
                    "limit": limit,
                    "min_score": min_score,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict):
                raise ValueError("knowledge search returned a non-object body")
            raw_hits = body.get("chunks") or []
            if not isinstance(raw_hits, list) or not all(isinstance(hit, dict) for hit in raw_hits):
                raise ValueError("knowledge search returned malformed chunks")
            raw_usage = body.get("usage") or {}
            if not isinstance(raw_usage, dict):
                raise ValueError("knowledge search returned malformed usage")
            tokens = int(raw_usage.get("prompt_tokens") or 0)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            # Every failure mode -- transport, status, decode, and shape --
            # collapses to this one exception. Nothing past this boundary is
            # allowed to see a raw httpx or parse error: that text carries the
            # base URL, and a tool result built from it may be read aloud.
            raise KnowledgeUnavailable(f"knowledge search failed: {exc}") from exc
        return list(raw_hits), tokens

    async def aclose(self) -> None:
        await self._client.aclose()


knowledge_client = KnowledgeClient()
