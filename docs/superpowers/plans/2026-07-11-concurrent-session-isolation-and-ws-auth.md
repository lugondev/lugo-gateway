# Concurrent Session Isolation + WebSocket Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let multiple profiles/sessions stream independently at the same time without cross-session state bleed, and require authentication on the voice WebSocket endpoints.

**Architecture:** Two independent fixes in the existing FastAPI/asyncio gateway (`apps/api_gateway/app/`): (1) thread STT model selection through as an explicit function parameter instead of a mutable process-global read on every transcribe call, and (2) add an auth check at the top of each WS route handler (before `accept()`), since Starlette's `BaseHTTPMiddleware`-based `AuthGuardMiddleware` never runs for websocket scope. A small unrelated hardening fix (single-flight lock) closes a benign double-build race in OmniVoice's shared voice-reference cache.

**Tech Stack:** Python 3.12, FastAPI/Starlette, pytest + pytest-asyncio (`asyncio_mode = "auto"`, no `@pytest.mark.asyncio` needed).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-11-concurrent-session-isolation-and-ws-auth-design.md` — read it before starting; every task below implements one of its components.
- Do not change `apply_stt_model()` / `whisper_manager.select()` / `set_active_qwen3_asr_model()` — they remain in place for the admin "System" page's model download/select flow (`app/api/routes/stt.py:/warm`, `app/main.py` boot warmup). Only the *live-session* mutate-then-reread pattern is removed.
- Do not touch `omnivoice_provider._active_model` — tracing its only call site (`app/services/tts_models.py:107`, an admin route) confirmed it isn't part of any live session race; out of scope.
- Every existing test must still pass unmodified in behavior (only signature additions with defaults — no call site should need to change unless it's one this plan explicitly touches).
- When `settings.admin_password` is unset (today's local/dev "auth disabled" state), WS auth must be a no-op, matching existing HTTP behavior — do not break local dev or existing tests that don't set `admin_password`.

---

### Task 1: STT providers accept an explicit `model` parameter

**Files:**
- Modify: `apps/api_gateway/app/services/stt/base.py:45-55`
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_provider.py:71-119`
- Modify: `apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py:119-178`
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_gemma_provider.py:56-62`
- Modify: `apps/api_gateway/app/services/stt/providers/remote_whisper_provider.py:22`
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_mlx_provider.py:78`
- Modify: `apps/api_gateway/app/services/stt/providers/vosk_provider.py:73`
- Modify: `apps/api_gateway/app/services/stt/providers/openrouter_provider.py:28`
- Test: `tests/unit/test_stt_model_param_isolation.py` (new)

**Interfaces:**
- Produces: `STTProvider.transcribe_bytes(self, audio_bytes: bytes, language: str | None = None, model: str | None = None) -> STTResult` — the new shared call shape every provider subclass implements. `model=None` means "use whatever this engine's process-global default currently is" (unchanged fallback behavior); a non-`None` value pins the exact model/session-cache-key to use for this call only, with no global mutation.
- Consumes: nothing from other tasks (this task only touches the provider layer).

- [ ] **Step 1: Write the failing test for `WhisperProvider` concurrent isolation**

Create `tests/unit/test_stt_model_param_isolation.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_stt_model_param_isolation.py -v`
Expected: FAIL — `TypeError: transcribe_bytes() got an unexpected keyword argument 'model'`

- [ ] **Step 3: Update `stt/base.py`'s abstract signature**

In `apps/api_gateway/app/services/stt/base.py`, replace:

```python
class STTProvider(ABC):
    name: str

    @abstractmethod
    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        raise NotImplementedError
```

with:

```python
class STTProvider(ABC):
    name: str

    @abstractmethod
    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        raise NotImplementedError
```

- [ ] **Step 4: Update `WhisperProvider`**

In `apps/api_gateway/app/services/stt/providers/whisper_provider.py`, replace:

```python
    def _load_model(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run scripts/setup_local_stt.sh"
            ) from exc

        model_name = get_active_whisper_model()
        key = self._cache_key(model_name)
```

with:

```python
    def _load_model(self, model: str | None = None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run scripts/setup_local_stt.sh"
            ) from exc

        model_name = model or get_active_whisper_model()
        key = self._cache_key(model_name)
```

Then replace:

