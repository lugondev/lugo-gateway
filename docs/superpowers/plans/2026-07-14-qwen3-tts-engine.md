# Qwen3-TTS (0.6B/1.7B) Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Qwen3-TTS as two new selectable TTS engines (`qwen3_tts_0_6b`, `qwen3_tts_1_7b`), each supporting voice cloning (Base checkpoint) and preset speakers (CustomVoice checkpoint), auto-detecting CPU/MPS/CUDA.

**Architecture:** One new provider file (`qwen3_tts_provider.py`) following the existing `RenderingTTSProvider` pattern used by `voxcpm2`/`kokoro_vi` in `extra_engines.py` — lazy-imported optional dependency, gated by `available()`. Two thin subclasses differ only in model size; a shared base class does routing (ref-audio present → voice clone, else → preset speaker) and device/dtype selection. Registered into `TTSService.providers` in `service.py`.

**Tech Stack:** Python, pytest (async tests via existing `pytest-asyncio` setup — see how `test_edge_tts_synthesize_*` tests are already `async def` with no extra decorator), numpy, PyTorch (lazily imported, already present in this dev venv), `qwen-tts` pip package (NOT installed as part of this plan — gated behind `available()` like `voxcpm`/`kokoro-vietnamese`; tests stub it via `sys.modules`).

## Global Constraints

- Engine ids are exactly `qwen3_tts_0_6b` and `qwen3_tts_1_7b` (used in `TTSService.providers`, checkpoint id strings, and test assertions) — do not rename.
- No changes to `TTSRequest`/`TTSResult` (`apps/api_gateway/app/schemas/tts.py`) — `language`, `voice`, `instruct`, `ref_audio_path`, `ref_text` already cover everything this engine needs.
- No changes to `pyproject.toml`, `system_config.py`, or any frontend JS — engines become selectable automatically once registered in `TTSService.providers`, exactly like `voxcpm2`/`kokoro_vi` today.
- `payload.language or "Auto"` is passed to both generation calls (the user has verified Vietnamese input works acceptably with `language="Auto"`, even though Vietnamese isn't in Qwen3-TTS's official language list).
- Default preset speaker is `"Vivian"` when `payload.voice` is unset.
- Env var `QWEN3_TTS_DEVICE` overrides auto device-detection when set.
- Checkpoint id format: `f"Qwen/Qwen3-TTS-12Hz-{size}-{kind}"` where `size` is `"0.6B"`/`"1.7B"` and `kind` is `"Base"`/`"CustomVoice"` (exact casing, confirmed against the real HuggingFace repo ids).

---

### Task 1: Device/dtype/attn-implementation selection helper

**Files:**
- Create: `apps/api_gateway/app/services/tts/providers/qwen3_tts_provider.py`
- Test: `tests/unit/test_tts_engines.py`

**Interfaces:**
- Produces: `_pick_device_dtype_attn() -> tuple[str, "torch.dtype", str | None]` — returns `(device, dtype, attn_implementation)`. `torch` is imported lazily inside the function (never at module top level — the whole module must stay importable when neither `torch` nor `qwen_tts` is installed, since `service.py` imports this module unconditionally).

- [ ] **Step 1: Write the failing tests**

Add to the top of `tests/unit/test_tts_engines.py` (alongside the existing imports):

```python
import numpy as np
```

Then append at the end of the file:

