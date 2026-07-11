import asyncio
import logging

import pytest
from unittest.mock import AsyncMock

from app.core.settings import settings
from app.services.tts.providers.omnivoice_provider import OmniVoiceProvider


def test_omnivoice_timeout_is_not_absurdly_long():
    # Real-time conversation TTS; a 600s (10 min) timeout was functionally
    # indistinguishable from a permanent hang if the sidecar ever stalls on a
    # request. Cap it to something a user could plausibly tolerate.
    assert settings.omnivoice_timeout_seconds <= 60


@pytest.mark.asyncio
async def test_server_synth_logs_and_reraises_on_cancellation(monkeypatch, caplog):
    provider = OmniVoiceProvider()
    monkeypatch.setattr(provider, "_ensure_server", AsyncMock(return_value=None))

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            await asyncio.sleep(10)

    monkeypatch.setattr(
        "app.services.tts.providers.omnivoice_provider.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(),
    )

    task = asyncio.create_task(provider._server_synth("hello", None, None, None, None))
    await asyncio.sleep(0.05)
    task.cancel()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            await task

    assert any("cancel" in r.message.lower() for r in caplog.records)
