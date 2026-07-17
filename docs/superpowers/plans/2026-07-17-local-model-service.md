# Local Model Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run STT/TTS engines as OpenAI-compatible containers that the gateway consumes through a Model Registry `base_url`, instead of loading models in-process.

**Architecture:** A new `apps/model_service/` FastAPI app imports the gateway's existing stateless providers and exposes `/v1/audio/transcriptions` and `/v1/audio/speech`. One image; `SERVICE_KIND` + `SERVICE_ENGINE` env pick the engine. Config reaches providers through a new env layer in `resolve.py` (precedence: registry row > env > defaults) — in the container the registry cache is cold so env wins automatically, and in the gateway a registry row exists so behavior is unchanged. The gateway gains two protocol-named engines, `openai_stt` and `openai_tts`, that resolve `base_url`/`api_key` per call.

**Tech Stack:** Python 3.11, FastAPI, httpx, pydantic-settings, pytest, Docker.

**Spec:** `docs/superpowers/specs/2026-07-17-local-model-service-design.md`

## Global Constraints

- Python 3.11 in the container image (matches `infra/docker/Dockerfile.api`).
- The gateway's existing behavior must not change. Every `resolve.py` change keeps registry rows winning over env.
- No provider (`whisper_provider.py`, `vieneu_provider.py`, …) gets a constructor or a signature change. They stay stateless.
- Container never reads the gateway's registry DB. It sets `DATABASE_URL=sqlite+aiosqlite:////tmp/model_service.db` so the `system_config_store` reads in `whisper_provider.py:118` / `whisper_mlx_provider.py:58` / `vieneu_provider.py:71` land on a throwaway DB and return inert defaults.
- Container TTS supports `RenderingTTSProvider` subclasses only (WAV producers). `edge_tts` is a cloud service already and is excluded.
- v1 kinds: `stt`, `tts`. No LLM. No OmniVoice.
- Tests must not load a real model. Inject fakes.

## Deviations from the spec (decided while reading the code — flagged for review)

1. **The spec says move the three `system_config_store` reads into `resolve.py`.** Dropped. Those reads only fetch `stt_glossary_path` and `default_tts_engine_voice`, both inert at defaults, and moving them would break the existing SystemConfig admin UI wiring for a gateway that is working today. Pointing `DATABASE_URL` at a `/tmp` file gets the same container outcome with zero gateway risk. Cost: glossary is not configurable in the container in v1.
2. **The spec says ignore the request's `model` field and 400 on mismatch.** Wrong — `STTProvider.transcribe_bytes(audio, language, model)` already takes a per-call model (`stt/base.py:51-53`), and the gateway's `RemoteWhisperProvider` sends the registry's `model_id` as `model` (`remote_whisper_provider.py:36`), which never equals the engine name. A 400-on-mismatch rule would break the gateway on every call. The container forwards `model` to the provider instead, which also makes per-request Whisper model-size selection work.
3. **`response_format` is ignored in v1.** The container always returns WAV with `Content-Type: audio/wav`, since only `RenderingTTSProvider` engines are supported.

## File Structure

| File | Responsibility |
|---|---|
| `apps/api_gateway/app/services/model_registry/resolve.py` | + env layer under the registry (modify) |
| `apps/api_gateway/app/services/tts/base.py` | + public `render_wav()` seam (modify) |
| `apps/api_gateway/app/services/stt/providers/openai_stt_provider.py` | gateway → remote STT service (create) |
| `apps/api_gateway/app/services/tts/providers/openai_tts_provider.py` | gateway → remote TTS service (create) |
| `apps/api_gateway/app/services/stt/service.py` | register `openai_stt`, fix `list_engines` else-branch (modify) |
| `apps/api_gateway/app/services/tts/service.py` | register `openai_tts` (modify) |
| `apps/api_gateway/app/schemas/stt.py` | add `openai_stt` to the engine pattern (modify) |
| `apps/api_gateway/app/api/routes/model_registry.py` | persist `base_url` for tts; test-branch for new engines (modify) |
| `apps/model_service/app/config.py` | env → `ServiceConfig`, validated (create) |
| `apps/model_service/app/auth.py` | Bearer dependency (create) |
| `apps/model_service/app/routes_stt.py` | `/v1/audio/transcriptions` (create) |
| `apps/model_service/app/routes_tts.py` | `/v1/audio/speech` (create) |
| `apps/model_service/app/main.py` | wiring, startup validation, error envelope (create) |
| `infra/docker/Dockerfile.model_service` | image (create) |
| `infra/compose/docker-compose.yml` | `model-service` behind a profile (modify) |

---

### Task 1: Env layer in `resolve.py`

**Files:**
- Modify: `apps/api_gateway/app/services/model_registry/resolve.py:41-63`
- Test: `tests/unit/test_resolve_env_layer.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `resolve_stt_engine_config(engine: str) -> dict` and `resolve_stt_local_device(engine: str) -> dict` gain env fallback. Env var names are `STT_{ENGINE}_{KEY}` uppercased, e.g. `STT_WHISPER_LOCAL_DEFAULT_MODEL`, `STT_WHISPER_LOCAL_DEVICE`, `STT_WHISPER_LOCAL_COMPUTE_TYPE`, `STT_VOSK_MODEL_PATH`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_resolve_env_layer.py
import pytest

from app.services.model_registry import resolve
from app.services.model_registry.store import model_registry_store


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch):
    # Container conditions: nothing has awaited the store, so the cache is cold
    # and find_sync returns None for every lookup.
    monkeypatch.setattr(model_registry_store, "_by_id", None, raising=False)


def test_env_overrides_default_when_no_registry_row(monkeypatch):
    monkeypatch.setenv("STT_WHISPER_LOCAL_DEFAULT_MODEL", "phowhisper-large")
    assert resolve.resolve_stt_engine_config("whisper_local")["default_model"] == "phowhisper-large"


def test_env_is_coerced_to_the_default_s_type(monkeypatch):
    monkeypatch.setenv("STT_WHISPER_LOCAL_VAD_FILTER", "false")
    monkeypatch.setenv("STT_WHISPER_LOCAL_BEAM_SIZE", "5")
    cfg = resolve.resolve_stt_engine_config("whisper_local")
    assert cfg["vad_filter"] is False
    assert cfg["beam_size"] == 5


def test_default_survives_when_env_absent():
    assert resolve.resolve_stt_engine_config("whisper_local")["beam_size"] == 1


def test_env_overrides_device_and_compute_type(monkeypatch):
    monkeypatch.setenv("STT_WHISPER_LOCAL_DEVICE", "cuda")
    monkeypatch.setenv("STT_WHISPER_LOCAL_COMPUTE_TYPE", "float16")
    assert resolve.resolve_stt_local_device("whisper_local") == {
        "device": "cuda",
        "compute_type": "float16",
    }


def test_device_resolver_returns_only_its_two_keys(monkeypatch):
    monkeypatch.setenv("STT_WHISPER_LOCAL_DEVICE", "cuda")
    assert set(resolve.resolve_stt_local_device("whisper_local")) == {"device", "compute_type"}


def test_registry_row_beats_env(monkeypatch):
    # Gateway conditions: a warm cache with a sentinel row must win, so existing
    # deployments are unaffected by env vars that happen to be set.
    monkeypatch.setattr(
        model_registry_store,
        "_by_id",
        {
            "x": {
                "id": "x", "kind": "stt", "engine": "whisper_local", "model_id": "",
                "enabled": True, "stage": "stable", "label": "", "api_key": "",
                "base_url": "", "config": {"default_model": "from-registry"},
            }
        },
        raising=False,
    )
    monkeypatch.setenv("STT_WHISPER_LOCAL_DEFAULT_MODEL", "from-env")
    assert resolve.resolve_stt_engine_config("whisper_local")["default_model"] == "from-registry"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_resolve_env_layer.py -v`
Expected: FAIL — `test_env_overrides_default_when_no_registry_row` asserts `phowhisper-large` but gets the default `phowhisper-medium`.

