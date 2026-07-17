"""Shared httpx -> RuntimeError translation for remote STT/TTS providers.

Four providers (openai_stt_provider, openai_tts_provider,
remote_whisper_provider, openrouter_provider) each make an httpx call to a
remote engine and want the same two outcomes: a non-2xx response becomes a
RuntimeError naming the status and a snippet of the body, and any other httpx
failure (timeout, connection error, ...) becomes a RuntimeError naming the
underlying exception. Centralizing it keeps the message format identical
across providers instead of four hand-copied blocks drifting apart.
"""

from __future__ import annotations

import httpx


def translate_httpx_error(name: str, exc: httpx.HTTPError) -> RuntimeError:
    """Build the RuntimeError a provider should raise for a given httpx error.

    Callers are expected to `raise translate_httpx_error(...) from exc`.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return RuntimeError(
            f"{name} returned HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        )
    return RuntimeError(f"{name} request failed: {exc}")