```python
def test_pick_device_dtype_attn_prefers_cuda(monkeypatch):
    import torch

    from app.services.tts.providers.qwen3_tts_provider import _pick_device_dtype_attn

    monkeypatch.delenv("QWEN3_TTS_DEVICE", raising=False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    device, dtype, attn = _pick_device_dtype_attn()

    assert device == "cuda:0"
    assert dtype is torch.bfloat16
    assert attn == "flash_attention_2"


def test_pick_device_dtype_attn_falls_back_to_mps(monkeypatch):
    import torch

    from app.services.tts.providers.qwen3_tts_provider import _pick_device_dtype_attn

    monkeypatch.delenv("QWEN3_TTS_DEVICE", raising=False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)

    device, dtype, attn = _pick_device_dtype_attn()

    assert device == "mps"
    assert dtype is torch.float16
    assert attn is None


def test_pick_device_dtype_attn_falls_back_to_cpu(monkeypatch):
    import torch

    from app.services.tts.providers.qwen3_tts_provider import _pick_device_dtype_attn

    monkeypatch.delenv("QWEN3_TTS_DEVICE", raising=False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)

    device, dtype, attn = _pick_device_dtype_attn()

    assert device == "cpu"
    assert dtype is torch.float32
    assert attn is None


def test_pick_device_dtype_attn_honors_env_override(monkeypatch):
    import torch

    from app.services.tts.providers.qwen3_tts_provider import _pick_device_dtype_attn

    monkeypatch.setenv("QWEN3_TTS_DEVICE", "cpu")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)  # must be ignored

    device, dtype, attn = _pick_device_dtype_attn()

    assert device == "cpu"
    assert dtype is torch.float32
    assert attn is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_tts_engines.py -k pick_device_dtype_attn -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.tts.providers.qwen3_tts_provider'`

- [ ] **Step 3: Create the module with the minimal implementation**

Create `apps/api_gateway/app/services/tts/providers/qwen3_tts_provider.py`:

```python
"""Qwen3-TTS (0.6B/1.7B) engine — voice clone (Base) + preset speakers (CustomVoice).

Package: qwen-tts (`pip install -U qwen-tts`). Not on this project's core
dependency list — optional, like voxcpm/kokoro-vietnamese; gated by
``available()``. Officially supports 10 languages (not Vietnamese), but
``language="Auto"`` has been verified to produce acceptable Vietnamese
output.
"""

import os


def _pick_device_dtype_attn():
    """Auto-detect device/dtype/attn-impl; ``QWEN3_TTS_DEVICE`` overrides."""
    import torch

    device = os.environ.get("QWEN3_TTS_DEVICE") or (
        "cuda:0"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    if device.startswith("cuda"):
        return device, torch.bfloat16, "flash_attention_2"
    if device == "mps":
        return device, torch.float16, None
    return device, torch.float32, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_tts_engines.py -k pick_device_dtype_attn -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/tts/providers/qwen3_tts_provider.py tests/unit/test_tts_engines.py
git commit -m "feat(tts): add device/dtype auto-detection for Qwen3-TTS"
```

---

### Task 2: Provider classes, registration, and generation routing

**Files:**
- Modify: `apps/api_gateway/app/services/tts/providers/qwen3_tts_provider.py`
- Modify: `apps/api_gateway/app/services/tts/service.py`
- Test: `tests/unit/test_tts_engines.py`

**Interfaces:**
- Consumes: `_pick_device_dtype_attn() -> tuple[str, "torch.dtype", str | None]` (Task 1); `module_available(module: str) -> bool` (`app.core.deps`); `float_array_to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes` (`app.core.audio`); `RenderingTTSProvider` (`app.services.tts.base`, requires implementing `async def _render_wav(self, payload: TTSRequest) -> bytes`); `TTSRequest` fields `text: str`, `language: str | None`, `voice: str | None`, `instruct: str | None`, `ref_audio_path: str | None`, `ref_text: str | None` (`app.schemas.tts`).
- Produces: `Qwen3TTS06BProvider` (`name = "qwen3_tts_0_6b"`), `Qwen3TTS17BProvider` (`name = "qwen3_tts_1_7b"`), `QWEN3_TTS_PROVIDERS: list` (both instances), `PRESET_SPEAKERS: list[dict]` (9 `{"label": str, "voice": str}` entries), `DEFAULT_SPEAKER = "Vivian"` — all importable from `app.services.tts.providers.qwen3_tts_provider`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tts_engines.py`:

```python
def _install_fake_qwen_tts(monkeypatch, model_cls):
    """qwen_tts is an optional dependency not installed in this test env, so
    `from qwen_tts import Qwen3TTSModel` inside _load_model() needs a stub
    module injected into sys.modules (mirrors _install_fake_edge_tts above)."""
    fake_mod = types.ModuleType("qwen_tts")
    fake_mod.Qwen3TTSModel = model_cls
    monkeypatch.setitem(sys.modules, "qwen_tts", fake_mod)


