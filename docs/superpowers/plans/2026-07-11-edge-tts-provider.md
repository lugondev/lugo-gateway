# edge-tts Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `edge_tts` as a sixth selectable TTS engine (Microsoft Edge's free cloud TTS via the `rany2/edge-tts` PyPI package), for test-UI/batch synthesis only.

**Architecture:** A new `TTSProvider` subclass (not `RenderingTTSProvider`, which hardcodes WAV output) streams MP3 chunks from `edge_tts.Communicate(...).stream()`, concatenates them, stores the raw MP3 via a new `ArtifactStore.save_mp3`, and estimates duration from the fixed CBR bitrate — no audio decoding, no new decode dependency.

**Tech Stack:** Python 3.12, FastAPI, `edge-tts` (pure-Python: aiohttp/certifi/tabulate/typing-extensions), pytest.

## Global Constraints

- Full design: `docs/superpowers/specs/2026-07-11-edge-tts-provider-design.md`.
- Engine name is `edge_tts` (snake_case, matches `omnivoice`/`vieneu`/`voxcpm2`/`kokoro_vi`).
- Not added to any default/warmup engine list (`settings.default_tts_engine`, `conversation_tts_engine`, `extra_warmup_tts_engines`) — opt-in only.
- No live-conversation-pipeline support and no MP3→WAV transcoding — out of scope per the spec.
- Voice list is static: `vi-VN-HoaiMyNeural` (default) and `vi-VN-NamMinhNeural` only.
- No network-dependent test may be added (no test may call the real Microsoft service).

---

### Task 1: `ArtifactStore.save_mp3`

**Files:**
- Modify: `apps/api_gateway/app/services/artifacts.py`
- Test: Create `tests/unit/test_artifacts.py`

**Interfaces:**
- Consumes: nothing new (existing `ArtifactStore.__init__`, `self.base_dir`, `self.url_prefix`).
- Produces: `ArtifactStore.save_mp3(data: bytes) -> tuple[str, str]` — same contract as `save_wav`: returns `(artifact_id, public_url)`, writes `{artifact_id}.mp3` under `self.base_dir`, URL is `f"{self.url_prefix}/{filename}"`. Task 2 calls this.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_artifacts.py
from app.services.artifacts import artifact_store


def test_save_mp3_writes_file_and_returns_url():
    data = b"fake-mp3-bytes"
    artifact_id, url = artifact_store.save_mp3(data)

    assert url == f"{artifact_store.url_prefix}/{artifact_id}.mp3"
    saved_path = artifact_store.base_dir / f"{artifact_id}.mp3"
    assert saved_path.read_bytes() == data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_artifacts.py -v`
Expected: FAIL with `AttributeError: 'ArtifactStore' object has no attribute 'save_mp3'`

- [ ] **Step 3: Implement `save_mp3`**

In `apps/api_gateway/app/services/artifacts.py`, add a method next to `save_wav`:

```python
    def save_mp3(self, data: bytes) -> tuple[str, str]:
        """Persist MP3 bytes; return (artifact_id, public_url)."""
        artifact_id = uuid.uuid4().hex
        filename = f"{artifact_id}.mp3"
        (self.base_dir / filename).write_bytes(data)
        return artifact_id, f"{self.url_prefix}/{filename}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_artifacts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/artifacts.py tests/unit/test_artifacts.py
git commit -m "feat(tts): add ArtifactStore.save_mp3 for non-WAV audio artifacts"
```

---

### Task 2: `EdgeTTSProvider` + registration + dependency

**Files:**
- Create: `apps/api_gateway/app/services/tts/providers/edge_tts_provider.py`
- Modify: `apps/api_gateway/app/services/tts/service.py`
- Modify: `pyproject.toml`
- Test: Modify `tests/unit/test_tts_engines.py`

**Interfaces:**
- Consumes: `ArtifactStore.save_mp3` (Task 1), `app.core.deps.module_available`, `app.core.errors.ProviderError`, `app.schemas.tts.TTSRequest`/`TTSResult`, `app.services.tts.base.TTSProvider`.
- Produces: `EdgeTTSProvider` class (`name = "edge_tts"`), registered in `TTSService.providers["edge_tts"]`. Nothing downstream depends on this beyond the registry/routes, which already exist.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tts_engines.py`:

