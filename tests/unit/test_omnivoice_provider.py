import asyncio
import logging

import pytest
from unittest.mock import AsyncMock

from app.core.settings import settings
from app.services.tts.providers.omnivoice_provider import OmniVoiceProvider
from app.services.tts.providers import omnivoice_provider as ov_mod


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


@pytest.mark.asyncio
async def test_ensure_voice_ref_is_single_flight_under_concurrent_cold_start(monkeypatch):
    ov_mod._voice_ref.clear()
    build_calls = []

    async def fake_synth(self, text, instruct=None, ref_audio=None, ref_text=None, speed=None):
        build_calls.append(1)
        await asyncio.sleep(0.05)  # widen the race window
        return b"fake-wav-bytes"

    monkeypatch.setattr(ov_mod.OmniVoiceProvider, "_synth", fake_synth)
    monkeypatch.setattr(ov_mod.settings, "artifacts_dir", "/tmp")

    provider = ov_mod.OmniVoiceProvider()
    results = await asyncio.gather(*[provider._ensure_voice_ref() for _ in range(8)])

    assert len(build_calls) == 1, f"voice ref synthesized {len(build_calls)}x — not single-flight"
    assert all(r["path"] == results[0]["path"] for r in results)

    ov_mod._voice_ref.clear()