```python
    def _do_transcribe(self, audio_bytes: bytes, language: str | None) -> str:
        model = self._load_model()
        temp_file_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                temp_file_path = f.name
            segments, _ = model.transcribe(
                temp_file_path,
                language=language,
                vad_filter=settings.whisper_vad_filter,
                beam_size=settings.whisper_beam_size,
                condition_on_previous_text=settings.whisper_condition_on_previous_text,
                initial_prompt=resolve_initial_prompt(
                    settings.whisper_initial_prompt, settings.stt_glossary_path
                ),
            )
            return " ".join(s.text.strip() for s in segments if s.text.strip())
        finally:
            if temp_file_path and os.path.isfile(temp_file_path):
                os.unlink(temp_file_path)

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        text = await asyncio.to_thread(self._do_transcribe, audio_bytes, language)
        return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
```

with:

```python
    def _do_transcribe(self, audio_bytes: bytes, language: str | None, model: str | None) -> str:
        whisper_model = self._load_model(model)
        temp_file_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                temp_file_path = f.name
            segments, _ = whisper_model.transcribe(
                temp_file_path,
                language=language,
                vad_filter=settings.whisper_vad_filter,
                beam_size=settings.whisper_beam_size,
                condition_on_previous_text=settings.whisper_condition_on_previous_text,
                initial_prompt=resolve_initial_prompt(
                    settings.whisper_initial_prompt, settings.stt_glossary_path
                ),
            )
            return " ".join(s.text.strip() for s in segments if s.text.strip())
        finally:
            if temp_file_path and os.path.isfile(temp_file_path):
                os.unlink(temp_file_path)

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        text = await asyncio.to_thread(self._do_transcribe, audio_bytes, language, model)
        return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/unit/test_stt_model_param_isolation.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Update `Qwen3AsrProvider`**

In `apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py`, replace:

```python
    def _mlx_session(self):
        model = get_active_qwen3_asr_model()
        key = f"mlx:{model}"
        if key not in _MODEL_CACHE:
            from mlx_qwen3_asr import Session

            _MODEL_CACHE[key] = Session(model)
        return _MODEL_CACHE[key]

    def _cuda_model(self):
        model = get_active_qwen3_asr_model()
        key = f"cuda:{model}"
        if key not in _MODEL_CACHE:
            import torch
            from qwen_asr import Qwen3ASRModel

            _MODEL_CACHE[key] = Qwen3ASRModel.from_pretrained(
                model,
                dtype=_cuda_dtype(torch),  # bf16 on Ampere+, fp16 on T4/Turing
                device_map=settings.qwen3_asr_device or "cuda:0",
                max_new_tokens=256,
            )
        return _MODEL_CACHE[key]

    def _transcribe(self, wav_path: str, language: str | None) -> str:
        backend = self._backend()
        lang = _LANG.get((language or "").lower())  # None => auto-detect
        if backend == "mlx":
            return _extract_text(self._mlx_session().transcribe(wav_path, language=lang))
        if backend == "cuda":
            return _extract_text(self._cuda_model().transcribe(audio=wav_path, language=lang))
        raise RuntimeError("Qwen3-ASR needs mlx-qwen3-asr (Apple) or qwen-asr (CUDA) installed")
```

with:

```python
    def _mlx_session(self, model: str | None = None):
        resolved = model or get_active_qwen3_asr_model()
        key = f"mlx:{resolved}"
        if key not in _MODEL_CACHE:
            from mlx_qwen3_asr import Session

            _MODEL_CACHE[key] = Session(resolved)
        return _MODEL_CACHE[key]

    def _cuda_model(self, model: str | None = None):
        resolved = model or get_active_qwen3_asr_model()
        key = f"cuda:{resolved}"
        if key not in _MODEL_CACHE:
            import torch
            from qwen_asr import Qwen3ASRModel

            _MODEL_CACHE[key] = Qwen3ASRModel.from_pretrained(
                resolved,
                dtype=_cuda_dtype(torch),  # bf16 on Ampere+, fp16 on T4/Turing
                device_map=settings.qwen3_asr_device or "cuda:0",
                max_new_tokens=256,
            )
        return _MODEL_CACHE[key]

    def _transcribe(self, wav_path: str, language: str | None, model: str | None = None) -> str:
        backend = self._backend()
        lang = _LANG.get((language or "").lower())  # None => auto-detect
        if backend == "mlx":
            return _extract_text(self._mlx_session(model).transcribe(wav_path, language=lang))
        if backend == "cuda":
            return _extract_text(self._cuda_model(model).transcribe(audio=wav_path, language=lang))
        raise RuntimeError("Qwen3-ASR needs mlx-qwen3-asr (Apple) or qwen-asr (CUDA) installed")
```

Then replace:

```python
    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            # Run on the single dedicated thread so the model is built once and all MLX
            # work stays thread-pinned (see _INFER_EXECUTOR above).
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(_INFER_EXECUTOR, self._transcribe, tmp, language)
            return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)