```python
from app.services.tts.providers.edge_tts_provider import EdgeTTSProvider


def test_lists_edge_tts():
    engines = {e["engine"] for e in tts_service.list_engines()}
    assert "edge_tts" in engines


def test_edge_tts_voices_shape():
    voices = tts_service.get_provider("edge_tts").list_voices()
    assert voices == [
        {"label": "Hoài My (nữ)", "voice": "vi-VN-HoaiMyNeural"},
        {"label": "Nam Minh (nam)", "voice": "vi-VN-NamMinhNeural"},
    ]


def test_edge_tts_rate_str():
    assert EdgeTTSProvider._rate_str(None) == "+0%"
    assert EdgeTTSProvider._rate_str(1.0) == "+0%"
    assert EdgeTTSProvider._rate_str(1.2) == "+20%"
    assert EdgeTTSProvider._rate_str(0.8) == "-20%"


def test_edge_tts_estimate_duration():
    from app.services.tts.providers.edge_tts_provider import _estimate_duration_seconds

    # 48000 bits/s CBR -> 6000 bytes/s
    assert _estimate_duration_seconds(b"x" * 6000) == pytest.approx(1.0)
    assert _estimate_duration_seconds(b"") == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_tts_engines.py -v -k edge_tts`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.tts.providers.edge_tts_provider'`

- [ ] **Step 3: Implement the provider**

Create `apps/api_gateway/app/services/tts/providers/edge_tts_provider.py`:

```python
"""edge-tts — free cloud TTS via Microsoft Edge's Read Aloud service.

No API key, no local model; needs outbound network access. Unofficial
(reverse-engineered) API — test-UI/batch synthesis only, not the live
conversation pipeline (see docs/superpowers/specs/2026-07-11-edge-tts-provider-design.md
for why: the live path needs real WAV/PCM, this engine's native output is MP3).
"""

from app.core.deps import module_available
from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest, TTSResult
from app.services.artifacts import artifact_store
from app.services.tts.base import TTSProvider

_SAMPLE_RATE = 24000
_BITRATE_BPS = 48000  # fixed edge-tts output format: audio-24khz-48kbitrate-mono-mp3


def _estimate_duration_seconds(mp3_bytes: bytes) -> float:
    """Approximate duration from the known constant bitrate (no decode step)."""
    if not mp3_bytes:
        return 0.0
    return len(mp3_bytes) * 8 / _BITRATE_BPS


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

    @staticmethod
    def _rate_str(speed: float | None) -> str:
        if not speed:
            return "+0%"
        return f"{round((speed - 1) * 100):+d}%"

    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        try:
            import edge_tts
        except ImportError as exc:
            raise ProviderError(f"{self.name} synthesis failed: edge-tts not installed") from exc

        voice = payload.voice or self.DEFAULT_VOICE
        rate = self._rate_str(payload.speed)
        communicate = edge_tts.Communicate(payload.text, voice=voice, rate=rate)

        chunks = bytearray()
        try:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.extend(chunk["data"])
        except Exception as exc:  # noqa: BLE001 - surface as a clean provider error
            raise ProviderError(f"{self.name} synthesis failed: {exc}") from exc

        if not chunks:
            raise ProviderError(f"{self.name} synthesis failed: no audio received")

        mp3_bytes = bytes(chunks)
        _, audio_url = artifact_store.save_mp3(mp3_bytes)

        return TTSResult(
            engine=self.name,
            sample_rate=_SAMPLE_RATE,
            audio_url=audio_url,
            duration_seconds=round(_estimate_duration_seconds(mp3_bytes), 3),
            text=payload.text,
        )
```

- [ ] **Step 4: Register the provider**

In `apps/api_gateway/app/services/tts/service.py`, add the import and registry entry:

