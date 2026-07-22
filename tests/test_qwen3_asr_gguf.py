"""Qwen3-ASR GGUF STT engine: registered, availability-gated, in catalog, serialized.

This backend shells out to the qwen3-asr-cli binary (no Python module). It is the
reference non-Python engine and is registered both in-process and (unchanged) as an
apps/model_service engine.
"""

import asyncio
import io
import threading
import time
import wave

import pytest

import app.services.stt.providers.qwen3_asr_gguf_provider as g_mod
from app.services.recommend.catalog import CANDIDATES
from app.services.stt.providers.qwen3_asr_gguf_provider import Qwen3AsrGgufProvider
from app.services.stt.service import stt_service


def _silent_wav() -> bytes:
    b = io.BytesIO()
    w = wave.open(b, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 1600)
    w.close()
    return b.getvalue()


def test_registered():
    assert "qwen3_asr_gguf" in stt_service.providers
    assert isinstance(stt_service.providers["qwen3_asr_gguf"], Qwen3AsrGgufProvider)


def test_stt_request_schema_accepts_qwen3_asr_gguf():
    from app.schemas.stt import STTRequest

    assert STTRequest(engine="qwen3_asr_gguf").engine == "qwen3_asr_gguf"


@pytest.mark.asyncio
async def test_listed_reflects_binary_presence(monkeypatch):
    # Force availability true regardless of whether a binary is installed on the
    # test host, and confirm list_engines surfaces it as a local engine.
    monkeypatch.setattr(Qwen3AsrGgufProvider, "available", lambda self: True)
    monkeypatch.setattr(Qwen3AsrGgufProvider, "detail", lambda self: "m.gguf · CPU")
    engines = {e["engine"]: e for e in await stt_service.list_engines()}
    assert "qwen3_asr_gguf" in engines
    assert engines["qwen3_asr_gguf"]["mode"] == "local"
    assert engines["qwen3_asr_gguf"]["available"] is True


@pytest.mark.asyncio
async def test_listed_hidden_when_binary_absent(monkeypatch):
    monkeypatch.setattr(Qwen3AsrGgufProvider, "available", lambda self: False)
    engines = {e["engine"]: e for e in await stt_service.list_engines()}
    assert engines["qwen3_asr_gguf"]["available"] is False
    assert "binary" in engines["qwen3_asr_gguf"]["detail"]


def test_in_recommend_catalog_cpu_vietnamese():
    cands = [c for c in CANDIDATES if c.engine == "qwen3_asr_gguf"]
    assert len(cands) == 1, "expect exactly one GGUF/CPU candidate"
    c = cands[0]
    assert c.category == "stt"
    assert c.chip == "cpu"  # the whole point: a CPU path for Qwen3-ASR
    assert c.vietnamese is True
    assert "qwen3_asr_cpp" in c.requires


@pytest.mark.asyncio
async def test_seeds_engine_config_registry_row():
    """The engine must surface in the Model Registry (SQLite store), not just as
    code-level resolve.py defaults. migrate_stt_local_models_to_registry() seeds
    the model_id="" sentinel row; with no legacy stt_local fields it falls back to
    STT_ENGINE_CONFIG_DEFAULTS for the full default config."""
    from app.services.model_registry import seed
    from app.services.model_registry.resolve import STT_ENGINE_CONFIG_DEFAULTS
    from app.services.model_registry.store import model_registry_store

    await seed.migrate_stt_local_models_to_registry()
    row = await model_registry_store.find("stt", "qwen3_asr_gguf", "")
    assert row is not None, "qwen3_asr_gguf must have a registry row"
    assert row["config"] == STT_ENGINE_CONFIG_DEFAULTS["qwen3_asr_gguf"]


def test_capability_flag_resolves(monkeypatch):
    from app.services.recommend import capabilities as caps_mod

    monkeypatch.setattr(g_mod, "resolve_qwen3_asr_gguf_binary", lambda: "/bin/qwen3-asr-cli")
    assert caps_mod._qwen3_asr_cpp() is True
    monkeypatch.setattr(g_mod, "resolve_qwen3_asr_gguf_binary", lambda: None)
    assert caps_mod._qwen3_asr_cpp() is False


@pytest.mark.asyncio
async def test_subprocess_calls_are_serialized_on_one_thread(monkeypatch):
    """One dedicated worker thread => concurrent transcribes launch binary processes
    one at a time (never N parallel processes thrashing a CPU-bound host), and all
    run on the same thread. Mirrors the MLX providers' single-thread guarantee."""
    txns: list[int] = []
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()

    monkeypatch.setattr(g_mod, "resolve_qwen3_asr_gguf_binary", lambda: "/bin/qwen3-asr-cli")
    monkeypatch.setattr(
        g_mod, "resolve_stt_engine_config",
        lambda _e: {"default_model": "/models/m.gguf", "n_threads": 8, "timeout_seconds": 120.0},
    )
    monkeypatch.setattr(g_mod.os.path, "isfile", lambda p: p == "/models/m.gguf")

    class _Proc:
        stdout = "ok"
        stderr = ""

    def _fake_run(argv, **kwargs):
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        txns.append(threading.get_ident())
        time.sleep(0.03)  # widen the window for overlap to show up
        with lock:
            concurrent["now"] -= 1
        return _Proc()

    monkeypatch.setattr(g_mod.subprocess, "run", _fake_run)

    p = Qwen3AsrGgufProvider()
    wav = _silent_wav()
    results = await asyncio.gather(*[p.transcribe_bytes(wav, "vi") for _ in range(6)])

    assert all(r.text == "ok" for r in results)
    assert concurrent["max"] == 1, "subprocess launches overlapped — not serialized"
    assert len(set(txns)) == 1, "transcribes ran on more than one thread"