```

with:

```python
    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            # Run on the single dedicated thread so the model is built once and all MLX
            # work stays thread-pinned (see _INFER_EXECUTOR above).
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(_INFER_EXECUTOR, self._transcribe, tmp, language, model)
            return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)
```

Note: `warm()` still calls `self._mlx_session()` / `self._cuda_model()` with no `model` arg — leave it unchanged, it should keep warming whatever the process-global default currently is (there's no specific session to warm for at boot/admin-warm time).

- [ ] **Step 7: Add a Qwen3-ASR isolation test**

Append to `tests/unit/test_stt_model_param_isolation.py`:

```python
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
```

- [ ] **Step 8: Update `WhisperGemmaProvider`**

In `apps/api_gateway/app/services/stt/providers/whisper_gemma_provider.py`, replace:

```python
    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        base = await self._whisper.transcribe_bytes(audio_bytes, language)
```

with:

```python
    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        base = await self._whisper.transcribe_bytes(audio_bytes, language, model)
```

- [ ] **Step 9: Update the four single-fixed-model providers to accept (and ignore) `model`**

These engines have no variant registry (`app/services/stt/model_registry.py:STT_MODEL_REGISTRIES` doesn't list them) — accept the parameter for interface consistency, don't use it.

In `apps/api_gateway/app/services/stt/providers/remote_whisper_provider.py`, replace:

```python
    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
```

with:

```python
    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
```

In `apps/api_gateway/app/services/stt/providers/whisper_mlx_provider.py`, replace:

```python
    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
```

with:

```python
    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
```

In `apps/api_gateway/app/services/stt/providers/vosk_provider.py`, replace:

```python
    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
```

with:

```python
    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
```

Note: this method has an existing local variable also named `model` (`model = _load_vosk_model()`, a few lines below the signature) — it shadows the new parameter immediately. That's fine here (the parameter is intentionally unused by this fixed-model engine), just don't rename the local — leave the body untouched.

In `apps/api_gateway/app/services/stt/providers/openrouter_provider.py`, replace:

```python
    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
```

with:

```python
    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
```

- [ ] **Step 10: Run the full unit test suite for this task**

Run: `pytest tests/unit/test_stt_model_param_isolation.py tests/test_qwen3_asr.py tests/unit/test_whisper_models.py tests/unit/test_qwen3_asr_model.py tests/unit/test_provider_single_flight_load.py -v`
Expected: all PASS (no regressions in existing whisper/qwen3 tests)

- [ ] **Step 11: Commit**

```bash
git add apps/api_gateway/app/services/stt/base.py \
        apps/api_gateway/app/services/stt/providers/whisper_provider.py \
        apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py \
        apps/api_gateway/app/services/stt/providers/whisper_gemma_provider.py \
        apps/api_gateway/app/services/stt/providers/remote_whisper_provider.py \
        apps/api_gateway/app/services/stt/providers/whisper_mlx_provider.py \
        apps/api_gateway/app/services/stt/providers/vosk_provider.py \
        apps/api_gateway/app/services/stt/providers/openrouter_provider.py \
        tests/unit/test_stt_model_param_isolation.py
git commit -m "fix(stt): thread model id explicitly through transcribe_bytes instead of a global"
```

---

### Task 2: Wire `ConversationSession` + livehost route to pass the model explicitly

**Files:**
- Modify: `apps/api_gateway/app/services/stt/model_registry.py` (add `resolve_default_stt_model`)
- Modify: `apps/api_gateway/app/services/conversation/session.py:42,116-140,185-192,479-497`
- Modify: `apps/api_gateway/app/api/routes/livehost.py:22,117-121,296-306`
- Modify: 17 test-stub `transcribe_bytes` signatures across `tests/` (mechanical, see Step 5)
- Test: `tests/unit/test_conversation_session_core.py` (extend), `tests/integration/test_livehost_ws_voice.py` (extend)

**Interfaces:**
- Consumes: `STTProvider.transcribe_bytes(self, audio_bytes, language=None, model=None)` from Task 1.
- Produces: `resolve_default_stt_model(engine: str) -> str | None` (new, in `app/services/stt/model_registry.py`) — the model id to use when a profile/query didn't pin one; `ConversationSession.stt_model_id: str | None` (new instance attribute) — the model id resolved once at session start, passed explicitly to every transcribe call for that session.

- [ ] **Step 1: Write the failing test — session-level isolation**

Add to `tests/unit/test_conversation_session_core.py` (after the existing imports/fixtures at the top of the file):

```python
class _RecordingSTT(STTProvider):
    name = "stub-record-stt"

    def __init__(self):
        self.seen_models: list[str | None] = []

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        self.seen_models.append(model)
        return STTResult(engine=self.name, text=f"heard:{model}", is_final=True)