```python
from app.services.tts.providers.edge_tts_provider import EdgeTTSProvider
```

and in `TTSService.__init__`:

```python
        self.providers: dict[str, TTSProvider] = {
            "omnivoice": OmniVoiceProvider(),
            "vieneu": VieNeuProvider(),
            "edge_tts": EdgeTTSProvider(),
        }
```

- [ ] **Step 5: Add the optional dependency**

In `pyproject.toml`, after the `tiktok` group:

```toml
# edge-tts — free cloud TTS via Microsoft Edge's Read Aloud service. No API key,
# no local model, but needs outbound network access; unofficial (reverse-engineered)
# API, test-UI/batch use only (see docs/superpowers/specs/2026-07-11-edge-tts-provider-design.md).
edge-tts = [
  "edge-tts>=7.2.8",
]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_tts_engines.py -v -k edge_tts`
Expected: PASS (all 4 new tests)

- [ ] **Step 7: Run the full unit suite to check for regressions**

Run: `pytest tests/unit -q`
Expected: PASS (previous test count + 5: 4 in `test_tts_engines.py`, 1 in `test_artifacts.py`)

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/services/tts/providers/edge_tts_provider.py \
        apps/api_gateway/app/services/tts/service.py \
        pyproject.toml \
        tests/unit/test_tts_engines.py
git commit -m "feat(tts): add edge-tts provider (free cloud TTS, test-UI/batch only)"
```

---

### Task 3: Manual verification with the real package installed

**Files:** none (verification only — no code changes).

**Interfaces:** none.

- [ ] **Step 1: Install the new optional dependency into the dev venv**

Run: `pip install -e ".[edge-tts]"`
Expected: installs `edge-tts` (and its aiohttp/certifi/tabulate/typing-extensions deps) with no errors.

- [ ] **Step 2: Confirm the engine reports available**

Run:
```bash
python3 -c "
from app.services.tts.service import tts_service
print([e for e in tts_service.list_engines() if e['engine'] == 'edge_tts'])
"
```
Expected: `[{'engine': 'edge_tts', 'available': True, 'detail': 'Microsoft Edge TTS (cloud, no API key, network required)', 'install_hint': 'pip install edge-tts', 'default': False}]`

- [ ] **Step 3: Start the API server and hit the endpoint end-to-end**

Run: `uvicorn app.main:app --app-dir apps/api_gateway --port 8000 &` then:
```bash
curl -s -X POST http://localhost:8000/v1/tts/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Xin chào, đây là edge tts.", "engine": "edge_tts"}' | python3 -m json.tool
```
Expected: `"success": true`, `data.engine == "edge_tts"`, `data.audio_url` ending in `.mp3`, `data.duration_seconds > 0`.

- [ ] **Step 4: Fetch and sanity-check the artifact**

Run: `curl -s -o /tmp/edge_tts_test.mp3 http://localhost:8000<audio_url from step 3> && file /tmp/edge_tts_test.mp3`
Expected: `/tmp/edge_tts_test.mp3: Audio file with ID3 version...` (or `MPEG ADTS, layer III` — a real MP3, not empty/corrupt).

- [ ] **Step 5: Stop the server and record the result**

Run: `kill %1` (or Ctrl-C the foreground `uvicorn`).
No commit — this task only confirms the feature works against the real network service; Task 2's commit already captured the code.

---

## Self-Review Notes

- Spec coverage: output format/no-transcode (Task 2 Step 3), duration estimate (`_estimate_duration_seconds`, tested), voice list (tested), speed→rate mapping (`_rate_str`, tested), artifact storage (Task 1), registration/no-default-warmup (Task 2 Step 4, `default: False` verified in Task 3 Step 2), dependency group (Task 2 Step 5), tests match existing per-engine convention (Task 2 Step 1) — all covered.
- No placeholders; every step has real code and exact commands.
- Type/name consistency checked: `EdgeTTSProvider.name`, `_rate_str`, `_estimate_duration_seconds`, `save_mp3` are spelled identically everywhere they're used across Task 1/2/3.
