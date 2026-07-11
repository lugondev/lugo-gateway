# edge-tts provider

## Problem

The project has five selectable TTS engines today (`omnivoice`, `vieneu`, `voxcpm2`, `kokoro_vi` — all local inference, CPU/MPS/GPU). None are a free, no-API-key, zero-local-compute option. [`rany2/edge-tts`](https://github.com/rany2/edge-tts) (PyPI `edge-tts`, pure-Python: aiohttp/certifi/tabulate/typing-extensions, no native binaries) wraps Microsoft Edge's "Read Aloud" cloud voice service over a websocket — no API key, no model download, no local GPU/CPU cost. Add it as a sixth engine, selectable from the existing multi-engine TTS test UI and `/v1/tts/synthesize` / `/v1/tts/stream` endpoints.

## Scope

- **In scope**: a new provider selectable for one-shot/batch synthesis (test UI, `/v1/tts/synthesize`, `/v1/tts/stream`).
- **Out of scope**: the live conversation pipeline (ESP32/RPi/browser real-time voice). `ConversationSession` (`app/services/conversation/session.py:595`) reads the TTS artifact via `wav_file_to_pcm16(result.audio_url...)`, which requires a real WAV file. edge-tts's native output is MP3; supporting the live path would require transcoding (new ffmpeg/PyAV dependency) plus accepting ~0.5-2s cloud round-trip latency into a real-time barge-in pipeline. Neither is worth it for this addition — `edge_tts` is not added to any default/warmup engine list, and no transcode dependency is introduced.

## Output format — native MP3, no transcoding

edge-tts's `Communicate.stream()` yields `{"type": "audio", "data": bytes}` chunks in a fixed format: `audio-24khz-48kbitrate-mono-mp3` (24kHz, 48kbps CBR, mono). Because this engine is test-UI/batch only (not the live PCM→Opus path), there's no need to decode to WAV — concatenate the audio chunks into one MP3 buffer and store it as-is. This avoids adding any decode dependency (ffmpeg/pydub/PyAV) that no other part of the project currently has.

- **Duration**: no decode step exists to measure it exactly, so approximate from the known constant bitrate: `duration_seconds = len(mp3_bytes) * 8 / 48000`. This is documented as an approximation (±1 frame), acceptable for a test-UI/batch engine.
- **Artifact storage**: add `ArtifactStore.save_mp3(data: bytes) -> tuple[str, str]` in `apps/api_gateway/app/services/artifacts.py`, mirroring the existing `save_wav` (writes a `.mp3` file, same `url_prefix` scheme). `StaticFiles` (mounted in `main.py:174`) already serves by extension/mimetype, so no other serving change is needed. `RenderingTTSProvider` is not reused (it hardcodes the WAV path); `EdgeTTSProvider` implements `TTSProvider` directly.

## Provider implementation

New file: `apps/api_gateway/app/services/tts/providers/edge_tts_provider.py`

```python
class EdgeTTSProvider(TTSProvider):
    name = "edge_tts"
    DEFAULT_VOICE = "vi-VN-HoaiMyNeural"
    VOICES = [
        {"label": "Hoài My (nữ)", "voice": "vi-VN-HoaiMyNeural"},
        {"label": "Nam Minh (nam)", "voice": "vi-VN-NamMinhNeural"},
    ]

    def available(self) -> bool:
        return module_available("edge_tts")

    def detail(self) -> str:
        return "Microsoft Edge TTS (cloud, no API key, network required)"

    def install_hint(self) -> str:
        return "pip install edge-tts"

    def list_voices(self) -> list[dict]:
        return self.VOICES

    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        # concatenate "audio" chunks from edge_tts.Communicate(...).stream(),
        # wrap edge_tts exceptions as ProviderError, save_mp3, return TTSResult
        ...
```

- **Voice**: `payload.voice` (falls back to `DEFAULT_VOICE` if unset) passed straight through to `edge_tts.Communicate(text, voice=...)`. Static curated list only (the two Vietnamese voices above) — matches the Kokoro-Vietnamese pattern (`{"label", "voice"}` shape). Any other Microsoft voice id can still be passed via `voice` even though it's not in the listed set; this engine is not restricted to Vietnamese, just curated-by-default.
- **Speed**: `payload.speed` (e.g. `1.2`) maps to edge-tts's `rate` param as a signed percentage string: `f"{round((speed - 1) * 100):+d}%"`, defaulting to `"+0%"` when `speed` is unset.
- **Unsupported fields**: `instruct`, `ref_audio_path`, `ref_text` are accepted by `TTSRequest` but ignored — edge-tts has no voice cloning or voice-design, only fixed cloud voices.
- **Errors**: wrap `edge_tts` exceptions (`NoAudioReceived`, `UnexpectedResponse`, `UnknownResponse`, `WebSocketError`) in `ProviderError`, matching the existing "no silent fallback" convention (`test_render_failure_raises_provider_error_no_silent_fallback`).

## Registration

- `apps/api_gateway/app/services/tts/service.py`: add `"edge_tts": EdgeTTSProvider()` to `TTSService.providers` alongside `omnivoice`/`vieneu`.
- Not added to `settings.default_tts_engine`, `conversation_tts_engine`, or `extra_warmup_tts_engines` — purely opt-in, test-UI/batch selection only.

## Dependency

`pyproject.toml`, new optional-dependency group:

```toml
# edge-tts — free cloud TTS via Microsoft Edge's Read Aloud service. No API key,
# no local model, but needs outbound network access; unofficial (reverse-engineered)
# API, test-UI/batch use only (see docs/superpowers/specs/2026-07-11-edge-tts-provider-design.md).
edge-tts = [
  "edge-tts>=7.2.8",
]
```

## Tests

Extend `tests/unit/test_tts_engines.py` following the existing per-engine pattern exactly:

- `test_lists_edge_tts` — `"edge_tts"` present in `tts_service.list_engines()`.
- `test_edge_tts_voices_shape` — `list_voices()` returns the curated list with `{"label", "voice"}` keys.

No network-dependent test is added (no live call to Microsoft's service in the test suite); synthesis failure/success paths are covered by the existing generic `available()`/`ProviderError` conventions rather than a new integration test.