async def test_two_sessions_transcribe_with_their_own_pinned_model():
    """Session A pins 'small', session B pins 'medium' on the same engine. Running
    a turn on each — even interleaved — must not let B's model affect A's turn."""
    provider = _RecordingSTT()
    stt_service.providers["stub-record-stt"] = provider
    try:
        events_a: list = []
        events_b: list = []

        async def emit_a(name, **p):
            events_a.append((name, p))

        async def emit_b(name, **p):
            events_b.append((name, p))

        async def noop_audio(pkt):
            pass

        sess_a = ConversationSession(
            _cfg(stt_engine="stub-record-stt", stt_model="small"), emit_a, noop_audio
        )
        sess_b = ConversationSession(
            _cfg(stt_engine="stub-record-stt", stt_model="medium"), emit_b, noop_audio
        )
        await sess_a.start()
        await sess_b.start()

        assert sess_a.stt_model_id == "small"
        assert sess_b.stt_model_id == "medium"

        await sess_a.close()
        await sess_b.close()
    finally:
        stt_service.providers.pop("stub-record-stt", None)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_conversation_session_core.py -k test_two_sessions_transcribe_with_their_own_pinned_model -v`
Expected: FAIL — `AttributeError: 'ConversationSession' object has no attribute 'stt_model_id'`

- [ ] **Step 3: Add `resolve_default_stt_model` to `model_registry.py`**

In `apps/api_gateway/app/services/stt/model_registry.py`, append after `apply_stt_model`:

```python
def resolve_default_stt_model(engine: str) -> str | None:
    """The model id a session should snapshot when its profile/query didn't pin
    one — whatever this engine's process-global default currently is. None for
    engines with no variant registry (single fixed model, e.g. vosk/whisper_mlx)."""
    if engine in ("whisper", "whisper_local", "whisper_gemma"):
        return whisper_manager.snapshot()["active"]
    if engine == "qwen3_asr":
        return get_active_qwen3_asr_model()
    return None
```

- [ ] **Step 4: Wire `ConversationSession`**

In `apps/api_gateway/app/services/conversation/session.py`, replace the import line:

```python
from app.services.stt.model_registry import apply_stt_model
```

with:

```python
from app.services.stt.model_registry import resolve_default_stt_model
```

In `ConversationSession.__init__`, replace:

```python
        # Set in start().
        self.profile = None
        self.stt_provider = None
        self.tts_provider = None
```

with:

```python
        # Set in start().
        self.profile = None
        self.stt_provider = None
        self.stt_model_id: str | None = None
        self.tts_provider = None
```

In `ConversationSession.start()`, replace:

```python
        if cfg.stt_model:
            try:
                apply_stt_model(cfg.stt_engine, cfg.stt_model)
            except AppError as exc:
                logger.warning(
                    "stt model override skipped (%s/%s): %s", cfg.stt_engine, cfg.stt_model, exc
                )
        self.stt_provider = stt_service.get_provider(cfg.stt_engine)
```

with:

```python
        self.stt_model_id = cfg.stt_model or resolve_default_stt_model(cfg.stt_engine)
        self.stt_provider = stt_service.get_provider(cfg.stt_engine)
```

Note: leave the existing `from app.core.errors import AppError` import in `session.py` untouched — it's still used by the fast-path routing's `except AppError:` a few lines below (Task 2 Step 4's next edit).

In `ConversationSession._run_turn()`, replace:

```python
        # Fast-path routing: short commands can go to a lower-latency engine.
        turn_provider = self.stt_provider
        turn_engine = cfg.stt_engine
        if speech_ms and settings.conversation_fast_stt_engine:
            chosen = select_stt_engine(
                speech_ms,
                cfg.stt_engine,
                settings.conversation_fast_stt_engine,
                settings.conversation_fast_stt_max_ms,
            )
            if chosen != cfg.stt_engine:
                try:
                    turn_provider = stt_service.get_provider(chosen)
                    turn_engine = chosen
                except AppError:
                    logger.info("fast STT engine %s unavailable; using %s", chosen, cfg.stt_engine)

        try:
            stt_result = await turn_provider.transcribe_bytes(wav, cfg.language)
        except RuntimeError as exc:
            await self.emit("error", message=f"STT failed: {exc}")
            return