- [ ] **Step 3: Write the implementation**

Add to `resolve.py`, after the imports:

```python
import os


def _coerce(raw: str, default):
    """Coerce an env string to the type of the default it overrides. bool is
    checked before int because bool is a subclass of int."""
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def _env_overrides(prefix: str, defaults: dict) -> dict:
    """Read {PREFIX}_{KEY} for each key in `defaults`. Only keys that exist in
    `defaults` are readable, so a typo'd env var is ignored rather than
    injecting an unknown key into provider config."""
    out = {}
    for key, default in defaults.items():
        raw = os.environ.get(f"{prefix}_{key}".upper())
        if raw is not None:
            out[key] = _coerce(raw, default)
    return out
```

Replace `resolve_stt_engine_config` (line 41-48) and `resolve_stt_local_device` (line 51-63):

```python
def resolve_stt_engine_config(engine: str) -> dict:
    """Engine-level config for a local STT engine (default model, whisper
    decode tuning), merged over the per-engine defaults above. Looked up by
    the reserved model_id="" sentinel -- see resolve_stt_local_device's
    docstring for why the per-model-size governance rows must not match.

    Precedence: registry row > env > defaults. The env layer exists for
    apps/model_service, which runs a provider with no registry DB: there the
    cache is cold, find_sync returns None, and env wins. In the gateway a
    sentinel row exists and still wins, so this is a no-op there."""
    defaults = STT_ENGINE_CONFIG_DEFAULTS.get(engine, {})
    entry = model_registry_store.find_sync("stt", engine, "")
    config = (entry or {}).get("config") or {}
    return {**defaults, **_env_overrides(f"STT_{engine}", defaults), **config}


def resolve_stt_local_device(engine: str) -> dict:
    """{'device': str, 'compute_type': str} for a local STT engine (only
    whisper_local uses compute_type; qwen3_asr's caller just ignores it).
    Looked up by the reserved model_id="" sentinel, which is distinct from
    the per-model-size governance rows seed_known_models() creates under the
    same (kind, engine) pair -- using find_enabled_sync here instead would
    silently match one of those governance rows (empty config) instead.

    Precedence: registry row > env > defaults (see resolve_stt_engine_config)."""
    defaults = {"device": "", "compute_type": "int8"}
    entry = model_registry_store.find_sync("stt", engine, "")
    config = (entry or {}).get("config") or {}
    merged = {**defaults, **_env_overrides(f"STT_{engine}", defaults), **config}
    # The sentinel row's config also carries engine-level keys (default_model,
    # beam_size, ...); return only this resolver's two.
    return {"device": merged["device"], "compute_type": merged["compute_type"]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_resolve_env_layer.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run the STT suite to prove the gateway is unchanged**

Run: `pytest tests/unit -k "stt or resolve or registry" -q`
Expected: all pass, no new failures.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/resolve.py tests/unit/test_resolve_env_layer.py
git commit -m "feat(model-registry): env fallback layer under registry config

Precedence registry > env > defaults. Lets a provider run with no registry
DB (apps/model_service); no-op for the gateway, where a sentinel row wins."
```

---

### Task 2: Public `render_wav()` seam on `RenderingTTSProvider`

**Files:**
- Modify: `apps/api_gateway/app/services/tts/base.py:54-67`
- Test: `tests/unit/test_tts_render_seam.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RenderingTTSProvider.render_wav(payload: TTSRequest) -> bytes` — real synthesis with no artifact side effect, raising `ProviderError` on failure. `synthesize()` now delegates to it. The container and the gateway's `openai_tts` both use it.

Why: `synthesize()` writes an artifact file and returns a URL, but the container needs raw bytes on the HTTP response. Reaching into `_render_wav` from outside would couple to a private name.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tts_render_seam.py
import pytest

from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider


class _FakeProvider(RenderingTTSProvider):
    name = "fake"

    def __init__(self, wav: bytes = b"RIFF....WAVE", exc: Exception | None = None):
        self._wav = wav
        self._exc = exc

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        if self._exc:
            raise self._exc
        return self._wav


@pytest.mark.asyncio
async def test_render_wav_returns_bytes_without_saving_an_artifact():
    provider = _FakeProvider(wav=b"RIFFbytes")
    assert await provider.render_wav(TTSRequest(text="xin chào", engine="fake")) == b"RIFFbytes"


@pytest.mark.asyncio
async def test_render_wav_wraps_failures_as_provider_error():
    provider = _FakeProvider(exc=RuntimeError("cuda oom"))
    with pytest.raises(ProviderError, match="fake synthesis failed: cuda oom"):
        await provider.render_wav(TTSRequest(text="xin chào", engine="fake"))


