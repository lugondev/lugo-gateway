from __future__ import annotations

import math

import httpx

from app.core.settings import settings


async def embed_texts(
    texts: list[str], base_url: str, api_key: str, model: str
) -> list[list[float]]:
    """Embed texts via an OpenAI-compatible /embeddings endpoint. Raises on failure."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=settings.conversation_llm_timeout_seconds) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/embeddings",
            headers=headers,
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
    return [d["embedding"] for d in data]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
