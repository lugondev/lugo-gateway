from __future__ import annotations

import math

import httpx

from app.services.system_config import system_config_store


async def embed_texts_with_usage(
    texts: list[str], base_url: str, api_key: str, model: str
) -> tuple[list[list[float]], int]:
    """Embed texts via an OpenAI-compatible /embeddings endpoint, also returning
    the provider's reported prompt_tokens (0 when it reports none) so the caller
    can meter the spend. Raises on failure."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = system_config_store.get().conversation.llm_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/embeddings",
            headers=headers,
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        body = resp.json()
    vectors = [d["embedding"] for d in body["data"]]
    tokens = int((body.get("usage") or {}).get("prompt_tokens") or 0)
    return vectors, tokens


async def embed_texts(
    texts: list[str], base_url: str, api_key: str, model: str
) -> list[list[float]]:
    """Vectors only -- for callers with no identity to attribute usage to (e.g.
    the Model Registry's add-time test call)."""
    vectors, _tokens = await embed_texts_with_usage(texts, base_url, api_key, model)
    return vectors


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
