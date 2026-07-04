import asyncio

import pytest

from app.core.errors import AppError
from app.services.warmup import is_ready, warm_providers


class _Spy:
    def __init__(self, raises: bool = False):
        self.calls = 0
        self.raises = raises

    def warm(self):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")


class _NoWarm:
    pass


@pytest.mark.asyncio
async def test_warm_providers_calls_warm_on_each():
    a, b = _Spy(), _Spy()
    await warm_providers(a, b)
    assert a.calls == 1
    assert b.calls == 1


@pytest.mark.asyncio
async def test_warm_providers_skips_provider_without_warm():
    no_warm = _NoWarm()
    spy = _Spy()
    await warm_providers(no_warm, spy)  # must not raise
    assert spy.calls == 1


@pytest.mark.asyncio
async def test_warm_providers_one_failure_does_not_stop_others():
    failing, ok = _Spy(raises=True), _Spy()
    await warm_providers(failing, ok)  # must not raise
    assert failing.calls == 1
    assert ok.calls == 1


@pytest.mark.asyncio
async def test_warm_default_engines_warms_configured_stt_and_tts(monkeypatch):
    from app.main import _warm_default_engines
    from app.services.stt.service import stt_service
    from app.services.tts.service import tts_service
    from app.core.settings import settings

    stt_spy, tts_spy = _Spy(), _Spy()
    monkeypatch.setattr(settings, "conversation_stt_engine", "fake_stt")
    monkeypatch.setattr(settings, "conversation_tts_engine", "fake_tts")
    monkeypatch.setattr(stt_service, "get_provider", lambda engine: stt_spy)
    monkeypatch.setattr(tts_service, "get_provider", lambda engine: tts_spy)

    await _warm_default_engines()

    assert stt_spy.calls == 1
    assert tts_spy.calls == 1


def test_is_ready_true_immediately_for_provider_without_warm():
    assert is_ready(_NoWarm()) is True


def test_is_ready_true_for_tts_provider_using_inherited_noop_warm():
    """TTSProvider.warm() defaults to a no-op for engines with nothing to load
    (e.g. mocks, remote APIs) — those should never report as 'cold'."""
    from app.schemas.tts import TTSRequest, TTSResult
    from app.services.tts.base import TTSProvider

    class _NoopWarmTTS(TTSProvider):
        name = "noop-warm-tts"

        async def synthesize(self, payload: TTSRequest) -> TTSResult:
            raise NotImplementedError

    provider = _NoopWarmTTS()
    assert is_ready(provider) is True


@pytest.mark.asyncio
async def test_warm_providers_skips_tts_provider_with_inherited_noop_warm(monkeypatch):
    from app.schemas.tts import TTSRequest, TTSResult
    from app.services.tts.base import TTSProvider

    class _NoopWarmTTS(TTSProvider):
        name = "noop-warm-tts-2"

        async def synthesize(self, payload: TTSRequest) -> TTSResult:
            raise NotImplementedError

    provider = _NoopWarmTTS()
    calls = []
    monkeypatch.setattr(asyncio, "to_thread", lambda *a, **kw: calls.append(1))
    await warm_providers(provider)
    assert calls == []  # never dispatched — nothing to warm


@pytest.mark.asyncio
async def test_is_ready_false_until_warm_completes_then_true():
    provider = _Spy()
    assert is_ready(provider) is False
    await warm_providers(provider)
    assert is_ready(provider) is True


@pytest.mark.asyncio
async def test_is_ready_becomes_true_even_if_warm_raises():
    provider = _Spy(raises=True)
    assert is_ready(provider) is False
    await warm_providers(provider)
    assert is_ready(provider) is True


@pytest.mark.asyncio
async def test_warm_default_engines_swallows_unknown_engine(monkeypatch):
    from app.main import _warm_default_engines
    from app.services.stt.service import stt_service

    def _raise(engine):
        raise AppError("Unsupported STT engine: nope")

    monkeypatch.setattr(stt_service, "get_provider", _raise)

    await _warm_default_engines()  # must not raise
