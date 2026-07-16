import sys
import types

import pytest

from app.services.stt.providers import qwen3_asr_provider as q


@pytest.fixture(autouse=True)
def _reset_active():
    q.set_active_qwen3_asr_model(None)
    yield
    q.set_active_qwen3_asr_model(None)


def test_resolve_shorthand_to_full_repo():
    assert q.resolve_qwen3_asr_model("0.6b") == "Qwen/Qwen3-ASR-0.6B"
    assert q.resolve_qwen3_asr_model("1.7B") == "Qwen/Qwen3-ASR-1.7B"


def test_resolve_full_repo_passthrough():
    assert q.resolve_qwen3_asr_model("Qwen/Qwen3-ASR-1.7B") == "Qwen/Qwen3-ASR-1.7B"


def test_get_active_defaults_to_registry_engine_config():
    from app.services.model_registry.resolve import resolve_stt_engine_config

    assert q.get_active_qwen3_asr_model() == resolve_stt_engine_config("qwen3_asr")["default_model"]


def test_set_active_resolves_shorthand_and_roundtrips():
    q.set_active_qwen3_asr_model("1.7b")
    assert q.get_active_qwen3_asr_model() == "Qwen/Qwen3-ASR-1.7B"


def test_set_active_none_resets_to_registry_engine_config():
    from app.services.model_registry.resolve import resolve_stt_engine_config

    q.set_active_qwen3_asr_model("1.7b")
    q.set_active_qwen3_asr_model(None)
    assert q.get_active_qwen3_asr_model() == resolve_stt_engine_config("qwen3_asr")["default_model"]


def test_clear_model_cache_empties_the_cache():
    q._MODEL_CACHE["cuda:Qwen/Qwen3-ASR-0.6B"] = object()
    q.clear_model_cache()
    assert q._MODEL_CACHE == {}


@pytest.mark.asyncio
async def test_uses_registry_device_over_default(monkeypatch):
    from app.services.model_registry.store import model_registry_store

    await model_registry_store.create("stt", "qwen3_asr", "", "Qwen3-ASR", config={"device": "mps"})

    # Force the CUDA backend path (the one that reads device_map) regardless of
    # host — same fake-module technique used for the mlx backend elsewhere in
    # this suite (see tests/test_qwen3_asr.py, test_stt_model_param_isolation.py).
    monkeypatch.setattr(q, "_is_apple_silicon", lambda: False)
    monkeypatch.setattr(q, "module_available", lambda m: m == "qwen_asr")

    captured: dict = {}

    class FakeQwen3ASRModel:
        @classmethod
        def from_pretrained(cls, model_id, **kwargs):
            captured.update(kwargs)
            return cls()

    fake_mod = types.ModuleType("qwen_asr")
    fake_mod.Qwen3ASRModel = FakeQwen3ASRModel
    monkeypatch.setitem(sys.modules, "qwen_asr", fake_mod)

    q._MODEL_CACHE.clear()
    provider = q.Qwen3AsrProvider()
    provider._cuda_model()

    assert captured["device_map"] == "mps"
    q._MODEL_CACHE.clear()