```

with:

```python
        # Fast-path routing: short commands can go to a lower-latency engine.
        turn_provider = self.stt_provider
        turn_engine = cfg.stt_engine
        turn_model = self.stt_model_id
        if speech_ms and settings.conversation_fast_stt_engine:
            chosen = select_stt_engine(
                speech_ms,
                cfg.stt_engine,
                settings.conversation_fast_stt_engine,
                settings.conversation_fast_stt_max_ms,
            )
            if chosen != cfg.stt_engine:
                try:
                    turn_provider = stt_service.get_provider(chosen)
                    turn_engine = chosen
                    turn_model = None  # different engine — this session's model pin doesn't apply
                except AppError:
                    logger.info("fast STT engine %s unavailable; using %s", chosen, cfg.stt_engine)

        try:
            stt_result = await turn_provider.transcribe_bytes(wav, cfg.language, model=turn_model)
        except RuntimeError as exc:
            await self.emit("error", message=f"STT failed: {exc}")
            return
```

- [ ] **Step 5: Update all existing test-stub `transcribe_bytes` signatures**

15 test files define a stub `STTProvider` subclass whose `transcribe_bytes` doesn't accept `model=` yet; since `ConversationSession`/livehost now always pass `model=` as a keyword, every stub must accept it (even if unused) or calls into it will raise `TypeError`. Run this from the repo root — it rewrites all three signature variants found in `tests/`:

```bash
cd /Users/lugon/code/speech-text-transformer
grep -rl 'async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:' tests | \
  xargs sed -i '' 's/async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:/async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:/'

grep -rl 'async def transcribe_bytes(self, audio_bytes, language=None):$' tests | \
  xargs sed -i '' 's/async def transcribe_bytes(self, audio_bytes, language=None):/async def transcribe_bytes(self, audio_bytes, language=None, model=None):/'

grep -rl 'async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:' tests | \
  xargs sed -i '' 's/async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:/async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None, model: str | None = None) -> STTResult:/'
```

Verify no stub was missed:

```bash
grep -rn "async def transcribe_bytes" tests | grep -v "model"
```

Expected: no output (every match now includes `model` in its signature).

- [ ] **Step 6: Run the test to verify it passes**

Run: `pytest tests/unit/test_conversation_session_core.py -v`
Expected: all PASS, including `test_two_sessions_transcribe_with_their_own_pinned_model`

- [ ] **Step 7: Wire the livehost route**

In `apps/api_gateway/app/api/routes/livehost.py`, replace the import:

```python
from app.services.stt.model_registry import apply_stt_model
```

with:

```python
from app.services.stt.model_registry import resolve_default_stt_model
```

Replace:

```python
    if stt_model:
        try:
            apply_stt_model(stt_engine, stt_model)
        except AppError as exc:
            logger.warning("stt model override skipped (%s/%s): %s", stt_engine, stt_model, exc)
```

with:

```python
    resolved_stt_model = stt_model or resolve_default_stt_model(stt_engine)
```

Replace, inside `_run_voice_turn`:

```python
            try:
                stt_result = await stt_provider.transcribe_bytes(wav, language)
```

with:

```python
            try:
                stt_result = await stt_provider.transcribe_bytes(wav, language, model=resolved_stt_model)
```

- [ ] **Step 8: Add a livehost integration test**

Add to `tests/integration/test_livehost_ws_voice.py` (this file already defines `_loud()`/`_silence()` PCM helpers and a `_StubTTS`/`"stub-livehost-tts"` provider registered by the autouse `_register_stub` fixture — reuse both, same pattern as `test_livehost_voice_turn_end_to_end` above):

```python
def test_livehost_stream_passes_resolved_model_to_stt(monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.livehost.resolve_default_stt_model",
        lambda engine: "sentinel-model",
    )

    seen: list = []

    class _RecordingStub(STTProvider):
        name = "stub-livehost-record"

        async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
            seen.append(model)
            return STTResult(engine=self.name, text="ok", is_final=True)

    stt_service.providers["stub-livehost-record"] = _RecordingStub()
    try:
        client = TestClient(app)
        url = (
            "/v1/livehost/stream?stt_engine=stub-livehost-record"
            "&tts_engine=stub-livehost-tts&sample_rate=16000"
        )
        with client.websocket_connect(url) as ws:
            started = ws.receive_json()
            assert started["event"] == "session_started"

            ws.send_bytes(_loud(500))
            ws.send_bytes(_silence(500))
            ws.send_bytes(_silence(500))

            for _ in range(20):
                ev = ws.receive_json()
                if ev["event"] == "turn_done":
                    break
    finally:
        stt_service.providers.pop("stub-livehost-record", None)

    assert seen == ["sentinel-model"]
