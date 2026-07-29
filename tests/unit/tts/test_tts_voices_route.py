"""GET /v1/tts/voices -- always returns {"voices": [...], "supports_clone": bool},
never a bare list (no more hasattr special-casing at the route level; every
TTSProvider answers both methods via base.py's defaults)."""

import pytest

from app.api.routes.tts import list_tts_voices
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _FakeVoicedProvider(TTSProvider):
    name = "fake_voiced"

    async def synthesize(self, payload):  # pragma: no cover - not exercised here
        raise NotImplementedError

    async def list_voices(self) -> list[dict]:
        return [{"label": "A", "voice": "a"}]

    async def supports_voice_clone(self) -> bool:
        return True


class _FakeBareProvider(TTSProvider):
    name = "fake_bare"

    async def synthesize(self, payload):  # pragma: no cover - not exercised here
        raise NotImplementedError


@pytest.mark.asyncio
async def test_voices_route_returns_voices_and_clone_flag(monkeypatch):
    monkeypatch.setitem(tts_service.providers, "fake_voiced", _FakeVoicedProvider())
    result = await list_tts_voices(engine="fake_voiced")
    assert result == {
        "success": True,
        "data": {"voices": [{"label": "A", "voice": "a"}], "supports_clone": True},
    }


@pytest.mark.asyncio
async def test_voices_route_defaults_for_an_engine_without_overrides(monkeypatch):
    monkeypatch.setitem(tts_service.providers, "fake_bare", _FakeBareProvider())
    result = await list_tts_voices(engine="fake_bare")
    assert result == {"success": True, "data": {"voices": [], "supports_clone": False}}