def test_lists_qwen3_tts_engines():
    engines = {e["engine"] for e in tts_service.list_engines()}
    assert {"qwen3_tts_0_6b", "qwen3_tts_1_7b"} <= engines


def test_qwen3_tts_install_hint_mentions_package():
    provider = tts_service.get_provider("qwen3_tts_0_6b")
    assert "qwen-tts" in provider.install_hint()


def test_qwen3_tts_voices_are_preset_speakers():
    from app.services.tts.providers.qwen3_tts_provider import PRESET_SPEAKERS

    voices = tts_service.get_provider("qwen3_tts_1_7b").list_voices()
    assert voices == PRESET_SPEAKERS
    assert len(voices) == 9
    assert {"label", "voice"} <= set(voices[0])


async def test_qwen3_tts_custom_voice_path_used_when_no_ref_audio(monkeypatch):
    from app.services.tts.providers import qwen3_tts_provider

    qwen3_tts_provider._CACHE.clear()
    calls = {}

    class _FakeModel:
        def generate_custom_voice(self, text, language, speaker, instruct):
            calls["custom_voice"] = (text, language, speaker, instruct)
            return np.array([0.0, 0.1, -0.1], dtype=np.float32), 24000

    class _FakeQwen3TTSModel:
        @staticmethod
        def from_pretrained(checkpoint_id, **kwargs):
            calls["checkpoint_id"] = checkpoint_id
            return _FakeModel()

    _install_fake_qwen_tts(monkeypatch, _FakeQwen3TTSModel)

    result = await tts_service.get_provider("qwen3_tts_0_6b").synthesize(TTSRequest(text="xin chào"))

    assert result.engine == "qwen3_tts_0_6b"
    assert result.sample_rate == 24000
    assert calls["checkpoint_id"] == "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    assert calls["custom_voice"] == ("xin chào", "Auto", "Vivian", None)