```

- [ ] **Step 9: Run it to verify it passes**

Run: `pytest tests/integration/test_livehost_ws_voice.py -v`
Expected: all PASS

- [ ] **Step 10: Run the full test suite to check for regressions**

Run: `pytest tests -v`
Expected: all PASS (0 failures)

- [ ] **Step 11: Commit**

```bash
git add apps/api_gateway/app/services/stt/model_registry.py \
        apps/api_gateway/app/services/conversation/session.py \
        apps/api_gateway/app/api/routes/livehost.py \
        tests/
git commit -m "fix(conversation): resolve STT model once per session instead of mutating a global"
```

---

### Task 3: WebSocket authentication

**Files:**
- Modify: `apps/api_gateway/app/core/settings.py` (add `device_auth_token`)
- Modify: `apps/api_gateway/app/core/auth_guard.py` (add `ws_authenticated`)
- Modify: `apps/api_gateway/app/api/routes/conversation.py:165-167`
- Modify: `apps/api_gateway/app/api/routes/stt.py:141-143`
- Modify: `apps/api_gateway/app/api/routes/livehost.py:75-77`
- Test: `tests/unit/test_auth_guard.py` (extend), `tests/integration/test_ws_auth.py` (new)

**Interfaces:**
- Produces: `ws_authenticated(websocket: WebSocket) -> bool` in `app.core.auth_guard` — call it as the very first line of a WS route handler, before `await websocket.accept()`; if it returns `False`, close with `code=4401` and return without accepting.
- Consumes: nothing from Tasks 1-2 (independent fix).

- [ ] **Step 1: Write the failing unit test for `ws_authenticated`**

Add to `tests/unit/test_auth_guard.py`:

```python
class _FakeWebSocket:
    def __init__(self, session: dict | None = None, query_params: dict | None = None):
        self.session = session or {}
        self.query_params = query_params or {}


def test_ws_auth_noop_when_admin_password_unset():
    from app.core.auth_guard import ws_authenticated

    assert settings.admin_password == ""
    assert ws_authenticated(_FakeWebSocket()) is True


def test_ws_auth_accepts_valid_browser_cookie_session(_with_password):
    from app.core.auth_guard import ws_authenticated

    assert ws_authenticated(_FakeWebSocket(session={"authenticated": True})) is True


def test_ws_auth_rejects_missing_cookie_and_missing_token(_with_password):
    from app.core.auth_guard import ws_authenticated

    assert ws_authenticated(_FakeWebSocket()) is False


def test_ws_auth_accepts_valid_device_token(_with_password, monkeypatch):
    from app.core.auth_guard import ws_authenticated

    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    ws = _FakeWebSocket(query_params={"device_token": "d3vice-secret"})
    assert ws_authenticated(ws) is True


def test_ws_auth_rejects_wrong_device_token(_with_password, monkeypatch):
    from app.core.auth_guard import ws_authenticated

    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    ws = _FakeWebSocket(query_params={"device_token": "wrong"})
    assert ws_authenticated(ws) is False


def test_ws_auth_rejects_device_token_when_none_configured(_with_password):
    from app.core.auth_guard import ws_authenticated

    ws = _FakeWebSocket(query_params={"device_token": "anything"})
    assert ws_authenticated(ws) is False
```

This file's existing `_with_password` fixture (already defined above these new tests) sets/restores `settings.admin_password`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_auth_guard.py -v`
Expected: FAIL — `ImportError: cannot import name 'ws_authenticated' from 'app.core.auth_guard'`

- [ ] **Step 3: Add the `device_auth_token` setting**

In `apps/api_gateway/app/core/settings.py`, replace:

```python
    # Browser control-panel login (single shared password). Empty = auth disabled.
    admin_password: str = ""
    # Cookie-signing secret for the login session. Empty (with admin_password set)
    # -> a random secret is generated at process startup (sessions reset on restart).
    session_secret: str = ""
```

with:

```python
    # Browser control-panel login (single shared password). Empty = auth disabled.
    admin_password: str = ""
    # Cookie-signing secret for the login session. Empty (with admin_password set)
    # -> a random secret is generated at process startup (sessions reset on restart).
    session_secret: str = ""
    # Shared secret for ESP32/RPi device WS clients, which can't do a browser
    # cookie login. Empty = device WS connections are rejected while
    # admin_password is set (browsers still work via cookie session).
    device_auth_token: str = ""
```

- [ ] **Step 4: Add `ws_authenticated` to `auth_guard.py`**

In `apps/api_gateway/app/core/auth_guard.py`, add after the existing imports:

```python
from starlette.websockets import WebSocket
```

Append at the end of the file:

