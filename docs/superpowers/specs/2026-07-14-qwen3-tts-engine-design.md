# Qwen3-TTS (0.6B + 1.7B) engine — design

## Context

The TTS service (`apps/api_gateway/app/services/tts/`) supports pluggable
engines behind a common `TTSProvider` interface: `omnivoice`, `vieneu`,
`edge_tts`, plus lazily-imported "extra" engines (`voxcpm2`, `kokoro_vi`) in
`providers/extra_engines.py`. Engines are optional deps gated by
`available()`/`install_hint()`; there is no central enum to update elsewhere
— an engine becomes selectable in TTS Profiles as soon as it's registered in
`TTSService.providers`.

Qwen3-TTS (Alibaba, Apache 2.0, HuggingFace, released Jan 2026) ships two
open-weight sizes, 0.6B and 1.7B, each with a `Base` checkpoint (voice
cloning from a reference clip) and a `CustomVoice` checkpoint (fixed preset
speakers). Officially supported languages are Chinese, English, Japanese,
Korean, German, French, Russian, Portuguese, Spanish, Italian — Vietnamese
is not on that list, but the user has already tested Vietnamese input with
`language="Auto"` and found the output acceptable. The reference docs
target CUDA + FlashAttention2; the user wants it usable on their Mac (CPU/
MPS) for dev and on a GPU host for prod.

Confirmed via web research (github.com/QwenLM/Qwen3-TTS, HuggingFace model
cards): pip package `qwen-tts`, import `from qwen_tts import Qwen3TTSModel`,
checkpoint ids `Qwen/Qwen3-TTS-12Hz-{0.6B,1.7B}-{Base,CustomVoice}`,
`Qwen3TTSModel.from_pretrained(checkpoint_id, device_map=..., dtype=...,
attn_implementation=...)`, `generate_voice_clone(text, language, ref_audio,
ref_text, x_vector_only_mode)` and `generate_custom_voice(text, language,
speaker, instruct)`, both returning `(wavs, sr)`. Preset speakers:
Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee.

## Decisions from brainstorming

- Run on both CPU/MPS (Mac dev) and GPU (prod) — auto-detect, no separate
  code paths.
- Support both checkpoint kinds (Base clone + CustomVoice presets), routed
  by whether `ref_audio_path` is present, mirroring `VoxCPM2Provider`.
- Two engines, one per size: `qwen3_tts_0_6b`, `qwen3_tts_1_7b`.

## Architecture

New file `apps/api_gateway/app/services/tts/providers/qwen3_tts_provider.py`
(separate from `extra_engines.py`, which is scoped to engines ported from
OmniVoice-Studio — Qwen3-TTS is a distinct integration with a different
loading/generation API shape).

```
_Qwen3TTSProviderBase(RenderingTTSProvider)   # shared logic
  ├── Qwen3TTS06BProvider   name = "qwen3_tts_0_6b"   _size = "0.6B"
  └── Qwen3TTS17BProvider   name = "qwen3_tts_1_7b"   _size = "1.7B"

QWEN3_TTS_PROVIDERS = [Qwen3TTS06BProvider(), Qwen3TTS17BProvider()]
```

`service.py` imports `QWEN3_TTS_PROVIDERS` and extends `self.providers`
with it, same loop shape as `EXTRA_TTS_PROVIDERS`.

## Request routing

`_Qwen3TTSProviderBase` picks checkpoint + generation call based on the
existing `TTSRequest` fields — no schema changes needed:

- `payload.ref_audio_path` set → checkpoint `...-Base`, call
  `generate_voice_clone(text=payload.text, language=payload.language or
  "Auto", ref_audio=payload.ref_audio_path, ref_text=payload.ref_text,
  x_vector_only_mode=False)`
- otherwise → checkpoint `...-CustomVoice`, call
  `generate_custom_voice(text=payload.text, language=payload.language or
  "Auto", speaker=payload.voice or DEFAULT_SPEAKER, instruct=payload.instruct)`

`DEFAULT_SPEAKER = "Vivian"`.

`list_voices()` returns the 9 preset speakers as
`[{"label": ..., "voice": ...}, ...]`, matching the
`KokoroVietnameseProvider.list_voices()` shape. Preset labels only apply to
CustomVoice mode; when the caller passes `ref_audio_path` the voice picker
is irrelevant, same as VoxCPM2 today.

## Device / dtype selection

```python
def _pick_device_dtype_attn():
    override = os.environ.get("QWEN3_TTS_DEVICE")
    if override:
        device = override
    elif torch.cuda.is_available():
        device = "cuda:0"
    elif torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"

    if device.startswith("cuda"):
        return device, torch.bfloat16, "flash_attention_2"
    if device == "mps":
        return device, torch.float16, None
    return device, torch.float32, None
```

`attn_implementation` is only passed through to `from_pretrained` when not
`None` (flash-attention isn't installable on CPU/MPS).

## Model caching

Module-level `_CACHE: dict[str, object] = {}` (own dict in this file, not
shared with `extra_engines.py`), keyed by `f"{self.name}:{checkpoint_kind}"`
(`"qwen3_tts_0_6b:base"`, `"qwen3_tts_0_6b:custom_voice"`, etc.) — up to 4
entries total across both sizes, each loaded on first use of that
checkpoint kind.

## Sample rate

`generate_voice_clone`/`generate_custom_voice` return `(wavs, sr)` per
call rather than a fixed sample rate baked into the model. Because
`RenderingTTSProvider.synthesize()` reads `self.sample_rate` right after
`_render_wav()` returns, `_Qwen3TTSProviderBase._render_wav` sets
`self.sample_rate = sr` before returning the encoded WAV bytes (sequential
per-request — no concurrent-call race since it's set then immediately
read within the same `synthesize()` call before the next request's
`_render_wav` runs... note: `self` is a shared singleton provider
instance, so this **is not concurrency-safe** if two requests to the same
provider interleave with different sample rates. In practice `sr` is
constant per checkpoint, so this is a non-issue; documented here as a known
simplification, not a hidden bug.)

## Install / availability

`_modules = ("qwen_tts",)`, gated via existing `module_available()` helper.
`_hint = "pip install -U qwen-tts  (GPU/CUDA recommended; runs on CPU/MPS
but slower, no flash-attention)"`. Not added to `pyproject.toml`, matching
`voxcpm`/`kokoro-vietnamese` (manual opt-in installs).

`detail()` returns e.g. `"Qwen3-TTS 0.6B · 12Hz codec · Base+CustomVoice"`.

## Testing

Following `tests/unit/test_tts_engines.py`'s existing pattern:

- `list_engines()` includes `qwen3_tts_0_6b` and `qwen3_tts_1_7b`
- `list_voices()` shape (empty list when `qwen_tts` isn't installed; 9
  `{"label", "voice"}` entries when it is / when stubbed)
- Stub the `qwen_tts` module into `sys.modules` (same technique as the
  `edge_tts` stubbing in the existing test file) to exercise routing logic
  (Base-vs-CustomVoice selection, default speaker, device/dtype/attn
  selection) without downloading real weights
- `_pick_device_dtype_attn()` unit-tested directly across
  cuda-available / mps-available / neither, plus the `QWEN3_TTS_DEVICE`
  override

## Out of scope

- No new `TTSRequest` fields (language/voice/ref_audio_path/instruct/ref_text
  all already exist and cover Qwen3-TTS's needs).
- No changes to `system_config.py`, frontend engine list, or TTS Profile
  validation — engines are picked up generically like `voxcpm2`/`kokoro_vi`.
- VoiceDesign checkpoint is not wired up (only Base + CustomVoice, per
  brainstorming decision).