async def test_qwen3_tts_voice_clone_path_used_when_ref_audio_present(monkeypatch):
    from app.services.tts.providers import qwen3_tts_provider

    qwen3_tts_provider._CACHE.clear()
    calls = {}

    class _FakeModel:
        def generate_voice_clone(self, text, language, ref_audio, ref_text, x_vector_only_mode):
            calls["voice_clone"] = (text, language, ref_audio, ref_text, x_vector_only_mode)
            return np.array([0.2, -0.2], dtype=np.float32), 24000

    class _FakeQwen3TTSModel:
        @staticmethod
        def from_pretrained(checkpoint_id, **kwargs):
            calls["checkpoint_id"] = checkpoint_id
            return _FakeModel()

    _install_fake_qwen_tts(monkeypatch, _FakeQwen3TTSModel)

    payload = TTSRequest(text="hi", ref_audio_path="/tmp/ref.wav", ref_text="reference text")
    result = await tts_service.get_provider("qwen3_tts_1_7b").synthesize(payload)

    assert result.sample_rate == 24000
    assert calls["checkpoint_id"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    assert calls["voice_clone"] == ("hi", "Auto", "/tmp/ref.wav", "reference text", False)


async def test_qwen3_tts_custom_voice_honors_explicit_voice_and_instruct(monkeypatch):
    from app.services.tts.providers import qwen3_tts_provider

    qwen3_tts_provider._CACHE.clear()
    calls = {}

    class _FakeModel:
        def generate_custom_voice(self, text, language, speaker, instruct):
            calls["custom_voice"] = (text, language, speaker, instruct)
            return np.array([0.0], dtype=np.float32), 24000

    class _FakeQwen3TTSModel:
        @staticmethod
        def from_pretrained(checkpoint_id, **kwargs):
            return _FakeModel()

    _install_fake_qwen_tts(monkeypatch, _FakeQwen3TTSModel)

    payload = TTSRequest(text="hello", voice="Ryan", instruct="cheerful", language="English")
    await tts_service.get_provider("qwen3_tts_0_6b").synthesize(payload)

    assert calls["custom_voice"] == ("hello", "English", "Ryan", "cheerful")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_tts_engines.py -k qwen3_tts -v`
Expected: FAIL — `test_lists_qwen3_tts_engines` fails with `AssertionError` (engines not registered yet); the others fail with `ImportError`/`AttributeError` (`PRESET_SPEAKERS`, `_CACHE` don't exist yet).

- [ ] **Step 3: Implement the full provider**

Replace the full contents of `apps/api_gateway/app/services/tts/providers/qwen3_tts_provider.py` with:

```python
"""Qwen3-TTS (0.6B/1.7B) engine — voice clone (Base) + preset speakers (CustomVoice).

Package: qwen-tts (`pip install -U qwen-tts`). Not on this project's core
dependency list — optional, like voxcpm/kokoro-vietnamese; gated by
``available()``. Officially supports 10 languages (not Vietnamese), but
``language="Auto"`` has been verified to produce acceptable Vietnamese
output.
"""

import asyncio
import os

import numpy as np

from app.core.audio import float_array_to_wav_bytes
from app.core.deps import module_available
from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider

_CACHE: dict[str, object] = {}

DEFAULT_SPEAKER = "Vivian"

PRESET_SPEAKERS = [
    {"label": "Vivian (bright young female, Chinese)", "voice": "Vivian"},
    {"label": "Serena (warm young female, Chinese)", "voice": "Serena"},
    {"label": "Uncle_Fu (seasoned male, Chinese)", "voice": "Uncle_Fu"},
    {"label": "Dylan (youthful Beijing male, Chinese)", "voice": "Dylan"},
    {"label": "Eric (lively Sichuan male, Chinese)", "voice": "Eric"},
    {"label": "Ryan (dynamic male, English)", "voice": "Ryan"},
    {"label": "Aiden (sunny American male, English)", "voice": "Aiden"},
    {"label": "Ono_Anna (playful female, Japanese)", "voice": "Ono_Anna"},
    {"label": "Sohee (warm female, Korean)", "voice": "Sohee"},
]


def _pick_device_dtype_attn():
    """Auto-detect device/dtype/attn-impl; ``QWEN3_TTS_DEVICE`` overrides."""
    import torch

    device = os.environ.get("QWEN3_TTS_DEVICE") or (
        "cuda:0"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    if device.startswith("cuda"):
        return device, torch.bfloat16, "flash_attention_2"
    if device == "mps":
        return device, torch.float16, None
    return device, torch.float32, None


def _to_mono_f32(wav) -> np.ndarray:
    arr = np.asarray(wav, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=0) if arr.shape[0] < arr.shape[1] else arr.mean(axis=1)
    return arr.reshape(-1)


class _Qwen3TTSProviderBase(RenderingTTSProvider):
    """Shared logic for the 0.6B/1.7B Qwen3-TTS engines."""

    _size: str = ""
    _modules = ("qwen_tts", "torch")
    sample_rate = 24000  # overwritten with the real value after the first synth call

    def available(self) -> bool:
        return all(module_available(m) for m in self._modules)

    def install_hint(self) -> str:
        return (
            "pip install -U qwen-tts  "
            "(GPU/CUDA recommended; runs on CPU/MPS but slower, no flash-attention)"
        )

    def detail(self) -> str:
        return f"Qwen3-TTS {self._size} · 12Hz codec · Base+CustomVoice"

    def list_voices(self) -> list[dict]:
        return list(PRESET_SPEAKERS)

    def _checkpoint_id(self, kind: str) -> str:
        return f"Qwen/Qwen3-TTS-12Hz-{self._size}-{kind}"

    def _load_model(self, kind: str):
        key = f"{self.name}:{kind}"
        if key not in _CACHE:
            from qwen_tts import Qwen3TTSModel

            device, dtype, attn = _pick_device_dtype_attn()
            kwargs = {"device_map": device, "dtype": dtype}
            if attn is not None:
                kwargs["attn_implementation"] = attn
            _CACHE[key] = Qwen3TTSModel.from_pretrained(self._checkpoint_id(kind), **kwargs)
        return _CACHE[key]

    def _generate(self, payload: TTSRequest):
        language = payload.language or "Auto"
        if payload.ref_audio_path:
            model = self._load_model("Base")
            return model.generate_voice_clone(
                text=payload.text,
                language=language,
                ref_audio=payload.ref_audio_path,
                ref_text=payload.ref_text,
                x_vector_only_mode=False,
            )
        model = self._load_model("CustomVoice")
        return model.generate_custom_voice(
            text=payload.text,
            language=language,
            speaker=payload.voice or DEFAULT_SPEAKER,
            instruct=payload.instruct,
        )

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        wavs, sr = await asyncio.to_thread(self._generate, payload)
        self.sample_rate = int(sr)
        return float_array_to_wav_bytes(_to_mono_f32(wavs[0]), sample_rate=self.sample_rate)


class Qwen3TTS06BProvider(_Qwen3TTSProviderBase):
    name = "qwen3_tts_0_6b"
    _size = "0.6B"


class Qwen3TTS17BProvider(_Qwen3TTSProviderBase):
    name = "qwen3_tts_1_7b"
    _size = "1.7B"


QWEN3_TTS_PROVIDERS = [Qwen3TTS06BProvider(), Qwen3TTS17BProvider()]
```

Note: `generate_custom_voice`/`generate_voice_clone` are called with keyword arguments matching the fakes' parameter names exactly (`text`, `language`, `speaker`, `instruct` / `text`, `language`, `ref_audio`, `ref_text`, `x_vector_only_mode`) — this must match the real `qwen-tts` package's signature (confirmed via the project's README quickstart).

- [ ] **Step 4: Register both providers in the TTS service**

In `apps/api_gateway/app/services/tts/service.py`, add the import:

```python
from app.services.tts.providers.qwen3_tts_provider import QWEN3_TTS_PROVIDERS
```

placing it alongside the existing provider imports, alphabetically between `omnivoice_provider` and `vieneu_provider`. Then in `TTSService.__init__`, after the `EXTRA_TTS_PROVIDERS` loop, add:

```python
        for provider in QWEN3_TTS_PROVIDERS:
            self.providers[provider.name] = provider
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_tts_engines.py -v`
Expected: all tests in the file pass (previous tests plus the 5 new `qwen3_tts_*` tests).

- [ ] **Step 6: Run the full unit test suite to check for regressions**

Run: `pytest tests/unit -q`
Expected: all tests pass (no regressions in `test_tts_profile_*`, `test_tts_streaming.py`, etc. — none of them enumerate engine names exhaustively, so adding two new providers should not break them, but confirm).

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/tts/providers/qwen3_tts_provider.py \
        apps/api_gateway/app/services/tts/service.py \
        tests/unit/test_tts_engines.py
git commit -m "feat(tts): add Qwen3-TTS 0.6B/1.7B engines (voice clone + preset speakers)"
```

---

## Manual follow-up (not part of this plan)

To actually exercise real synthesis (not the stubbed tests), the user installs the real package themselves when ready:

```bash
pip install -U qwen-tts
```

First real call to either engine will download the relevant checkpoint(s) from HuggingFace (several GB per checkpoint) and, on this Mac, run on MPS with `float16` — expect it to be noticeably slower than the documented CUDA+FlashAttention2 numbers.