```python
def ws_authenticated(websocket: WebSocket) -> bool:
    """Auth check for WS handshakes — AuthGuardMiddleware can't run here since
    BaseHTTPMiddleware never runs for websocket scope. Browsers reuse the same
    cookie session as the HTTP UI; devices (no browser login flow) use a shared
    token passed as a query param at connect time."""
    if not settings.admin_password:
        return True
    if websocket.session.get("authenticated"):
        return True
    token = websocket.query_params.get("device_token")
    return bool(settings.device_auth_token) and token == settings.device_auth_token
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/unit/test_auth_guard.py -v`
Expected: all PASS

- [ ] **Step 6: Write the failing integration test for the three WS routes**

Create `tests/integration/test_ws_auth.py`. Note: for the two "accepts" tests, each route must be given a *lightweight stub* engine via query params — `/v1/conversation/stream` and `/v1/livehost/stream` both fire a background `_warm_and_notify()` task on session start that calls `.warm()` on the resolved STT/TTS providers; left at their real defaults (`whisper`/`omnivoice`) that would try to load/download an actual model during the test. `/v1/stt/stream` doesn't warm anything at connect time, so its default (`vosk`) is already safe as-is.

```python
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-ws-auth-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="ok", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-ws-auth-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _register_stub_engines():
    stt_service.providers["stub-ws-auth-stt"] = _StubSTT()
    tts_service.providers["stub-ws-auth-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-ws-auth-stt", None)
    tts_service.providers.pop("stub-ws-auth-tts", None)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")
    monkeypatch.setattr(settings, "device_auth_token", "")


# (path, query string appended for the "accepts" tests only — irrelevant for the
# rejection test, since auth is checked before any query param is read)
ROUTES = [
    ("/v1/conversation/stream", "stt_engine=stub-ws-auth-stt&tts_engine=stub-ws-auth-tts"),
    ("/v1/stt/stream", "engine=vosk"),
    ("/v1/livehost/stream", "stt_engine=stub-ws-auth-stt&tts_engine=stub-ws-auth-tts"),
]


@pytest.mark.parametrize("path,query", ROUTES)
def test_ws_rejects_unauthenticated_connection(client, _with_password, path, query):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(path):
            pass
    assert exc_info.value.code == 4401


@pytest.mark.parametrize("path,query", ROUTES)
def test_ws_accepts_valid_device_token(client, _with_password, path, query):
    with client.websocket_connect(f"{path}?{query}&device_token=d3vice-secret") as ws:
        first = ws.receive_json()
        assert first  # session_started — past the auth gate


@pytest.mark.parametrize("path,query", ROUTES)
def test_ws_accepts_valid_browser_cookie(client, _with_password, path, query):
    login = client.post("/api/auth/login", json={"password": "s3cret"})
    assert login.status_code == 200
    with client.websocket_connect(f"{path}?{query}") as ws:
        first = ws.receive_json()
        assert first
```

- [ ] **Step 7: Run the test to verify it fails**

Run: `pytest tests/integration/test_ws_auth.py -v`
Expected: FAIL — all three endpoints currently accept the connection unconditionally (no `WebSocketDisconnect` raised for the unauthenticated case)

- [ ] **Step 8: Apply the check in `conversation.py`**

In `apps/api_gateway/app/api/routes/conversation.py`, add the import:

```python
from app.core.auth_guard import ws_authenticated
```

Replace:

```python
@router.websocket("/stream")
async def conversation_stream(websocket: WebSocket) -> None:
    await websocket.accept()
```

with:

```python
@router.websocket("/stream")
async def conversation_stream(websocket: WebSocket) -> None:
    if not ws_authenticated(websocket):
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
```

- [ ] **Step 9: Apply the check in `stt.py`**

In `apps/api_gateway/app/api/routes/stt.py`, add the import:

```python
from app.core.auth_guard import ws_authenticated
```

Replace:

```python
@router.websocket("/stream")
async def stt_stream(websocket: WebSocket) -> None:
    await websocket.accept()
```

with:

```python
@router.websocket("/stream")
async def stt_stream(websocket: WebSocket) -> None:
    if not ws_authenticated(websocket):
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
```

- [ ] **Step 10: Apply the check in `livehost.py`**

In `apps/api_gateway/app/api/routes/livehost.py`, add the import:

```python
from app.core.auth_guard import ws_authenticated
```

Replace:

```python
@router.websocket("/stream")
async def livehost_stream(websocket: WebSocket) -> None:
    await websocket.accept()
```

with:

```python
@router.websocket("/stream")
async def livehost_stream(websocket: WebSocket) -> None:
    if not ws_authenticated(websocket):
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
```

- [ ] **Step 11: Run the integration test to verify it passes**

Run: `pytest tests/integration/test_ws_auth.py -v`
Expected: all PASS