@pytest.mark.asyncio
async def test_synthesize_still_wraps_errors_as_provider_error():
    provider = _FakeProvider(exc=RuntimeError("cuda oom"))
    with pytest.raises(ProviderError, match="fake synthesis failed: cuda oom"):
        await provider.synthesize(TTSRequest(text="xin chào", engine="fake"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tts_render_seam.py -v`
Expected: FAIL — `AttributeError: '_FakeProvider' object has no attribute 'render_wav'`.

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/tts/base.py`, replace `RenderingTTSProvider.synthesize` (lines 54-67) with:

```python
    async def render_wav(self, payload: TTSRequest) -> bytes:
        """Public seam: real synthesis -> WAV bytes, no artifact side effect.

        apps/model_service returns these bytes straight on the HTTP response;
        synthesize() below is the gateway's artifact-saving path on top."""
        try:
            return await self._render_wav(payload)
        except Exception as exc:  # noqa: BLE001 - surface as a clean provider error
            raise ProviderError(f"{self.name} synthesis failed: {exc}") from exc

    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        wav = await self.render_wav(payload)

        _, audio_url = artifact_store.save_wav(wav)
        return TTSResult(
            engine=self.name,
            sample_rate=self.sample_rate,
            audio_url=audio_url,
            duration_seconds=round(wav_duration_seconds(wav), 3),
            text=payload.text,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_tts_render_seam.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the TTS suite to prove nothing regressed**

Run: `pytest tests/unit -k tts -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/tts/base.py tests/unit/test_tts_render_seam.py
git commit -m "feat(tts): public render_wav() seam on RenderingTTSProvider

synthesize() delegates to it; callers that need raw WAV bytes (the model
service, the openai_tts client) no longer touch the private _render_wav."
```

---

### Task 3: Model service config + auth

**Files:**
- Create: `apps/model_service/__init__.py` (empty), `apps/model_service/app/__init__.py` (empty)
- Create: `apps/model_service/app/config.py`, `apps/model_service/app/auth.py`
- Test: `tests/unit/model_service/__init__.py` (empty), `tests/unit/model_service/test_config.py`, `tests/unit/model_service/test_auth.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ServiceConfig` — frozen dataclass with fields `kind: str`, `engine: str`, `api_token: str`, `port: int`.
  - `load_config(env: Mapping[str, str] | None = None) -> ServiceConfig` — raises `ConfigError`.
  - `ConfigError(RuntimeError)`.
  - `make_auth_dependency(expected_token: str) -> Callable` — a FastAPI dependency raising `HTTPException(401)`.

- [ ] **Step 1: Write the failing config test**

```python
# tests/unit/model_service/test_config.py
import pytest

from model_service.app.config import ConfigError, load_config

_VALID = {"SERVICE_KIND": "stt", "SERVICE_ENGINE": "whisper_local", "SERVICE_API_TOKEN": "t0ken"}


def test_loads_a_valid_env():
    cfg = load_config(_VALID)
    assert (cfg.kind, cfg.engine, cfg.api_token, cfg.port) == ("stt", "whisper_local", "t0ken", 8100)


def test_kind_is_normalized():
    assert load_config({**_VALID, "SERVICE_KIND": " STT "}).kind == "stt"


@pytest.mark.parametrize("kind", ["", "llm", "nonsense"])
def test_rejects_bad_kind(kind):
    with pytest.raises(ConfigError, match="SERVICE_KIND"):
        load_config({**_VALID, "SERVICE_KIND": kind})


def test_rejects_missing_engine():
    with pytest.raises(ConfigError, match="SERVICE_ENGINE"):
        load_config({**_VALID, "SERVICE_ENGINE": "  "})


def test_rejects_missing_token():
    # The token is mandatory: an unauthenticated STT container that gets its
    # port published hands out free GPU to anyone who finds it.
    with pytest.raises(ConfigError, match="SERVICE_API_TOKEN"):
        load_config({**_VALID, "SERVICE_API_TOKEN": ""})


def test_port_is_overridable():
    assert load_config({**_VALID, "SERVICE_PORT": "9000"}).port == 9000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/model_service/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'model_service'`.

- [ ] **Step 3: Write the config implementation**

```python
# apps/model_service/app/config.py
"""Env -> validated ServiceConfig for the standalone model service.

Everything is validated at startup rather than on first request: a container
with a typo'd SERVICE_ENGINE should fail its healthcheck immediately, not 30
minutes later when the first audio arrives.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

VALID_KINDS = ("stt", "tts")


class ConfigError(RuntimeError):
    """Env is missing or invalid; the process must not start."""


@dataclass(frozen=True)
class ServiceConfig:
    kind: str
    engine: str
    api_token: str
    port: int = 8100


def load_config(env: Mapping[str, str] | None = None) -> ServiceConfig:
    env = os.environ if env is None else env

    kind = env.get("SERVICE_KIND", "").strip().lower()
    if kind not in VALID_KINDS:
        raise ConfigError(f"SERVICE_KIND must be one of {VALID_KINDS!r}, got {kind!r}")

    engine = env.get("SERVICE_ENGINE", "").strip()
    if not engine:
        raise ConfigError("SERVICE_ENGINE is required (e.g. whisper_local, vieneu)")

    api_token = env.get("SERVICE_API_TOKEN", "").strip()
    if not api_token:
        raise ConfigError("SERVICE_API_TOKEN is required; the service refuses to run open")

    try:
        port = int(env.get("SERVICE_PORT", "8100"))
    except ValueError as exc:
        raise ConfigError(f"SERVICE_PORT must be an integer: {exc}") from exc

    return ServiceConfig(kind=kind, engine=engine, api_token=api_token, port=port)
```

- [ ] **Step 4: Make the package importable**

Create empty `apps/model_service/__init__.py` and `apps/model_service/app/__init__.py`.

Add `apps/model_service` to the test path so `import model_service` resolves. In `pyproject.toml`, find the `[tool.pytest.ini_options]` block and add `apps` to `pythonpath` (create the key if absent, keeping any existing entries):

```toml
[tool.pytest.ini_options]
pythonpath = ["apps/api_gateway", "apps"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/model_service/test_config.py -v`
Expected: 8 passed.

- [ ] **Step 6: Write the failing auth test**

```python
# tests/unit/model_service/test_auth.py
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from model_service.app.auth import make_auth_dependency


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/guarded", dependencies=[Depends(make_auth_dependency("s3cret"))])
    def guarded():
        return {"ok": True}

    return TestClient(app)


def test_accepts_the_right_token(client):
    r = client.get("/guarded", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_rejects_a_wrong_token(client):
    r = client.get("/guarded", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_rejects_a_missing_header(client):
    assert client.get("/guarded").status_code == 401


def test_rejects_a_non_bearer_scheme(client):
    r = client.get("/guarded", headers={"Authorization": "Basic s3cret"})
    assert r.status_code == 401
```

- [ ] **Step 7: Run test to verify it fails**

Run: `pytest tests/unit/model_service/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'model_service.app.auth'`.

- [ ] **Step 8: Write the auth implementation**

```python
# apps/model_service/app/auth.py
"""Bearer-token auth for the model service.

One static token from env, compared in constant time. There are no users and
no sessions here -- the only caller is the gateway, holding the token in its
Model Registry entry's api_key column.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import Header, HTTPException

_PREFIX = "Bearer "


def make_auth_dependency(expected_token: str) -> Callable:
    async def verify(authorization: str = Header(default="")) -> None:
        if not authorization.startswith(_PREFIX):
            raise HTTPException(status_code=401, detail="missing bearer token")
        supplied = authorization[len(_PREFIX):]
        # compare_digest, not ==, so a wrong token can't be recovered by timing.
        if not secrets.compare_digest(supplied, expected_token):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    return verify
```

- [ ] **Step 9: Run test to verify it passes**

Run: `pytest tests/unit/model_service/test_auth.py -v`
Expected: 4 passed.

- [ ] **Step 10: Commit**

```bash
git add apps/model_service tests/unit/model_service pyproject.toml
git commit -m "feat(model-service): env config + bearer auth

Validated at startup; the token is mandatory so the container never runs open."
```

---

### Task 4: STT route + app wiring

**Files:**
- Create: `apps/model_service/app/routes_stt.py`, `apps/model_service/app/main.py`
- Test: `tests/unit/model_service/test_routes_stt.py`

**Interfaces:**
- Consumes: `ServiceConfig`, `load_config`, `ConfigError` from `model_service.app.config`; `make_auth_dependency` from `model_service.app.auth`; `STTProvider.transcribe_bytes(audio_bytes, language=None, model=None) -> STTResult` from `app.services.stt.base`.
- Produces:
  - `build_stt_router(config: ServiceConfig, provider) -> APIRouter` — mounts `POST /v1/audio/transcriptions` and `GET /v1/models`.
  - `create_app(config: ServiceConfig | None = None, provider=None) -> FastAPI` in `main.py`.
  - `app` module-level instance in `main.py` for uvicorn.

Note: the request's `model` form field is **forwarded to the provider**, not validated against the engine name. The gateway sends the registry entry's `model_id` there (`remote_whisper_provider.py:36`), which is a model name like `phowhisper-medium`, never the engine name.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/model_service/test_routes_stt.py
import pytest
from fastapi.testclient import TestClient

from app.core.errors import EngineNotFoundError, ProviderError
from app.schemas.stt import STTResult
from model_service.app.config import ServiceConfig
from model_service.app.main import create_app

_CFG = ServiceConfig(kind="stt", engine="whisper_local", api_token="t0ken")
_AUTH = {"Authorization": "Bearer t0ken"}


class _FakeSTT:
    name = "whisper_local"

    def __init__(self, exc: Exception | None = None):
        self.calls: list[tuple] = []
        self._exc = exc

    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        self.calls.append((audio_bytes, language, model))
        if self._exc:
            raise self._exc
        return STTResult(engine=self.name, text="xin chào", is_final=True, confidence=None)


def _client(provider):
    return TestClient(create_app(config=_CFG, provider=provider))


def test_transcribes_and_returns_openai_shape():
    client = _client(_FakeSTT())
    r = client.post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"RIFFDATA", "audio/wav")}
    )
    assert r.status_code == 200
    assert r.json() == {"text": "xin chào"}


def test_forwards_language_and_model_to_the_provider():
    # The gateway sends the registry entry's model_id here; it must reach the
    # provider, which takes a per-call model.
    provider = _FakeSTT()
    _client(provider).post(
        "/v1/audio/transcriptions",
        headers=_AUTH,
        files={"file": ("a.wav", b"RIFFDATA", "audio/wav")},
        data={"language": "vi", "model": "phowhisper-medium"},
    )
    assert provider.calls == [(b"RIFFDATA", "vi", "phowhisper-medium")]


def test_blank_language_and_model_become_none():
    provider = _FakeSTT()
    _client(provider).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert provider.calls == [(b"D", None, None)]


def test_requires_auth():
    r = _client(_FakeSTT()).post(
        "/v1/audio/transcriptions", files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert r.status_code == 401


def test_provider_error_becomes_502_in_the_openai_envelope():
    r = _client(_FakeSTT(exc=ProviderError("engine died"))).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert r.status_code == 502
    assert r.json()["error"]["message"] == "engine died"
    assert r.json()["error"]["type"] == "provider_error"


def test_engine_not_found_becomes_400():
    r = _client(_FakeSTT(exc=EngineNotFoundError("no such model"))).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_empty_upload_is_rejected():
    r = _client(_FakeSTT()).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"", "audio/wav")}
    )
    assert r.status_code == 400


def test_models_lists_the_running_engine():
    r = _client(_FakeSTT()).get("/v1/models", headers=_AUTH)
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["whisper_local"]


def test_health_needs_no_auth():
    r = _client(_FakeSTT()).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "kind": "stt", "engine": "whisper_local"}


def test_stt_container_does_not_expose_speech():
    # Kind-based mounting: this container has no TTS provider loaded at all.
    assert _client(_FakeSTT()).post("/v1/audio/speech", headers=_AUTH, json={"input": "hi"}).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/model_service/test_routes_stt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'model_service.app.main'`.

- [ ] **Step 3: Write the STT router**

```python
# apps/model_service/app/routes_stt.py
"""OpenAI-compatible transcription endpoint over a gateway STT provider."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.services.stt.base import STTProvider
from model_service.app.auth import make_auth_dependency
from model_service.app.config import ServiceConfig


def build_stt_router(config: ServiceConfig, provider: STTProvider) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(make_auth_dependency(config.api_token))])

    @router.get("/models")
    async def list_models() -> dict:
        return {"object": "list", "data": [{"id": config.engine, "object": "model", "owned_by": "local"}]}

    @router.post("/audio/transcriptions")
    async def create_transcription(
        file: UploadFile = File(...),
        model: str = Form(default=""),
        language: str = Form(default=""),
        response_format: str = Form(default="json"),
    ) -> dict:
        audio = await file.read()
        if not audio:
            raise HTTPException(status_code=400, detail="uploaded file is empty")

        # `model` is a model name (the gateway sends its registry entry's
        # model_id), not the engine name -- forward it; the provider takes a
        # per-call model and falls back to its configured default on None.
        result = await provider.transcribe_bytes(audio, language or None, model or None)
        return {"text": result.text}

    return router
```

- [ ] **Step 4: Write `main.py`**

```python
# apps/model_service/app/main.py
"""Standalone STT/TTS service: one engine per container, chosen by env.

Wraps the gateway's existing providers in an OpenAI-compatible HTTP surface so
the gateway can consume them as a service (a Model Registry base_url) instead
of loading the model in-process.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.core.errors import EngineNotFoundError, ProviderError
from model_service.app.config import ConfigError, ServiceConfig, load_config

logger = logging.getLogger(__name__)


def _resolve_provider(config: ServiceConfig):
    """Fetch the configured provider, failing fast on an unknown engine."""
    if config.kind == "stt":
        from app.services.stt.service import stt_service

        return stt_service.get_provider(config.engine)

    from app.services.tts.base import RenderingTTSProvider
    from app.services.tts.service import tts_service

    provider = tts_service.get_provider(config.engine)
    if not isinstance(provider, RenderingTTSProvider):
        # Only WAV-rendering engines can serve raw bytes on the response. The
        # odd one out is edge_tts, which is a cloud service anyway.
        raise ConfigError(
            f"TTS engine '{config.engine}' is not a RenderingTTSProvider and cannot be served"
        )
    return provider


def create_app(config: ServiceConfig | None = None, provider=None) -> FastAPI:
    config = config or load_config()
    if provider is None:
        provider = _resolve_provider(config)

    app = FastAPI(title=f"model-service ({config.kind}:{config.engine})")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "kind": config.kind, "engine": config.engine}

    if config.kind == "stt":
        from model_service.app.routes_stt import build_stt_router

        app.include_router(build_stt_router(config, provider))
    else:
        from model_service.app.routes_tts import build_tts_router

        app.include_router(build_tts_router(config, provider))

    @app.exception_handler(HTTPException)
    async def _http_error(_request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": exc.detail, "type": "invalid_request_error"}},
        )

    @app.exception_handler(EngineNotFoundError)
    async def _engine_error(_request, exc: EngineNotFoundError):
        return JSONResponse(
            status_code=400, content={"error": {"message": str(exc), "type": "invalid_request_error"}}
        )

    @app.exception_handler(ProviderError)
    async def _provider_error(_request, exc: ProviderError):
        # The engine itself failed (OOM, model missing): the request was fine,
        # so this is 502 rather than 400 -- and the caller owns the retry.
        logger.exception("provider failed")
        return JSONResponse(
            status_code=502, content={"error": {"message": str(exc), "type": "provider_error"}}
        )

    return app
```

Note: there is deliberately **no module-level `app = create_app()`**. That would call `load_config()` at import time, so every test that imports this module would die with `ConfigError` before it could pass a test config. Uvicorn gets the factory instead (`--factory`, see Task 9's `CMD`).

Note: `_engine_error` catches `EngineNotFoundError` raised *inside a request* (an unknown per-call model). An unknown `SERVICE_ENGINE` is caught earlier, by `_resolve_provider` during `create_app`, and kills the process at startup.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/model_service/test_routes_stt.py -v`
Expected: 10 passed. `test_stt_container_does_not_expose_speech` passes because `create_app` mounts only the STT router.

- [ ] **Step 6: Write the startup-validation test**

```python
# append to tests/unit/model_service/test_config.py
import pytest

from model_service.app.config import ConfigError, ServiceConfig
from model_service.app.main import create_app


def test_unknown_stt_engine_fails_at_startup():
    from app.core.errors import EngineNotFoundError

    with pytest.raises(EngineNotFoundError):
        create_app(ServiceConfig(kind="stt", engine="not_an_engine", api_token="t"))


def test_non_rendering_tts_engine_is_refused():
    # edge_tts is a plain TTSProvider: it cannot hand back WAV bytes.
    with pytest.raises(ConfigError, match="RenderingTTSProvider"):
        create_app(ServiceConfig(kind="tts", engine="edge_tts", api_token="t"))
```

- [ ] **Step 7: Run it**

Run: `pytest tests/unit/model_service/test_config.py -v`
Expected: 10 passed. (`test_non_rendering_tts_engine_is_refused` needs Task 5's `routes_tts.py` to exist only if the router gets built — it does not, because `_resolve_provider` raises first.)

- [ ] **Step 8: Commit**

```bash
git add apps/model_service tests/unit/model_service
git commit -m "feat(model-service): STT endpoint + app wiring

OpenAI-compatible /v1/audio/transcriptions over the existing STT providers.
Engine validity is checked at startup; errors use the OpenAI envelope."
```

---

### Task 5: TTS route

**Files:**
- Create: `apps/model_service/app/routes_tts.py`
- Test: `tests/unit/model_service/test_routes_tts.py`

**Interfaces:**
- Consumes: `ServiceConfig`, `make_auth_dependency`, and `RenderingTTSProvider.render_wav(payload: TTSRequest) -> bytes` from Task 2.
- Produces: `build_tts_router(config: ServiceConfig, provider) -> APIRouter` — mounts `POST /v1/audio/speech` and `GET /v1/models`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/model_service/test_routes_tts.py
import pytest
from fastapi.testclient import TestClient

from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest
from model_service.app.config import ServiceConfig
from model_service.app.main import create_app

_CFG = ServiceConfig(kind="tts", engine="vieneu", api_token="t0ken")
_AUTH = {"Authorization": "Bearer t0ken"}


class _FakeTTS:
    name = "vieneu"

    def __init__(self, exc: Exception | None = None):
        self.calls: list[TTSRequest] = []
        self._exc = exc

    async def render_wav(self, payload: TTSRequest) -> bytes:
        self.calls.append(payload)
        if self._exc:
            raise self._exc
        return b"RIFFWAVEDATA"


def _client(provider):
    return TestClient(create_app(config=_CFG, provider=provider))


def test_synthesizes_and_returns_wav_bytes():
    r = _client(_FakeTTS()).post("/v1/audio/speech", headers=_AUTH, json={"input": "xin chào"})
    assert r.status_code == 200
    assert r.content == b"RIFFWAVEDATA"
    assert r.headers["content-type"] == "audio/wav"


def test_maps_openai_fields_onto_the_tts_request():
    provider = _FakeTTS()
    _client(provider).post(
        "/v1/audio/speech",
        headers=_AUTH,
        json={"input": "xin chào", "voice": "vi-female-1", "speed": 1.25},
    )
    payload = provider.calls[0]
    assert (payload.text, payload.voice, payload.speed, payload.engine) == (
        "xin chào", "vi-female-1", 1.25, "vieneu",
    )


def test_requires_auth():
    assert _client(_FakeTTS()).post("/v1/audio/speech", json={"input": "hi"}).status_code == 401


def test_empty_input_is_rejected():
    r = _client(_FakeTTS()).post("/v1/audio/speech", headers=_AUTH, json={"input": ""})
    assert r.status_code == 422


def test_provider_error_becomes_502():
    r = _client(_FakeTTS(exc=ProviderError("vieneu synthesis failed: oom"))).post(
        "/v1/audio/speech", headers=_AUTH, json={"input": "xin chào"}
    )
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "provider_error"


def test_tts_container_does_not_expose_transcriptions():
    r = _client(_FakeTTS()).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert r.status_code == 404


def test_models_lists_the_running_engine():
    r = _client(_FakeTTS()).get("/v1/models", headers=_AUTH)
    assert [m["id"] for m in r.json()["data"]] == ["vieneu"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/model_service/test_routes_tts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'model_service.app.routes_tts'`.

- [ ] **Step 3: Write the implementation**

```python
# apps/model_service/app/routes_tts.py
"""OpenAI-compatible speech endpoint over a gateway TTS provider.

`response_format` is accepted but ignored: only RenderingTTSProvider engines
are servable and they all produce WAV.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider
from model_service.app.auth import make_auth_dependency
from model_service.app.config import ServiceConfig


class SpeechRequest(BaseModel):
    input: str = Field(..., min_length=1)
    model: str = ""
    voice: str | None = None
    speed: float | None = None
    language: str | None = None
    response_format: str = "wav"


def build_tts_router(config: ServiceConfig, provider: RenderingTTSProvider) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(make_auth_dependency(config.api_token))])

    @router.get("/models")
    async def list_models() -> dict:
        return {"object": "list", "data": [{"id": config.engine, "object": "model", "owned_by": "local"}]}

    @router.post("/audio/speech")
    async def create_speech(payload: SpeechRequest) -> Response:
        wav = await provider.render_wav(
            TTSRequest(
                text=payload.input,
                engine=config.engine,
                voice=payload.voice,
                speed=payload.speed,
                language=payload.language,
            )
        )
        return Response(content=wav, media_type="audio/wav")

    return router
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/model_service/test_routes_tts.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/model_service/app/routes_tts.py tests/unit/model_service/test_routes_tts.py
git commit -m "feat(model-service): TTS endpoint

OpenAI-compatible /v1/audio/speech returning WAV bytes via render_wav()."
```

---

### Task 6: Gateway `openai_stt` provider

**Files:**
- Create: `apps/api_gateway/app/services/stt/providers/openai_stt_provider.py`
- Modify: `apps/api_gateway/app/services/stt/service.py:20-50` and `:145-148`
- Modify: `apps/api_gateway/app/schemas/stt.py:5-8`
- Test: `tests/unit/test_openai_stt_provider.py`

**Interfaces:**
- Consumes: `model_registry_store.find(kind, engine, model_id)` and `find_enabled(kind, engine)` (both async, `store.py:99-103`); `STTResult`.
- Produces: `OpenAICompatSttProvider(name: str = "openai_stt", timeout_seconds: float = 60.0, entry: dict | None = None)` with `transcribe_bytes(audio_bytes, language=None, model=None) -> STTResult`. The `entry` argument is an override used only by the registry's test-before-add call, where no row exists yet.

Why per-call resolution rather than the constructor-cached `RemoteWhisperProvider`: it removes the need for a `reinit_remote_providers()` branch on every admin edit (`routes/model_registry.py:121-122`).

**Critical:** `stt/service.py:145-148` ends its if/elif with `base_url, model = remote[engine]`, a dict with only `whisper_service`/`eventlab` keys. Registering `openai_stt` without touching that branch makes `GET /v1/stt/engines` raise `KeyError`. And `schemas/stt.py:7` pins `STTRequest.engine` to a regex whitelist — an unlisted engine gets a 422.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_openai_stt_provider.py
import httpx
import pytest

from app.services.stt.providers.openai_stt_provider import OpenAICompatSttProvider

_ENTRY = {
    "id": "e1", "kind": "stt", "engine": "openai_stt", "model_id": "phowhisper-medium",
    "label": "local box", "enabled": True, "stage": "stable",
    "api_key": "t0ken", "base_url": "http://stt-service:8100/v1", "config": {},
}


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": " xin chào  "})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


@pytest.mark.asyncio
async def test_posts_to_the_entry_base_url_with_bearer(captured, monkeypatch):
    async def fake_find(kind, engine, model_id):
        return _ENTRY

    monkeypatch.setattr(
        "app.services.stt.providers.openai_stt_provider.model_registry_store.find", fake_find
    )
    provider = OpenAICompatSttProvider()
    result = await provider.transcribe_bytes(b"RIFFDATA", "vi", "phowhisper-medium")

    assert captured["url"] == "http://stt-service:8100/v1/audio/transcriptions"
    assert captured["auth"] == "Bearer t0ken"
    assert result.text == "xin chào"
    assert result.engine == "openai_stt"


@pytest.mark.asyncio
async def test_falls_back_to_the_enabled_entry_when_no_model_given(captured, monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return _ENTRY

    monkeypatch.setattr(
        "app.services.stt.providers.openai_stt_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    result = await OpenAICompatSttProvider().transcribe_bytes(b"RIFFDATA")
    assert result.text == "xin chào"


@pytest.mark.asyncio
async def test_explicit_entry_override_skips_the_registry(captured):
    # The registry's test-before-add call has no row to look up yet.
    provider = OpenAICompatSttProvider(entry=_ENTRY)
    await provider.transcribe_bytes(b"RIFFDATA")
    assert captured["auth"] == "Bearer t0ken"


@pytest.mark.asyncio
async def test_unconfigured_entry_raises_a_clear_error(monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return None

    monkeypatch.setattr(
        "app.services.stt.providers.openai_stt_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    with pytest.raises(RuntimeError, match="not configured"):
        await OpenAICompatSttProvider().transcribe_bytes(b"RIFFDATA")


@pytest.mark.asyncio
async def test_http_error_surfaces_the_status_and_body(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="invalid bearer token")

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await OpenAICompatSttProvider(entry=_ENTRY).transcribe_bytes(b"RIFFDATA")


@pytest.mark.asyncio
async def test_timeout_comes_from_the_entry_config(captured, monkeypatch):
    entry = {**_ENTRY, "config": {"timeout_seconds": 5.0}}
    provider = OpenAICompatSttProvider(entry=entry)
    await provider.transcribe_bytes(b"RIFFDATA")  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_openai_stt_provider.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.stt.providers.openai_stt_provider'`.

- [ ] **Step 3: Write the provider**

```python
# apps/api_gateway/app/services/stt/providers/openai_stt_provider.py
"""STT against any OpenAI-compatible /audio/transcriptions endpoint.

The name describes the protocol, not the backend: the same Model Registry
entry can point at apps/model_service, a faster-whisper-server, or any other
compatible host.

Unlike RemoteWhisperProvider (which caches base_url/api_key at construction and
needs stt_service.reinit_remote_providers() after every admin edit), this
resolves its entry per call, so an edited entry takes effect immediately.
"""

from __future__ import annotations

import httpx

from app.schemas.stt import STTResult
from app.services.model_registry.store import model_registry_store
from app.services.stt.base import STTProvider

_DEFAULT_TIMEOUT = 60.0


class OpenAICompatSttProvider(STTProvider):
    name = "openai_stt"

    def __init__(
        self,
        name: str = "openai_stt",
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        entry: dict | None = None,
    ) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds
        # Only the registry's test-before-add call passes an entry: at that
        # point the row being validated does not exist yet.
        self._entry_override = entry

    async def _resolve_entry(self, model: str | None) -> dict | None:
        if self._entry_override is not None:
            return self._entry_override
        if model:
            return await model_registry_store.find(kind="stt", engine=self.name, model_id=model)
        return await model_registry_store.find_enabled(kind="stt", engine=self.name)

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        entry = await self._resolve_entry(model)
        base_url = (entry or {}).get("base_url", "").strip()
        if not base_url:
            raise RuntimeError(
                f"{self.name} is not configured. Add a Model Registry entry with the "
                f"service's base URL (e.g. http://stt-service:8100/v1)."
            )

        api_key = (entry or {}).get("api_key", "").strip()
        timeout = (entry.get("config") or {}).get("timeout_seconds") or self.timeout_seconds

        endpoint = f"{base_url.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        data = {"model": model or entry.get("model_id", ""), "response_format": "json"}
        if language:
            data["language"] = language
        files = {"file": ("audio.wav", audio_bytes, "audio/wav")}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(endpoint, headers=headers, data=data, files=files)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"{self.name} returned HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{self.name} request failed: {exc}") from exc

        return STTResult(
            engine=self.name, text=str(payload.get("text", "")).strip(), is_final=True, confidence=None
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_openai_stt_provider.py -v`
Expected: 6 passed.

- [ ] **Step 5: Register the engine and fix the two chokepoints**

In `apps/api_gateway/app/services/stt/service.py`, add the import:

```python
from app.services.stt.providers.openai_stt_provider import OpenAICompatSttProvider
```

Add to the `self.providers` dict in `__init__` (after `"whisper_or"`):

```python
            "openai_stt": OpenAICompatSttProvider(
                timeout_seconds=remote_stt.remote_stt_timeout_seconds
            ),
```

Then fix the `list_engines` tail. Replace lines 139-148 (`elif engine in ("qwen3_asr_or", "whisper_or"): ... else: base_url, model = remote[engine]`) with:

```python
            elif engine in ("qwen3_asr_or", "whisper_or"):
                # Per-model key (Model Registry entry), not a system-wide toggle --
                # "configured" here means at least one enabled entry for this engine
                # has a key set, so it's actually usable for some model.
                configured = await model_registry_store.has_key_for_engine("stt", engine)
                entry = {"mode": "remote", "available": configured, "detail": provider.model if configured else None}
            elif engine == "openai_stt":
                # Configured = some enabled entry carries a base_url pointing at a
                # service; there is no per-engine singleton config to read.
                row = await model_registry_store.find_enabled("stt", "openai_stt")
                base_url = (row or {}).get("base_url", "")
                entry = {"mode": "remote", "available": bool(base_url), "detail": base_url or None}
            else:
                base_url, model = remote[engine]
                configured = bool(base_url)
                entry = {"mode": "remote", "available": configured, "detail": model if configured else None}
```

In `apps/api_gateway/app/schemas/stt.py`, extend the pattern on line 7 with `openai_stt`:

```python
    engine: str = Field(
        default="vosk",
        pattern="^(vosk|whisper|whisper_local|whisper_mlx|qwen3_asr|whisper_service|eventlab|qwen3_asr_or|whisper_or|openai_stt)$",
    )
```

- [ ] **Step 6: Write the registration test**

```python
# append to tests/unit/test_openai_stt_provider.py
import pytest

from app.schemas.stt import STTRequest
from app.services.stt.service import stt_service


def test_engine_is_registered():
    assert stt_service.get_provider("openai_stt").name == "openai_stt"


def test_schema_accepts_the_engine():
    assert STTRequest(engine="openai_stt").engine == "openai_stt"


@pytest.mark.asyncio
async def test_list_engines_does_not_keyerror_on_the_new_engine(monkeypatch):
    # service.py's list_engines ends in `remote[engine]`, a dict keyed only by
    # whisper_service/eventlab -- a new engine must not fall into that branch.
    engines = await stt_service.list_engines()
    row = next(e for e in engines if e["engine"] == "openai_stt")
    assert row["mode"] == "remote"
```

- [ ] **Step 7: Run the tests**

Run: `pytest tests/unit/test_openai_stt_provider.py tests/unit -k "stt" -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/services/stt tests/unit/test_openai_stt_provider.py apps/api_gateway/app/schemas/stt.py
git commit -m "feat(stt): openai_stt engine for OpenAI-compatible STT services

Resolves base_url/api_key per call, so admin edits need no provider reinit.
Also extends the STTRequest engine pattern and the list_engines mode chain,
which would otherwise 422 and KeyError on the new engine."
```

---

### Task 7: Gateway `openai_tts` provider

**Files:**
- Create: `apps/api_gateway/app/services/tts/providers/openai_tts_provider.py`
- Modify: `apps/api_gateway/app/services/tts/service.py:14-22`
- Test: `tests/unit/test_openai_tts_provider.py`

**Interfaces:**
- Consumes: `RenderingTTSProvider` and its `_render_wav` contract; `model_registry_store.find_enabled(kind, engine)`.
- Produces: `OpenAICompatTTSProvider(name: str = "openai_tts", timeout_seconds: float = 60.0, entry: dict | None = None)`, a `RenderingTTSProvider` whose `_render_wav(payload) -> bytes` POSTs to `{base_url}/audio/speech`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_openai_tts_provider.py
import httpx
import pytest

from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest
from app.services.tts.providers.openai_tts_provider import OpenAICompatTTSProvider

_ENTRY = {
    "id": "t1", "kind": "tts", "engine": "openai_tts", "model_id": "vieneu",
    "label": "local box", "enabled": True, "stage": "stable",
    "api_key": "t0ken", "base_url": "http://tts-service:8100/v1", "config": {},
}


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["json"] = request.read().decode()
        return httpx.Response(200, content=b"RIFFWAVEDATA")

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    return seen


@pytest.mark.asyncio
async def test_posts_to_audio_speech_and_returns_wav_bytes(captured):
    provider = OpenAICompatTTSProvider(entry=_ENTRY)
    wav = await provider.render_wav(TTSRequest(text="xin chào", engine="openai_tts", voice="v1"))

    assert wav == b"RIFFWAVEDATA"
    assert captured["url"] == "http://tts-service:8100/v1/audio/speech"
    assert captured["auth"] == "Bearer t0ken"
    assert '"input":"xin chào"' in captured["json"].replace(" ", "")
    assert '"voice":"v1"' in captured["json"].replace(" ", "")


@pytest.mark.asyncio
async def test_resolves_the_enabled_entry_from_the_registry(captured, monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return _ENTRY

    monkeypatch.setattr(
        "app.services.tts.providers.openai_tts_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    wav = await OpenAICompatTTSProvider().render_wav(TTSRequest(text="hi", engine="openai_tts"))
    assert wav == b"RIFFWAVEDATA"


@pytest.mark.asyncio
async def test_unconfigured_raises_provider_error(monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return None

    monkeypatch.setattr(
        "app.services.tts.providers.openai_tts_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    # render_wav wraps everything as ProviderError (tts/base.py).
    with pytest.raises(ProviderError, match="not configured"):
        await OpenAICompatTTSProvider().render_wav(TTSRequest(text="hi", engine="openai_tts"))


@pytest.mark.asyncio
async def test_http_error_becomes_provider_error(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(502, text="engine died"))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    with pytest.raises(ProviderError, match="HTTP 502"):
        await OpenAICompatTTSProvider(entry=_ENTRY).render_wav(
            TTSRequest(text="hi", engine="openai_tts")
        )


def test_engine_is_registered():
    from app.services.tts.service import tts_service

    assert tts_service.get_provider("openai_tts").name == "openai_tts"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_openai_tts_provider.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.tts.providers.openai_tts_provider'`.

- [ ] **Step 3: Write the provider**

```python
# apps/api_gateway/app/services/tts/providers/openai_tts_provider.py
"""TTS against any OpenAI-compatible /audio/speech endpoint.

The name describes the protocol, not the backend -- the entry can point at
apps/model_service or any other compatible host. The entry is resolved per
call, so admin edits take effect without a provider rebuild.

Only WAV is handled: apps/model_service serves RenderingTTSProvider engines,
which all produce WAV.
"""

from __future__ import annotations

import httpx

from app.schemas.tts import TTSRequest
from app.services.model_registry.store import model_registry_store
from app.services.tts.base import RenderingTTSProvider

_DEFAULT_TIMEOUT = 60.0


class OpenAICompatTTSProvider(RenderingTTSProvider):
    name = "openai_tts"
    sample_rate = 24000

    def __init__(
        self,
        name: str = "openai_tts",
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        entry: dict | None = None,
    ) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds
        # Only the registry's test-before-add call passes an entry.
        self._entry_override = entry

    async def _resolve_entry(self) -> dict | None:
        if self._entry_override is not None:
            return self._entry_override
        return await model_registry_store.find_enabled(kind="tts", engine=self.name)

    def detail(self) -> str:
        return self.name

    def install_hint(self) -> str:
        return "Add a Model Registry entry pointing at a TTS service base URL."

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        entry = await self._resolve_entry()
        base_url = (entry or {}).get("base_url", "").strip()
        if not base_url:
            raise RuntimeError(
                f"{self.name} is not configured. Add a Model Registry entry with the "
                f"service's base URL (e.g. http://tts-service:8100/v1)."
            )

        api_key = (entry or {}).get("api_key", "").strip()
        timeout = (entry.get("config") or {}).get("timeout_seconds") or self.timeout_seconds

        endpoint = f"{base_url.rstrip('/')}/audio/speech"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        body = {
            "model": entry.get("model_id", ""),
            "input": payload.text,
            "voice": payload.voice,
            "speed": payload.speed,
            "language": payload.language,
            "response_format": "wav",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"{self.name} returned HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{self.name} request failed: {exc}") from exc
```

- [ ] **Step 4: Register the engine**

In `apps/api_gateway/app/services/tts/service.py`, add the import:

```python
from app.services.tts.providers.openai_tts_provider import OpenAICompatTTSProvider
```

and add to the `self.providers` dict in `__init__`:

```python
            "openai_tts": OpenAICompatTTSProvider(),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_openai_tts_provider.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/tts tests/unit/test_openai_tts_provider.py
git commit -m "feat(tts): openai_tts engine for OpenAI-compatible TTS services

First HTTP-backed TTS provider in the gateway; resolves its entry per call."
```

---

### Task 8: Registry route — persist `base_url` for TTS, test the new engines

**Files:**
- Modify: `apps/api_gateway/app/api/routes/model_registry.py:69-104`
- Test: `tests/unit/test_model_registry_routes.py` (append — this file already owns the `client` fixture and the `_signup_login` admin helper these routes need; a new file would have to duplicate both)

**Interfaces:**
- Consumes: `OpenAICompatSttProvider(entry=...)` (Task 6), `OpenAICompatTTSProvider(entry=...)` (Task 7).
- Produces: no new symbols; `POST /v1/model_registry` now persists `base_url` for `kind="tts"` and builds the openai_* providers from the submitted payload.

Why this task is mandatory: `routes/model_registry.py:102` writes `base_url` only when `kind in ("llm", "stt")`. A TTS entry submitted with a base_url is **silently stored with an empty one**, so `openai_tts` can never resolve a URL and the whole TTS path is dead. This is a bug fix — write the failing test first.

- [ ] **Step 1: Write the failing regression test**

Append to `tests/unit/test_model_registry_routes.py`, reusing the `client` fixture (line 13) and `_signup_login` helper (line 25) already in that file:

```python
_SERVICE_BASE = "http://tts-service:8100/v1"


def test_tts_entry_keeps_its_base_url(client, monkeypatch):
    """Regression: create_entry whitelisted base_url to (llm, stt), so a TTS
    service entry lost its URL on save and openai_tts could never resolve it."""
    from app.services.tts.providers import openai_tts_provider

    async def fake_render(self, payload):
        return b"RIFFWAVEDATA"

    monkeypatch.setattr(openai_tts_provider.OpenAICompatTTSProvider, "_render_wav", fake_render)
    _signup_login(client, "admin_base_url", role="admin")

    r = client.post(
        "/v1/model_registry",
        json={
            "kind": "tts", "engine": "openai_tts", "model_id": "vieneu",
            "label": "local box", "base_url": _SERVICE_BASE, "api_key": "t0ken",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["base_url"] == _SERVICE_BASE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_model_registry_routes.py::test_tts_entry_keeps_its_base_url -v`
Expected: FAIL — `assert '' == 'http://tts-service:8100/v1'`.

- [ ] **Step 3: Fix the whitelist**

In `apps/api_gateway/app/api/routes/model_registry.py`, replace the comment and `create` call at lines 94-104:

```python
    # Persist api_key for every kind (stt: read by openrouter_provider.py and
    # openai_stt_provider.py; llm: read by responder.py's
    # resolve_llm_override_from_registry; tts: read by openai_tts_provider.py).
    # base_url is meaningful for every kind now: llm and the openai_stt/openai_tts
    # service engines all pair a model with an OpenAI-compatible endpoint.
    created = await model_registry_store.create(
        payload.kind, payload.engine, payload.model_id, payload.label, stage=payload.stage,
        api_key=payload.api_key,
        base_url=payload.base_url,
        config=payload.config,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_model_registry_routes.py::test_tts_entry_keeps_its_base_url -v`
Expected: 1 passed.

- [ ] **Step 5: Build the openai_* providers from the payload at test-before-add time**

Still in `routes/model_registry.py`, add next to `_OPENROUTER_STT_ENGINES` (line 25):

```python
# Service engines whose config lives entirely on the entry being submitted
# (base_url + api_key). Like the OpenRouter engines, the singleton provider
# would look up a row that doesn't exist yet, so the add-time test call gets an
# explicit entry built from the payload.
_SERVICE_STT_ENGINES = {"openai_stt"}
_SERVICE_TTS_ENGINES = {"openai_tts"}
```

Replace the `if payload.kind == "stt": ... elif payload.kind == "tts": ...` block (lines 70-80) with:

```python
        if payload.kind == "stt":
            if payload.engine in _OPENROUTER_STT_ENGINES:
                provider = OpenRouterSttProvider(
                    name=payload.engine, model=payload.model_id, api_key=payload.api_key
                )
            elif payload.engine in _SERVICE_STT_ENGINES:
                provider = OpenAICompatSttProvider(name=payload.engine, entry=payload.model_dump())
            else:
                provider = stt_service.get_provider(payload.engine)
            await provider.transcribe_bytes(_SAMPLE_WAV)
        elif payload.kind == "tts":
            if payload.engine in _SERVICE_TTS_ENGINES:
                provider = OpenAICompatTTSProvider(name=payload.engine, entry=payload.model_dump())
            else:
                provider = tts_service.get_provider(payload.engine)
            await provider.synthesize(TTSRequest(text=payload.sample_text, engine=payload.engine))
```

`CreateEntryRequest.model_dump()` yields `base_url`, `api_key`, `model_id`, and `config` — exactly the keys both providers read from an entry.

Add the imports at the top:

```python
from app.services.stt.providers.openai_stt_provider import OpenAICompatSttProvider
from app.services.tts.providers.openai_tts_provider import OpenAICompatTTSProvider
```

- [ ] **Step 6: Write the add-time test**

```python
# append to tests/unit/test_model_registry_routes.py
def test_bad_service_url_is_rejected_at_add_time(client):
    """The admin should learn the URL/token is wrong when they click Add, not on
    the first real transcription."""
    _signup_login(client, "admin_bad_url", role="admin")
    r = client.post(
        "/v1/model_registry",
        json={
            "kind": "stt", "engine": "openai_stt", "model_id": "phowhisper-medium",
            "label": "typo", "base_url": "http://nonexistent.invalid:9/v1", "api_key": "t0ken",
        },
    )
    assert r.status_code == 400
    assert "openai_stt" in r.json()["detail"]
```

- [ ] **Step 7: Run the registry suite**

Run: `pytest tests/unit -k "model_registry" -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/api/routes/model_registry.py tests/unit/test_model_registry_base_url.py
git commit -m "fix(model-registry): persist base_url for tts entries

create_entry whitelisted base_url to (llm, stt), so a TTS service entry was
silently saved with an empty URL. Also builds openai_stt/openai_tts from the
submitted payload for the test-before-add call."
```

---

### Task 9: Docker image + compose

**Files:**
- Create: `infra/docker/Dockerfile.model_service`
- Modify: `infra/compose/docker-compose.yml`
- Create: `docs/model-service.md`

**Interfaces:**
- Consumes: `apps/model_service/app/main.py:create_app`.
- Produces: an image running `uvicorn model_service.app.main:create_app --factory`, and a `model-service` compose service behind the `models` profile.

- [ ] **Step 1: Read the existing Dockerfile to match its conventions**

Run: `cat infra/docker/Dockerfile.api`
Expected: a `python:3.11-slim` base installing `libsndfile1` + `libopus0` and `pip install ".[tts,opus]"`.

- [ ] **Step 2: Write the Dockerfile**

```dockerfile
# infra/docker/Dockerfile.model_service
# One image for every local engine; SERVICE_KIND + SERVICE_ENGINE pick which
# one this container runs. Reuses the gateway's providers -- hence the same
# system deps and the api_gateway package on PYTHONPATH.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY apps ./apps

RUN pip install --no-cache-dir ".[tts,opus]"

ENV PYTHONPATH=/app/apps/api_gateway:/app/apps \
    PYTHONUNBUFFERED=1 \
    SERVICE_PORT=8100 \
    ARTIFACTS_DIR=/tmp/artifacts \
    DATABASE_URL=sqlite+aiosqlite:////tmp/model_service.db

# ARTIFACTS_DIR: app/services/artifacts.py mkdirs at import time and this
# service never serves artifacts -- keep it off the image's working tree.
# DATABASE_URL: three providers still read system_config_store for inert
# defaults (glossary path, default voice); point them at a throwaway file so
# nothing tries to write into a read-only rootfs.

EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"SERVICE_PORT\"]}/health').read()"

# --factory: main.py has no module-level `app`, so that importing it in tests
# doesn't run load_config() and die on missing env.
CMD ["sh", "-c", "uvicorn model_service.app.main:create_app --factory --host 0.0.0.0 --port ${SERVICE_PORT}"]
```

`--start-period=120s` because the first request may download model weights.

- [ ] **Step 3: Build the image**

Run: `docker build -f infra/docker/Dockerfile.model_service -t model-service:dev .`
Expected: build succeeds.

- [ ] **Step 4: Verify the container refuses to run without a token**

Run: `docker run --rm -e SERVICE_KIND=stt -e SERVICE_ENGINE=vosk model-service:dev`
Expected: exits non-zero with `ConfigError: SERVICE_API_TOKEN is required; the service refuses to run open`.

- [ ] **Step 5: Add the compose service**

Add to `infra/compose/docker-compose.yml` under `services:` (keep the existing `api` and `redis` untouched):

```yaml
  model-service:
    # Opt-in: `docker compose --profile models up`. Left out of the default
    # `up` so gateway development doesn't pull model weights.
    profiles: ["models"]
    build:
      context: ../..
      dockerfile: infra/docker/Dockerfile.model_service
    environment:
      SERVICE_KIND: stt
      SERVICE_ENGINE: vosk
      SERVICE_API_TOKEN: ${MODEL_SERVICE_TOKEN:?set MODEL_SERVICE_TOKEN}
      STT_VOSK_MODEL_PATH: /models/vosk-model-small-en-us-0.15
    volumes:
      - ../../models:/models:ro
    ports:
      - "8100:8100"
```

`${MODEL_SERVICE_TOKEN:?...}` makes compose fail loudly rather than starting an unauthenticated service.

- [ ] **Step 6: End-to-end check with vosk (small, CPU-only)**

Run:
```bash
MODEL_SERVICE_TOKEN=dev-token docker compose -f infra/compose/docker-compose.yml --profile models up -d model-service
curl -sf http://localhost:8100/health
curl -s -H "Authorization: Bearer dev-token" \
  -F file=@tests/fixtures/sample.wav -F language=en \
  http://localhost:8100/v1/audio/transcriptions
```
Expected: `/health` returns `{"status":"ok","kind":"stt","engine":"vosk"}`; the transcription returns `{"text": ...}`. If no fixture WAV exists, generate one:
`python -c "from app.core.audio import pcm16_to_wav_bytes; open('/tmp/s.wav','wb').write(pcm16_to_wav_bytes(b'\x00\x00'*16000, sample_rate=16000))"`

Also verify auth actually bites:
```bash
curl -s -o /dev/null -w '%{http_code}' -F file=@/tmp/s.wav http://localhost:8100/v1/audio/transcriptions
```
Expected: `401`.

- [ ] **Step 7: Write the docs**

Create `docs/model-service.md` covering: what the service is and why (engines as services, not in-process models); the env table (`SERVICE_KIND`, `SERVICE_ENGINE`, `SERVICE_API_TOKEN`, `SERVICE_PORT`, `ARTIFACTS_DIR`, `DATABASE_URL`, plus the `STT_{ENGINE}_{KEY}` env layer with `STT_WHISPER_LOCAL_DEFAULT_MODEL`/`STT_WHISPER_LOCAL_DEVICE` as examples); a `docker run` example per kind; and the Model Registry steps to wire it into the gateway (kind `stt`, engine `openai_stt`, `base_url` `http://stt-service:8100/v1`, `api_key` = the container's token). State the v1 limits: no LLM, no OmniVoice, no `edge_tts`, no streaming over the service boundary, and glossary not configurable in-container.

- [ ] **Step 8: Run the full unit suite**

Run: `pytest tests/unit -q`
Expected: no failures beyond the 3 pre-existing ones noted in the project's session history. Compare against `git stash && pytest tests/unit -q` if unsure.

- [ ] **Step 9: Commit**

```bash
git add infra/docker/Dockerfile.model_service infra/compose/docker-compose.yml docs/model-service.md
git commit -m "feat(model-service): docker image + compose profile + docs

One image; SERVICE_KIND/SERVICE_ENGINE pick the engine. Behind the 'models'
compose profile so the default up is unchanged."
```

---

## Verification

After Task 9, verify the whole loop by hand — the unit tests all use fakes, so nothing above proves a real model works behind a real HTTP hop:

1. `MODEL_SERVICE_TOKEN=dev-token docker compose -f infra/compose/docker-compose.yml --profile models up -d`
2. In the gateway admin UI, add a Model Registry entry: kind `stt`, engine `openai_stt`, model_id `vosk-model-small-en-us-0.15`, base_url `http://model-service:8100/v1`, api_key `dev-token`. It must save without error — that proves the test-before-add call reached the container.
3. Confirm `GET /v1/stt/engines` lists `openai_stt` with `mode: remote`, `available: true`.
4. Run a transcription through the gateway with `engine=openai_stt` and confirm the text comes back.
5. Confirm the gateway process never loaded a model: no `faster_whisper`/`vosk` memory growth in the `api` container.
