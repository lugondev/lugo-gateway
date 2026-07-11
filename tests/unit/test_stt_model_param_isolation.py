"""Two concurrent transcribe_bytes() calls with different `model=` values must
not clobber each other via the process-global active-model state.

Regression coverage for the bug where ConversationSession.start() called
apply_stt_model() (mutating a process-global), and _load_model()/_mlx_session()
re-read that same global on every transcribe — so session B picking a different
model would silently change what session A's next turn transcribed against.
"""

import asyncio
import io
import wave

import pytest

from app.services.stt.providers import whisper_provider
from app.services.stt.providers.whisper_provider import WhisperProvider


def _silent_wav() -> bytes:
    b = io.BytesIO()
    w = wave.open(b, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 1600)
    w.close()
    return b.getvalue()


@pytest.fixture(autouse=True)
def _reset_cache():
    whisper_provider._MODEL_CACHE.clear()
    yield
    whisper_provider._MODEL_CACHE.clear()


class _FakeModel:
    def __init__(self, resolved_path, **kw):
        self._path = resolved_path

    def transcribe(self, path, **kw):
        class Seg:
            def __init__(self, text):
                self.text = text
        return [Seg(f"got:{self._path}")], None


def test_concurrent_sessions_use_their_own_model_not_the_global(monkeypatch):
    monkeypatch.setattr(whisper_provider, "resolve_whisper_model", lambda m: m)
    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _FakeModel)

    provider = WhisperProvider()
    wav = _silent_wav()

    async def call_a():
        return await provider.transcribe_bytes(wav, "vi", model="small")

    async def call_b():
        # Simulate another session's session-start flipping the process-global
        # default WHILE call_a is in flight — call_a must be unaffected since it
        # passes model= explicitly.
        whisper_provider.set_active_whisper_model("medium")
        return await provider.transcribe_bytes(wav, "vi", model="medium")

    async def run():
        return await asyncio.gather(call_a(), call_b())

    result_a, result_b = asyncio.run(run())

    assert "small" in result_a.text
    assert "medium" in result_b.text


def test_load_model_falls_back_to_active_global_when_model_omitted(monkeypatch):
    monkeypatch.setattr(whisper_provider, "resolve_whisper_model", lambda m: m)
    original = whisper_provider.get_active_whisper_model()
    whisper_provider.set_active_whisper_model("tiny")

    calls = []

    class _Recording(_FakeModel):
        def __init__(self, resolved_path, **kw):
            calls.append(resolved_path)
            super().__init__(resolved_path, **kw)

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _Recording)

    try:
        provider = WhisperProvider()
        provider._load_model()  # no explicit model -> falls back to the active global
        assert calls == ["tiny"]
    finally:
        whisper_provider.set_active_whisper_model(original)


import sys
import types


def test_qwen3_concurrent_sessions_use_their_own_model(monkeypatch):
    import app.services.stt.providers.qwen3_asr_provider as q_mod

    q_mod._MODEL_CACHE.clear()
    monkeypatch.setattr(q_mod, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(q_mod, "module_available", lambda m: m == "mlx_qwen3_asr")

    class FakeSession:
        def __init__(self, model):
            self._model = model

        def transcribe(self, path, language=None):
            return types.SimpleNamespace(text=f"got:{self._model}")

    fake_mod = types.ModuleType("mlx_qwen3_asr")
    fake_mod.Session = FakeSession
    monkeypatch.setitem(sys.modules, "mlx_qwen3_asr", fake_mod)

    provider = q_mod.Qwen3AsrProvider()
    wav = _silent_wav()

    async def call_a():
        return await provider.transcribe_bytes(wav, "vi", model="0.6b")

    async def call_b():
        q_mod.set_active_qwen3_asr_model("1.7b")
        return await provider.transcribe_bytes(wav, "vi", model="1.7b")

    async def run():
        return await asyncio.gather(call_a(), call_b())

    result_a, result_b = asyncio.run(run())

    assert "0.6b" in result_a.text
    assert "1.7b" in result_b.text
    q_mod.set_active_qwen3_asr_model(None)