- [ ] **Step 12: Run the full test suite to check for regressions**

Run: `pytest tests -v`
Expected: all PASS — in particular, every existing WS integration test (`test_conversation_ws.py`, `test_stt_ws.py`, `test_livehost_ws_voice.py`, `test_livehost_ws_social.py`, `test_gateway_modalities.py`, `test_opus_transport.py`) must be unaffected, since none of them set `admin_password` and `ws_authenticated` is a no-op in that case.

- [ ] **Step 13: Commit**

```bash
git add apps/api_gateway/app/core/settings.py \
        apps/api_gateway/app/core/auth_guard.py \
        apps/api_gateway/app/api/routes/conversation.py \
        apps/api_gateway/app/api/routes/stt.py \
        apps/api_gateway/app/api/routes/livehost.py \
        tests/unit/test_auth_guard.py \
        tests/integration/test_ws_auth.py
git commit -m "feat(auth): require auth on voice WS endpoints (cookie for browsers, token for devices)"
```

---

### Task 4: OmniVoice single-flight voice-ref lock

**Files:**
- Modify: `apps/api_gateway/app/services/tts/providers/omnivoice_provider.py:1-22,65-75`
- Test: `tests/unit/test_omnivoice_provider.py` (extend)

**Interfaces:**
- Consumes: nothing from Tasks 1-3 (independent hardening fix).
- Produces: no new public interface — `_ensure_voice_ref()` behavior is unchanged for the caller (`_render_wav`), only its cold-start concurrency safety improves.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_omnivoice_provider.py`:

```python
async def test_ensure_voice_ref_is_single_flight_under_concurrent_cold_start(monkeypatch):
    from app.services.tts.providers import omnivoice_provider as ov_mod

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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_omnivoice_provider.py -k single_flight -v`
Expected: FAIL — `assert 8 == 1` (all 8 concurrent calls synthesize independently)

- [ ] **Step 3: Add the lock**

In `apps/api_gateway/app/services/tts/providers/omnivoice_provider.py`, replace:

```python
import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 24000  # OmniVoice audio tokenizer rate.

# Runtime-selected model repo id; falls back to settings. Reset on restart.
_active_model: str | None = None

# Process-wide pinned voice reference {"path", "text"} cloned for every chunk.
_voice_ref: dict[str, str] = {}
```

with:

```python
import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 24000  # OmniVoice audio tokenizer rate.

# Runtime-selected model repo id; falls back to settings. Reset on restart.
_active_model: str | None = None

# Process-wide pinned voice reference {"path", "text"} cloned for every chunk.
_voice_ref: dict[str, str] = {}
# Guards the check-then-build in _ensure_voice_ref: concurrent cold-start calls
# (e.g. two sessions' first turn landing at the same moment) would otherwise each
# synthesize the reference independently — wasted work, not incorrect output,
# since both would build the same thing from the same global settings.
_voice_ref_lock = asyncio.Lock()
```

Then replace:

```python
    async def _ensure_voice_ref(self) -> dict[str, str]:
        """Generate a fixed reference voice once; reused (cloned) for every chunk."""
        if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
            return _voice_ref
        ref_dir = Path(settings.artifacts_dir).resolve()
        ref_dir.mkdir(parents=True, exist_ok=True)
        ref_path = str(ref_dir / "_omnivoice_voice_ref.wav")
        wav = await self._synth(settings.omnivoice_ref_text, instruct=settings.omnivoice_default_instruct)
        Path(ref_path).write_bytes(wav)
        _voice_ref.update({"path": ref_path, "text": settings.omnivoice_ref_text})
        return _voice_ref
```

with:

```python
    async def _ensure_voice_ref(self) -> dict[str, str]:
        """Generate a fixed reference voice once; reused (cloned) for every chunk."""
        if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
            return _voice_ref
        async with _voice_ref_lock:
            if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
                return _voice_ref
            ref_dir = Path(settings.artifacts_dir).resolve()
            ref_dir.mkdir(parents=True, exist_ok=True)
            ref_path = str(ref_dir / "_omnivoice_voice_ref.wav")
            wav = await self._synth(
                settings.omnivoice_ref_text, instruct=settings.omnivoice_default_instruct
            )
            Path(ref_path).write_bytes(wav)
            _voice_ref.update({"path": ref_path, "text": settings.omnivoice_ref_text})
            return _voice_ref
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/unit/test_omnivoice_provider.py -v`
Expected: all PASS

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `pytest tests -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/tts/providers/omnivoice_provider.py tests/unit/test_omnivoice_provider.py
git commit -m "fix(tts): single-flight OmniVoice's shared voice-ref build under concurrent cold start"
```
