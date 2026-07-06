# TTS Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone, named TTS Profile (engine + preset-or-cloned voice + style/speed/language) that an LLM Profile or a live session can reference by name instead of configuring TTS parameters every time.

**Architecture:** New `TtsProfile` model + `TtsProfileStore` (JSON file, mirrors the existing `services/profiles/` pattern exactly) behind a new `/v1/tts/profiles` CRUD router. The existing LLM `Profile.tts` (`TtsConfig`) shrinks from inline `engine`/`voice` fields to a single `profile_name` reference. `conversation.py` and `livehost.py` resolve a `TtsProfile` (query param `?tts_profile=` > linked LLM profile > legacy fallback) and pass its full field set into every `TTSRequest` built during a turn. UI: LLM Profile panel's engine/voice inputs become one profile-name dropdown; a new "TTS Profiles" card manages the profiles themselves.

**Tech Stack:** FastAPI, Pydantic v2, vanilla JS ES modules, pytest + `TestClient` (incl. WebSocket test client).

## Global Constraints

- `profiles.json` is empty in this repo (verified both at repo root and `apps/api_gateway/`) — no migration logic needed; `TtsConfig`'s schema can change directly.
- Mirror `services/profiles/{models,store}.py` and `api/routes/profiles.py` byte-for-byte in shape for the new TTS-profile equivalents — same atomic-write JSON store, same CRUD route shapes, same `threading.Lock` guard.
- Every per-sentence `TTSRequest` built in `conversation.py` / `livehost.py` must carry the resolved profile's `ref_audio_path`, `ref_text`, `instruct`, `speed`, and `language` — not just `engine`/`voice` as today.
- No new UI test framework exists in this repo (no Playwright/Jest config) — new/changed JS files are checked with `node --check`; final manual verification is done by running `make dev` and exercising the panel in a browser (per this project's UI-change convention).

---

### Task 1: `TtsProfile` model + `tts_profiles_path` setting

**Files:**
- Create: `apps/api_gateway/app/services/tts/profile_models.py`
- Modify: `apps/api_gateway/app/core/settings.py:195` (insert after `profiles_path`)
- Test: `tests/unit/test_tts_profile_models.py`

**Interfaces:**
- Produces: `TtsProfile(BaseModel)` with fields `name: str`, `engine: str = ""`, `voice_mode: Literal["preset", "clone"] = "preset"`, `voice: str = ""`, `ref_audio_path: str = ""`, `ref_text: str = ""`, `instruct: str = ""`, `speed: float | None = None`, `language: str | None = None`. Produces `settings.tts_profiles_path: str = "tts_profiles.json"`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tts_profile_models.py`:

```python
from app.services.tts.profile_models import TtsProfile


def test_tts_profile_defaults():
    p = TtsProfile(name="x")
    assert p.engine == ""
    assert p.voice_mode == "preset"
    assert p.voice == ""
    assert p.ref_audio_path == ""
    assert p.ref_text == ""
    assert p.instruct == ""
    assert p.speed is None
    assert p.language is None


def test_tts_profile_preset_full():
    p = TtsProfile(name="cohost-girl", engine="vieneu", voice_mode="preset", voice="vi-female-1")
    assert p.name == "cohost-girl"
    assert p.engine == "vieneu"
    assert p.voice == "vi-female-1"


def test_tts_profile_clone_full():
    p = TtsProfile(
        name="cloned-host", engine="omnivoice", voice_mode="clone",
        ref_audio_path="artifacts/refs/host.wav", ref_text="Xin chao cac ban",
        instruct="cheerful", speed=1.2, language="vi",
    )
    assert p.voice_mode == "clone"
    assert p.ref_audio_path == "artifacts/refs/host.wav"
    assert p.ref_text == "Xin chao cac ban"
    assert p.instruct == "cheerful"
    assert p.speed == 1.2
    assert p.language == "vi"


def test_tts_profile_roundtrip():
    p = TtsProfile(name="rt", engine="vieneu", speed=0.9)
    data = p.model_dump()
    p2 = TtsProfile.model_validate(data)
    assert p2.engine == "vieneu"
    assert p2.speed == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tts_profile_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.tts.profile_models'`

- [ ] **Step 3: Write the model**

Create `apps/api_gateway/app/services/tts/profile_models.py`:

```python
from typing import Literal

from pydantic import BaseModel


class TtsProfile(BaseModel):
    name: str
    engine: str = ""
    voice_mode: Literal["preset", "clone"] = "preset"
    voice: str = ""            # preset mode: voice id from GET /v1/tts/voices?engine=
    ref_audio_path: str = ""   # clone mode
    ref_text: str = ""         # clone mode: transcript of the reference audio
    instruct: str = ""         # style/emotion instruction (engine-dependent, e.g. omnivoice)
    speed: float | None = None
    language: str | None = None
```

Then edit `apps/api_gateway/app/core/settings.py`, in the "LLM profiles + MCP tooling" block:

```python
    # LLM profiles + MCP tooling
    profiles_path: str = "profiles.json"
    tts_profiles_path: str = "tts_profiles.json"
    mcp_servers_path: str = "mcp_servers.json"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_tts_profile_models.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/tts/profile_models.py apps/api_gateway/app/core/settings.py tests/unit/test_tts_profile_models.py
git commit -m "feat(tts): add TtsProfile model"
```

---

### Task 2: `TtsProfileStore`

**Files:**
- Create: `apps/api_gateway/app/services/tts/profile_store.py`
- Test: `tests/unit/test_tts_profile_store.py`

**Interfaces:**
- Consumes: `TtsProfile` from Task 1 (`app.services.tts.profile_models`); `settings.tts_profiles_path` from Task 1.
- Produces: `TtsProfileStore(path: str)` with `.list() -> dict[str, TtsProfile]`, `.get(name: str) -> TtsProfile | None`, `.upsert(profile: TtsProfile) -> None`, `.delete(name: str) -> None`. Module singleton `tts_profile_store = TtsProfileStore(settings.tts_profiles_path)`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tts_profile_store.py`:

```python
import pytest

from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore


@pytest.fixture
def store(tmp_path):
    return TtsProfileStore(str(tmp_path / "tts_profiles.json"))


def test_empty_store_returns_empty_dict(store):
    assert store.list() == {}


def test_upsert_and_get(store):
    p = TtsProfile(name="test", engine="vieneu")
    store.upsert(p)
    result = store.get("test")
    assert result is not None
    assert result.engine == "vieneu"


def test_get_missing_returns_none(store):
    assert store.get("nonexistent") is None


def test_list_multiple_profiles(store):
    store.upsert(TtsProfile(name="a"))
    store.upsert(TtsProfile(name="b"))
    profiles = store.list()
    assert set(profiles.keys()) == {"a", "b"}


def test_upsert_overwrites_existing(store):
    store.upsert(TtsProfile(name="x", engine="vieneu"))
    store.upsert(TtsProfile(name="x", engine="omnivoice"))
    assert store.get("x").engine == "omnivoice"


def test_delete_removes_profile(store):
    store.upsert(TtsProfile(name="del"))
    store.delete("del")
    assert store.get("del") is None


def test_delete_nonexistent_is_noop(store):
    store.delete("ghost")  # should not raise


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "tts_profiles.json")
    s1 = TtsProfileStore(path)
    s1.upsert(TtsProfile(name="persist", engine="vieneu"))
    s2 = TtsProfileStore(path)
    assert s2.get("persist").engine == "vieneu"


def test_auto_creates_file(tmp_path):
    store = TtsProfileStore(str(tmp_path / "new.json"))
    assert store.list() == {}


def test_clone_profile_roundtrips(store):
    p = TtsProfile(
        name="cloned", engine="omnivoice", voice_mode="clone",
        ref_audio_path="artifacts/refs/host.wav", ref_text="hello",
    )
    store.upsert(p)
    result = store.get("cloned")
    assert result.voice_mode == "clone"
    assert result.ref_audio_path == "artifacts/refs/host.wav"
    assert result.ref_text == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tts_profile_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.tts.profile_store'`

- [ ] **Step 3: Write the store**

Create `apps/api_gateway/app/services/tts/profile_store.py`:

```python
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.core.settings import settings
from app.services.tts.profile_models import TtsProfile


class TtsProfileStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._ensure()

    def _ensure(self) -> None:
        if not self._path.exists():
            self._write({})

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
            return data.get("profiles", {})
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _write(self, profiles: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"profiles": profiles}, indent=2))
        tmp.replace(self._path)

    def list(self) -> dict[str, TtsProfile]:
        with self._lock:
            return {k: TtsProfile.model_validate(v) for k, v in self._read().items()}

    def get(self, name: str) -> TtsProfile | None:
        return self.list().get(name)

    def upsert(self, profile: TtsProfile) -> None:
        with self._lock:
            profiles = self._read()
            profiles[profile.name] = profile.model_dump()
            self._write(profiles)

    def delete(self, name: str) -> None:
        with self._lock:
            profiles = self._read()
            profiles.pop(name, None)
            self._write(profiles)


tts_profile_store = TtsProfileStore(settings.tts_profiles_path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_tts_profile_store.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/tts/profile_store.py tests/unit/test_tts_profile_store.py
git commit -m "feat(tts): add TtsProfileStore"
```

---

### Task 3: `/v1/tts/profiles` CRUD API

**Files:**
- Create: `apps/api_gateway/app/api/routes/tts_profiles.py`
- Modify: `apps/api_gateway/app/main.py:26` (import), `apps/api_gateway/app/main.py:117` (register)
- Test: `tests/unit/test_tts_profile_routes.py`

**Interfaces:**
- Consumes: `TtsProfile`, `tts_profile_store` from Tasks 1–2.
- Produces: `router` (FastAPI `APIRouter`, prefix `/v1/tts/profiles`) exposing `GET ""`, `POST ""`, `GET "/{name}"`, `PUT "/{name}"`, `DELETE "/{name}"` — same response envelope (`{"success": True, "data": ...}`) as `api/routes/profiles.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tts_profile_routes.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.tts.profile_store import TtsProfileStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.tts_profiles.tts_profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


def test_list_tts_profiles_empty(client):
    resp = client.get("/v1/tts/profiles")
    assert resp.status_code == 200
    assert resp.json()["data"] == {}


def test_create_tts_profile(client):
    payload = {"name": "test", "engine": "vieneu", "voice_mode": "preset", "voice": "v1"}
    resp = client.post("/v1/tts/profiles", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == "test"
    assert data["engine"] == "vieneu"


def test_get_tts_profile(client):
    client.post("/v1/tts/profiles", json={"name": "x", "engine": "vieneu"})
    resp = client.get("/v1/tts/profiles/x")
    assert resp.status_code == 200
    assert resp.json()["data"]["engine"] == "vieneu"


def test_get_missing_tts_profile_404(client):
    resp = client.get("/v1/tts/profiles/ghost")
    assert resp.status_code == 404


def test_update_tts_profile(client):
    client.post("/v1/tts/profiles", json={"name": "upd", "engine": "vieneu"})
    resp = client.put("/v1/tts/profiles/upd", json={"name": "upd", "engine": "omnivoice"})
    assert resp.status_code == 200
    assert resp.json()["data"]["engine"] == "omnivoice"


def test_update_uses_path_name(client):
    resp = client.put("/v1/tts/profiles/canonical", json={"name": "ignored", "engine": "vieneu"})
    assert resp.json()["data"]["name"] == "canonical"


def test_delete_tts_profile(client):
    client.post("/v1/tts/profiles", json={"name": "del"})
    resp = client.delete("/v1/tts/profiles/del")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    assert client.get("/v1/tts/profiles/del").status_code == 404


def test_list_shows_created_tts_profile(client):
    client.post("/v1/tts/profiles", json={"name": "visible"})
    resp = client.get("/v1/tts/profiles")
    assert "visible" in resp.json()["data"]


def test_create_clone_tts_profile(client):
    payload = {
        "name": "cloned", "engine": "omnivoice", "voice_mode": "clone",
        "ref_audio_path": "artifacts/refs/host.wav", "ref_text": "hello there",
        "instruct": "cheerful", "speed": 1.2, "language": "vi",
    }
    resp = client.post("/v1/tts/profiles", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["voice_mode"] == "clone"
    assert data["ref_audio_path"] == "artifacts/refs/host.wav"
    assert data["speed"] == 1.2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tts_profile_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.routes.tts_profiles'`

- [ ] **Step 3: Write the routes and register the router**

Create `apps/api_gateway/app/api/routes/tts_profiles.py`:

```python
from fastapi import APIRouter, HTTPException

from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import tts_profile_store

router = APIRouter(prefix="/v1/tts/profiles", tags=["tts"])


@router.get("")
async def list_tts_profiles() -> dict:
    profiles = tts_profile_store.list()
    return {"success": True, "data": {k: v.model_dump() for k, v in profiles.items()}}


@router.post("")
async def create_tts_profile(payload: TtsProfile) -> dict:
    tts_profile_store.upsert(payload)
    return {"success": True, "data": payload.model_dump()}


@router.get("/{name}")
async def get_tts_profile(name: str) -> dict:
    profile = tts_profile_store.get(name)
    if not profile:
        raise HTTPException(status_code=404, detail=f"TTS profile '{name}' not found")
    return {"success": True, "data": profile.model_dump()}


@router.put("/{name}")
async def update_tts_profile(name: str, payload: TtsProfile) -> dict:
    data = payload.model_dump()
    data["name"] = name
    profile = TtsProfile(**data)
    tts_profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


@router.delete("/{name}")
async def delete_tts_profile(name: str) -> dict:
    tts_profile_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}
```

Edit `apps/api_gateway/app/main.py` — add the import next to the other route imports (alphabetically after `system` import, before `ui`):

```python
from app.api.routes.system import router as system_router
from app.api.routes.tts import router as tts_router
from app.api.routes.tts_profiles import router as tts_profiles_router
from app.api.routes.ui import router as ui_router
```

And register it next to `tts_router`:

```python
app.include_router(tts_router)
app.include_router(tts_profiles_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_tts_profile_routes.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/tts_profiles.py apps/api_gateway/app/main.py tests/unit/test_tts_profile_routes.py
git commit -m "feat(tts): add /v1/tts/profiles CRUD API"
```

---

### Task 4: `TtsConfig` becomes a reference; update existing profile tests

**Files:**
- Modify: `apps/api_gateway/app/services/profiles/models.py:14-16`
- Modify: `tests/unit/test_profiles_models.py:8,18`
- Modify: `tests/unit/test_profiles_store.py:68,74`
- Modify: `tests/unit/test_profiles_routes.py:32`

**Interfaces:**
- Produces: `TtsConfig(BaseModel)` with a single field `profile_name: str = ""` (was `engine: str = ""`, `voice: str = ""`).

- [ ] **Step 1: Update the existing tests first (they currently pass against the old shape — change them to assert the new shape, which will fail until Step 3)**

In `tests/unit/test_profiles_models.py`, change:

```python
def test_profile_defaults():
    p = Profile(name="x")
    assert p.llm.base_url == ""
    assert p.tts.engine == ""
    assert p.mcp_servers == []
    assert p.system_prompt == ""
```

to:

```python
def test_profile_defaults():
    p = Profile(name="x")
    assert p.llm.base_url == ""
    assert p.tts.profile_name == ""
    assert p.mcp_servers == []
    assert p.system_prompt == ""
```

And change:

```python
def test_profile_full():
    p = Profile(
        name="home",
        llm=LlmConfig(base_url="http://localhost:11434/v1", api_key="", model="llama3.2"),
        system_prompt="You are a home assistant.",
        tts=TtsConfig(engine="vieneu", voice=""),
        mcp_servers=[McpServer(name="ha", url="http://localhost:3001/mcp")],
    )
```

to:

```python
def test_profile_full():
    p = Profile(
        name="home",
        llm=LlmConfig(base_url="http://localhost:11434/v1", api_key="", model="llama3.2"),
        system_prompt="You are a home assistant.",
        tts=TtsConfig(profile_name="cohost-voice"),
        mcp_servers=[McpServer(name="ha", url="http://localhost:3001/mcp")],
    )
```

(the rest of that test's assertions are unchanged).

In `tests/unit/test_profiles_store.py`, change:

```python
def test_profile_with_llm_and_tts_roundtrips(store):
    p = Profile(
        name="full",
        llm=LlmConfig(base_url="http://localhost:11434/v1", model="llama3.2"),
        tts=TtsConfig(engine="vieneu"),
        system_prompt="Be helpful.",
    )
    store.upsert(p)
    result = store.get("full")
    assert result.llm.model == "llama3.2"
    assert result.tts.engine == "vieneu"
```

to:

```python
def test_profile_with_llm_and_tts_roundtrips(store):
    p = Profile(
        name="full",
        llm=LlmConfig(base_url="http://localhost:11434/v1", model="llama3.2"),
        tts=TtsConfig(profile_name="cohost-voice"),
        system_prompt="Be helpful.",
    )
    store.upsert(p)
    result = store.get("full")
    assert result.llm.model == "llama3.2"
    assert result.tts.profile_name == "cohost-voice"
```

In `tests/unit/test_profiles_routes.py`, change:

```python
def test_create_profile(client):
    payload = {
        "name": "test",
        "system_prompt": "Be brief.",
        "llm": {"base_url": "http://localhost:11434/v1", "api_key": "", "model": "llama3.2"},
        "tts": {"engine": "vieneu", "voice": ""},
        "mcp_servers": [],
    }
```

to:

```python
def test_create_profile(client):
    payload = {
        "name": "test",
        "system_prompt": "Be brief.",
        "llm": {"base_url": "http://localhost:11434/v1", "api_key": "", "model": "llama3.2"},
        "tts": {"profile_name": "cohost-voice"},
        "mcp_servers": [],
    }
```

- [ ] **Step 2: Run the updated tests to verify they fail against the old model**

Run: `pytest tests/unit/test_profiles_models.py tests/unit/test_profiles_store.py tests/unit/test_profiles_routes.py -v`
Expected: `test_profile_defaults` and `test_profile_with_llm_and_tts_roundtrips` FAIL with `AttributeError: 'TtsConfig' object has no attribute 'profile_name'` (pydantic v2 silently ignores the unknown `profile_name` kwarg against the old model, so construction itself doesn't error — the attribute access does). `test_profile_full` and `test_create_profile` still pass at this point since neither asserts anything about the `tts` field's shape; they'll still pass unchanged after Step 3 too.

- [ ] **Step 3: Update the model**

In `apps/api_gateway/app/services/profiles/models.py`, change:

```python
class TtsConfig(BaseModel):
    engine: str = ""
    voice: str = ""
```

to:

```python
class TtsConfig(BaseModel):
    profile_name: str = ""   # name of a TtsProfile (services/tts/profile_store.py); "" = server defaults
```

- [ ] **Step 4: Run all four test files to verify they pass**

Run: `pytest tests/unit/test_profiles_models.py tests/unit/test_profiles_store.py tests/unit/test_profiles_routes.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/profiles/models.py tests/unit/test_profiles_models.py tests/unit/test_profiles_store.py tests/unit/test_profiles_routes.py
git commit -m "refactor(profiles): TtsConfig references a TtsProfile by name instead of inlining engine/voice"
```

---

### Task 5: Resolve TTS Profile in `conversation.py`

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py:33` (import), `:239-244` (resolution), `:467` (TTSRequest build)
- Test: `tests/unit/test_conversation_tts_profile.py`

**Interfaces:**
- Consumes: `TtsProfile`, `tts_profile_store` from Tasks 1–2; `TtsConfig.profile_name` from Task 4.
- Produces: within `conversation_stream`, local variables `tts_engine`, `voice`, `ref_audio_path`, `ref_text`, `tts_instruct`, `tts_speed`, `tts_language`, all fed into every `TTSRequest` built in `_stream_to_tts`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_conversation_tts_profile.py`:

```python
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.profiles.models import Profile, TtsConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-conv-ttsp"

    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _RecordingTTS(TTSProvider):
    name = "stub-conv-ttsp-tts"

    def __init__(self) -> None:
        self.calls: list = []

    async def synthesize(self, payload) -> TTSResult:
        self.calls.append(payload)
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    stt_service.providers["stub-conv-ttsp"] = _StubSTT()
    stub_tts = _RecordingTTS()
    tts_service.providers["stub-conv-ttsp-tts"] = stub_tts

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_tts_profiles = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)
    monkeypatch.setattr("app.api.routes.conversation.tts_profile_store", fresh_tts_profiles)

    yield stub_tts, fresh_profiles, fresh_tts_profiles

    stt_service.providers.pop("stub-conv-ttsp", None)
    tts_service.providers.pop("stub-conv-ttsp-tts", None)


@pytest.fixture
def client():
    return TestClient(app)


def _loud(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return (b"\x00\x00") * int(SR * ms / 1000)


def _run_one_turn(ws):
    ws.send_bytes(_loud(500))
    ws.send_bytes(_silence(500))
    ws.send_bytes(_silence(500))
    for _ in range(30):
        ev = ws.receive_json()
        if ev["event"] == "turn_done":
            return


def test_tts_profile_linked_from_llm_profile_resolves_clone_fields(client, _hermetic):
    stub_tts, profiles, tts_profiles = _hermetic
    tts_profiles.upsert(TtsProfile(
        name="cloned-host", engine="stub-conv-ttsp-tts", voice_mode="clone",
        ref_audio_path="artifacts/refs/host.wav", ref_text="hello there",
        instruct="cheerful", speed=1.2, language="vi",
    ))
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="cloned-host")))

    url = "/v1/conversation/stream?stt_engine=stub-conv-ttsp&profile=host&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    assert stub_tts.calls, "TTS provider was never invoked"
    payload = stub_tts.calls[0]
    assert payload.ref_audio_path == "artifacts/refs/host.wav"
    assert payload.ref_text == "hello there"
    assert payload.instruct == "cheerful"
    assert payload.speed == 1.2
    assert payload.language == "vi"


def test_query_param_tts_profile_overrides_llm_profile(client, _hermetic):
    stub_tts, profiles, tts_profiles = _hermetic
    tts_profiles.upsert(TtsProfile(name="from-llm-profile", engine="stub-conv-ttsp-tts", voice="v1"))
    tts_profiles.upsert(TtsProfile(
        name="pinned", engine="stub-conv-ttsp-tts", voice_mode="clone",
        ref_audio_path="ref.wav", ref_text="pinned voice",
    ))
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="from-llm-profile")))

    url = (
        "/v1/conversation/stream?stt_engine=stub-conv-ttsp&profile=host"
        "&tts_profile=pinned&sample_rate=16000"
    )
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    payload = stub_tts.calls[0]
    assert payload.ref_audio_path == "ref.wav"
    assert payload.ref_text == "pinned voice"


def test_no_tts_profile_falls_back_to_legacy_query_params(client, _hermetic):
    stub_tts, _profiles, _tts_profiles = _hermetic
    url = (
        "/v1/conversation/stream?stt_engine=stub-conv-ttsp"
        "&tts_engine=stub-conv-ttsp-tts&voice=manual-voice&sample_rate=16000"
    )
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    payload = stub_tts.calls[0]
    assert payload.engine == "stub-conv-ttsp-tts"
    assert payload.voice == "manual-voice"
    assert payload.ref_audio_path is None
    assert payload.instruct is None
    assert payload.speed is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_conversation_tts_profile.py -v`
Expected: FAIL. `test_tts_profile_linked_from_llm_profile_resolves_clone_fields` and `test_query_param_tts_profile_overrides_llm_profile` both set `profile=host`, and the still-unmodified resolution code reads `profile.tts.engine` — an attribute that no longer exists on `TtsConfig` after Task 4 (it only has `profile_name` now). That raises `AttributeError` inside the handler before `session_started` is ever sent, so both tests fail at `assert ws.receive_json()["event"] == "session_started"` (the connection errors/closes instead). `test_no_tts_profile_falls_back_to_legacy_query_params` passes no `profile=` at all, so `profile` is `None`, the `profile.tts.engine` access short-circuits away, and it already passes — that's fine, it locks in today's legacy-fallback behavior.

- [ ] **Step 3: Wire up resolution and TTSRequest construction**

In `apps/api_gateway/app/api/routes/conversation.py`, add the import next to the other `app.services.profiles` import:

```python
from app.services.profiles.store import profile_store
from app.services.tts.profile_store import tts_profile_store
```

Replace the resolution block:

```python
    if profile and profile.tts.engine:
        tts_engine = profile.tts.engine
        voice = profile.tts.voice or q.get("voice") or None
    else:
        tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
        voice = q.get("voice") or None
```

with:

```python
    # TTS profile resolution: ?tts_profile= (explicit pin) > the active LLM
    # profile's linked TTS profile > legacy tts_engine/voice query params.
    tts_profile_name = q.get("tts_profile") or (profile.tts.profile_name if profile else "") or None
    tts_profile = tts_profile_store.get(tts_profile_name) if tts_profile_name else None
    if tts_profile and tts_profile.engine:
        tts_engine = tts_profile.engine
        voice = tts_profile.voice or q.get("voice") or None
        ref_audio_path = tts_profile.ref_audio_path or None
        ref_text = tts_profile.ref_text or None
        tts_instruct = tts_profile.instruct or None
        tts_speed = tts_profile.speed
        tts_language = tts_profile.language
    else:
        tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
        voice = q.get("voice") or None
        ref_audio_path = ref_text = tts_instruct = None
        tts_speed = tts_language = None
```

Replace the `TTSRequest` build inside `_stream_to_tts`'s `_synth`:

```python
            async def _synth(sentence: str):
                result = await tts_provider.synthesize(
                    TTSRequest(text=sentence, engine=tts_engine, voice=voice)
                )
```

with:

```python
            async def _synth(sentence: str):
                result = await tts_provider.synthesize(
                    TTSRequest(
                        text=sentence, engine=tts_engine, voice=voice,
                        ref_audio_path=ref_audio_path, ref_text=ref_text,
                        instruct=tts_instruct, speed=tts_speed, language=tts_language,
                    )
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_conversation_tts_profile.py -v`
Expected: 3 passed

- [ ] **Step 5: Run the full existing conversation test suite to check for regressions**

Run: `pytest tests/integration/test_conversation_ws.py tests/unit/test_conversation_profile.py -v`
Expected: all pass (these use `?tts_engine=` directly with no profile, which is exactly the untouched legacy-fallback branch)

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py tests/unit/test_conversation_tts_profile.py
git commit -m "feat(conversation): resolve TTS profile (clone/instruct/speed/language) per turn"
```

---

### Task 6: Resolve TTS Profile in `livehost.py`

**Files:**
- Modify: `apps/api_gateway/app/api/routes/livehost.py:21` (import), `:87-92` (resolution), `:228` (TTSRequest build)
- Test: `tests/unit/test_livehost_tts_profile.py`

**Interfaces:**
- Consumes: same as Task 5.
- Produces: within `livehost_stream`, the same seven local variables as Task 5, fed into `_stream_to_tts`'s `TTSRequest`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_livehost_tts_profile.py`:

```python
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.profiles.models import Profile, TtsConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-livehost-ttsp"

    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="chao ban", is_final=True)


class _RecordingTTS(TTSProvider):
    name = "stub-livehost-ttsp-tts"

    def __init__(self) -> None:
        self.calls: list = []

    async def synthesize(self, payload) -> TTSResult:
        self.calls.append(payload)
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    stt_service.providers["stub-livehost-ttsp"] = _StubSTT()
    stub_tts = _RecordingTTS()
    tts_service.providers["stub-livehost-ttsp-tts"] = stub_tts

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_tts_profiles = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.livehost.profile_store", fresh_profiles)
    monkeypatch.setattr("app.api.routes.livehost.tts_profile_store", fresh_tts_profiles)

    yield stub_tts, fresh_profiles, fresh_tts_profiles

    stt_service.providers.pop("stub-livehost-ttsp", None)
    tts_service.providers.pop("stub-livehost-ttsp-tts", None)


@pytest.fixture
def client():
    return TestClient(app)


def _loud(ms: int) -> bytes:
    n = int(SR * ms / 1000)
    return (np.full(n, 0.2, dtype=np.float32) * 32767).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return (b"\x00\x00") * int(SR * ms / 1000)


def _run_one_turn(ws):
    ws.send_bytes(_loud(500))
    ws.send_bytes(_silence(500))
    ws.send_bytes(_silence(500))
    for _ in range(20):
        ev = ws.receive_json()
        if ev["event"] == "turn_done":
            return


def test_livehost_tts_profile_linked_from_llm_profile(client, _hermetic):
    stub_tts, profiles, tts_profiles = _hermetic
    tts_profiles.upsert(TtsProfile(
        name="cloned-host", engine="stub-livehost-ttsp-tts", voice_mode="clone",
        ref_audio_path="artifacts/refs/host.wav", ref_text="hello there",
        instruct="cheerful", speed=1.1, language="vi",
    ))
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="cloned-host")))

    url = "/v1/livehost/stream?stt_engine=stub-livehost-ttsp&profile=host&sample_rate=16000"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    assert stub_tts.calls, "TTS provider was never invoked"
    payload = stub_tts.calls[0]
    assert payload.ref_audio_path == "artifacts/refs/host.wav"
    assert payload.ref_text == "hello there"
    assert payload.instruct == "cheerful"
    assert payload.speed == 1.1
    assert payload.language == "vi"


def test_livehost_query_param_tts_profile_overrides_llm_profile(client, _hermetic):
    stub_tts, profiles, tts_profiles = _hermetic
    tts_profiles.upsert(TtsProfile(name="from-llm-profile", engine="stub-livehost-ttsp-tts", voice="v1"))
    tts_profiles.upsert(TtsProfile(
        name="pinned", engine="stub-livehost-ttsp-tts", voice_mode="clone",
        ref_audio_path="ref.wav", ref_text="pinned voice",
    ))
    profiles.upsert(Profile(name="host", tts=TtsConfig(profile_name="from-llm-profile")))

    url = (
        "/v1/livehost/stream?stt_engine=stub-livehost-ttsp&profile=host"
        "&tts_profile=pinned&sample_rate=16000"
    )
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    payload = stub_tts.calls[0]
    assert payload.ref_audio_path == "ref.wav"
    assert payload.ref_text == "pinned voice"


def test_livehost_no_tts_profile_falls_back_to_legacy_query_params(client, _hermetic):
    stub_tts, _profiles, _tts_profiles = _hermetic
    url = (
        "/v1/livehost/stream?stt_engine=stub-livehost-ttsp"
        "&tts_engine=stub-livehost-ttsp-tts&voice=manual-voice&sample_rate=16000"
    )
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        _run_one_turn(ws)

    payload = stub_tts.calls[0]
    assert payload.engine == "stub-livehost-ttsp-tts"
    assert payload.voice == "manual-voice"
    assert payload.ref_audio_path is None
    assert payload.instruct is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_livehost_tts_profile.py -v`
Expected: FAIL, for the same reason as Task 5 Step 2: `test_livehost_tts_profile_linked_from_llm_profile` and `test_livehost_query_param_tts_profile_overrides_llm_profile` set `profile=host`, and the still-unmodified `profile.tts.engine` access raises `AttributeError` before `session_started` is sent, so both fail at that first `assert`. `test_livehost_no_tts_profile_falls_back_to_legacy_query_params` sets no `profile=`, so it already passes.

- [ ] **Step 3: Wire up resolution and TTSRequest construction**

In `apps/api_gateway/app/api/routes/livehost.py`, add the import next to the profile-store import:

```python
from app.services.profiles.store import profile_store
from app.services.tts.profile_store import tts_profile_store
```

Replace the resolution block:

```python
    if profile and profile.tts.engine:
        tts_engine = profile.tts.engine
        voice = profile.tts.voice or q.get("voice") or None
    else:
        tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
        voice = q.get("voice") or None
```

with:

```python
    # TTS profile resolution: ?tts_profile= (explicit pin) > the active LLM
    # profile's linked TTS profile > legacy tts_engine/voice query params.
    tts_profile_name = q.get("tts_profile") or (profile.tts.profile_name if profile else "") or None
    tts_profile = tts_profile_store.get(tts_profile_name) if tts_profile_name else None
    if tts_profile and tts_profile.engine:
        tts_engine = tts_profile.engine
        voice = tts_profile.voice or q.get("voice") or None
        ref_audio_path = tts_profile.ref_audio_path or None
        ref_text = tts_profile.ref_text or None
        tts_instruct = tts_profile.instruct or None
        tts_speed = tts_profile.speed
        tts_language = tts_profile.language
    else:
        tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
        voice = q.get("voice") or None
        ref_audio_path = ref_text = tts_instruct = None
        tts_speed = tts_language = None
```

Replace the `TTSRequest` build inside `_stream_to_tts`'s `_synth`:

```python
            async def _synth(sentence: str):
                result = await tts_provider.synthesize(TTSRequest(text=sentence, engine=tts_engine, voice=voice))
```

with:

```python
            async def _synth(sentence: str):
                result = await tts_provider.synthesize(
                    TTSRequest(
                        text=sentence, engine=tts_engine, voice=voice,
                        ref_audio_path=ref_audio_path, ref_text=ref_text,
                        instruct=tts_instruct, speed=tts_speed, language=tts_language,
                    )
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_livehost_tts_profile.py -v`
Expected: 3 passed

- [ ] **Step 5: Run the full existing livehost test suite to check for regressions**

Run: `pytest tests/integration/test_livehost_ws_voice.py tests/integration/test_livehost_ws_social.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/livehost.py tests/unit/test_livehost_tts_profile.py
git commit -m "feat(livehost): resolve TTS profile (clone/instruct/speed/language) per turn"
```

---

### Task 7: UI — TTS Profiles management panel + LLM Profile panel switches to a dropdown

**Files:**
- Create: `apps/api_gateway/app/static/js/tts-profiles.js`
- Modify: `apps/api_gateway/app/static/index.html:170-181` (LLM profile panel TTS fields), `apps/api_gateway/app/static/index.html:428` (insert new card before closing `section-tts` div)
- Modify: `apps/api_gateway/app/static/js/profiles.js` (imports, `openProfilePanel`, remove `pfUpdateTtsVoice`, `saveProfile` payload, listener wiring, add `renderProfileTtsSelect`)
- Modify: `apps/api_gateway/app/static/js/tts-engines.js` (drop `pf-tts-engine` wiring — that select no longer exists)
- Modify: `apps/api_gateway/app/static/js/main.js` (import + call `loadTtsProfiles`)

**Interfaces:**
- Consumes: `GET/POST/PUT/DELETE /v1/tts/profiles` (Task 3), `GET /v1/tts/engines`, `GET /v1/tts/voices?engine=` (existing).
- Produces: `export let ttsProfileData` and `export async function loadTtsProfiles()` from `tts-profiles.js`, consumed by `profiles.js`'s new `renderProfileTtsSelect()`.

This task has no pytest coverage (pure front-end); verify with `node --check` on every touched/created `.js` file, then a manual pass in the browser per this project's UI-change convention.

- [ ] **Step 1: Replace the LLM Profile panel's TTS Engine/Voice fields with a TTS Profile dropdown**

In `apps/api_gateway/app/static/index.html`, replace:

```html
                  <label>
                    TTS Engine
                    <select id="pf-tts-engine">
                      <option value="">(inherit global)</option>
                    </select>
                  </label>
                  <label id="pf-tts-voice-wrap" class="hidden">
                    TTS Voice
                    <select id="pf-tts-voice">
                      <option value="">(auto)</option>
                    </select>
                  </label>
```

with:

```html
                  <label>
                    TTS Profile
                    <select id="pf-tts-profile">
                      <option value="">(inherit global)</option>
                    </select>
                  </label>
```

- [ ] **Step 2: Add the "TTS Profiles" management card**

In `apps/api_gateway/app/static/index.html`, insert this new `<section class="card">` right before the closing `</div>` of `section-tts` (i.e. right after the "TTS Stream" card's closing `</section>`, before line `</div>` that ends `id="section-tts"`):

```html
            <section class="card">
              <h2>TTS Profiles</h2>
              <p class="hint">Bundle an engine, a voice (preset or cloned), and style/speed/language into a reusable named profile. Reference it from an LLM Profile's "TTS Profile" field, or pin one per-session with <code>?tts_profile=&lt;name&gt;</code>.</p>
              <div id="tts-profile-list" class="model-list"></div>

              <h3 class="sub" id="tp-form-title">New TTS Profile</h3>
              <div class="row tight">
                <label>
                  Name <span class="hint" style="display:inline;margin:0">(slug, no spaces)</span>
                  <input id="tp-name" type="text" placeholder="cohost-voice" />
                </label>
                <label>
                  Engine
                  <select id="tp-engine"></select>
                </label>
              </div>
              <div class="row tight">
                <label class="check">
                  <input type="radio" name="tp-voice-mode" id="tp-mode-preset" value="preset" checked /> Preset voice
                </label>
                <label class="check">
                  <input type="radio" name="tp-voice-mode" id="tp-mode-clone" value="clone" /> Clone from reference audio
                </label>
              </div>
              <label id="tp-preset-wrap">
                Voice
                <select id="tp-voice"><option value="">(auto)</option></select>
              </label>
              <div id="tp-clone-wrap" class="hidden">
                <label>
                  Reference audio path <span class="hint" style="display:inline;margin:0">(server-side path)</span>
                  <input id="tp-ref-audio" type="text" placeholder="artifacts/refs/host.wav" />
                </label>
                <label>
                  Reference transcript
                  <textarea id="tp-ref-text" rows="2" placeholder="Exact words spoken in the reference audio…"></textarea>
                </label>
              </div>
              <div class="row tight">
                <label>
                  Instruct <span class="hint" style="display:inline;margin:0">(style/emotion, engine-dependent)</span>
                  <input id="tp-instruct" type="text" placeholder="cheerful, fast-paced…" />
                </label>
                <label>
                  Speed
                  <input id="tp-speed" type="number" step="0.1" placeholder="1.0" />
                </label>
                <label>
                  Language
                  <input id="tp-language" type="text" placeholder="vi" />
                </label>
              </div>
              <div class="actions">
                <button id="tp-save-btn">Save Profile</button>
                <button id="tp-cancel-btn" class="ghost">Cancel</button>
                <button id="tp-delete-btn" class="danger hidden">Delete</button>
              </div>
              <p id="tp-status" class="meta"></p>
            </section>
```

- [ ] **Step 3: Create `tts-profiles.js`**

Create `apps/api_gateway/app/static/js/tts-profiles.js`:

```js
import { el, print } from "./helpers.js";
import { renderProfileTtsSelect } from "./profiles.js";

export let ttsProfileData = {};
export let ttsProfileEditName = null; // null = "new" (no profile currently loaded into the form)

export async function loadTtsProfiles() {
  try {
    const body = await (await fetch("/v1/tts/profiles")).json();
    ttsProfileData = body.data || {};
    renderTtsProfileList();
    renderProfileTtsSelect();
  } catch {
    /* ignore */
  }
}

export function renderTtsProfileList() {
  const host = el("tts-profile-list");
  if (!host) return;
  const names = Object.keys(ttsProfileData).sort();
  if (!names.length) {
    host.innerHTML = '<p class="hint">No TTS profiles yet. Create one below.</p>';
    return;
  }
  host.innerHTML = names.map((name) => {
    const p = ttsProfileData[name];
    const voiceSummary = p.voice_mode === "clone" ? "cloned voice" : (p.voice || "auto voice");
    return `
      <div class="model-row">
        <div class="model-info">
          <strong>${name}</strong>
          <code>${p.engine || "(no engine)"}</code>
          <span class="hint">${voiceSummary}</span>
        </div>
        <div class="model-action">
          <button class="mini" data-tp-edit="${name}">Edit</button>
          <button class="mini danger" data-tp-delete="${name}">Delete</button>
        </div>
      </div>
    `;
  }).join("");

  document.querySelectorAll("[data-tp-edit]").forEach((btn) =>
    btn.addEventListener("click", () => openTtsProfileForm(btn.getAttribute("data-tp-edit")))
  );
  document.querySelectorAll("[data-tp-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteTtsProfile(btn.getAttribute("data-tp-delete")))
  );
}

export function toggleTtsVoiceMode() {
  const isClone = el("tp-mode-clone")?.checked;
  const presetWrap = el("tp-preset-wrap");
  const cloneWrap = el("tp-clone-wrap");
  if (presetWrap) presetWrap.classList.toggle("hidden", !!isClone);
  if (cloneWrap) cloneWrap.classList.toggle("hidden", !isClone);
}

export async function loadTtsProfileVoiceOptions(engine) {
  const sel = el("tp-voice");
  if (!sel) return;
  sel.innerHTML = '<option value="">(auto)</option>';
  if (!engine) return;
  try {
    const body = await (await fetch(`/v1/tts/voices?engine=${encodeURIComponent(engine)}`)).json();
    (body.data || []).forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.voice;
      opt.textContent = v.label;
      sel.appendChild(opt);
    });
  } catch {
    /* voices optional */
  }
}

export function openTtsProfileForm(name) {
  ttsProfileEditName = name || null;
  el("tp-form-title").textContent = name ? `Edit "${name}"` : "New TTS Profile";
  const p = name ? ttsProfileData[name] : null;

  el("tp-name").value = name || "";
  el("tp-name").disabled = !!name;
  el("tp-engine").value = p?.engine || "";
  const isClone = p?.voice_mode === "clone";
  el("tp-mode-preset").checked = !isClone;
  el("tp-mode-clone").checked = isClone;
  toggleTtsVoiceMode();
  loadTtsProfileVoiceOptions(p?.engine || "").then(() => {
    if (p?.voice) el("tp-voice").value = p.voice;
  });
  el("tp-ref-audio").value = p?.ref_audio_path || "";
  el("tp-ref-text").value = p?.ref_text || "";
  el("tp-instruct").value = p?.instruct || "";
  el("tp-speed").value = p?.speed ?? "";
  el("tp-language").value = p?.language || "";
  el("tp-delete-btn").classList.toggle("hidden", !name);
  el("tp-status").textContent = "";
}

export function resetTtsProfileForm() {
  openTtsProfileForm(null);
}

export async function saveTtsProfile() {
  const name = el("tp-name").value.trim();
  if (!name) { print(el("tp-status"), "Enter a profile name", true); return; }

  const speedRaw = el("tp-speed").value.trim();
  const payload = {
    name,
    engine: el("tp-engine").value || "",
    voice_mode: el("tp-mode-clone").checked ? "clone" : "preset",
    voice: el("tp-voice").value || "",
    ref_audio_path: el("tp-ref-audio").value.trim(),
    ref_text: el("tp-ref-text").value.trim(),
    instruct: el("tp-instruct").value.trim(),
    speed: speedRaw ? parseFloat(speedRaw) : null,
    language: el("tp-language").value.trim() || null,
  };

  print(el("tp-status"), "Saving…");
  try {
    const isNew = !ttsProfileEditName;
    const url = isNew ? "/v1/tts/profiles" : `/v1/tts/profiles/${encodeURIComponent(ttsProfileEditName)}`;
    const resp = await fetch(url, {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(el("tp-status"), body.detail || body.error || JSON.stringify(body), true);
      return;
    }
    el("tp-status").textContent = "Saved ✓";
    await loadTtsProfiles();
    openTtsProfileForm(name);
  } catch (error) {
    print(el("tp-status"), String(error), true);
  }
}

export async function deleteTtsProfile(name) {
  if (!confirm(`Delete TTS profile "${name}"?`)) return;
  try {
    const resp = await fetch(`/v1/tts/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) { const b = await resp.json(); print(el("tp-status"), b.detail || "Delete failed", true); return; }
    await loadTtsProfiles();
    if (ttsProfileEditName === name) resetTtsProfileForm();
  } catch (error) {
    print(el("tp-status"), String(error), true);
  }
}

if (el("tp-engine")) {
  el("tp-engine").addEventListener("change", (e) => loadTtsProfileVoiceOptions(e.target.value));
}
if (el("tp-mode-preset")) el("tp-mode-preset").addEventListener("change", toggleTtsVoiceMode);
if (el("tp-mode-clone")) el("tp-mode-clone").addEventListener("change", toggleTtsVoiceMode);
if (el("tp-save-btn")) el("tp-save-btn").addEventListener("click", saveTtsProfile);
if (el("tp-cancel-btn")) el("tp-cancel-btn").addEventListener("click", resetTtsProfileForm);
if (el("tp-delete-btn")) el("tp-delete-btn").addEventListener("click", () => deleteTtsProfile(ttsProfileEditName));
```

- [ ] **Step 4: Populate the `tp-engine` select from `/v1/tts/engines`**

The `tp-engine` select needs the same engine list as `pf-tts-engine` used to get from `tts-engines.js`'s `loadTtsEngines()`. Add `"tp-engine"` to that function's target-select list so it gets populated the same way as the others.

In `apps/api_gateway/app/static/js/tts-engines.js`, in `loadTtsEngines()`, change:

```js
    [["tts-engine", "tts-engine-detail"], ["tts-stream-engine", "tts-stream-engine-detail"], ["t2v-tts-engine", "t2v-engine-detail"], ["pf-tts-engine", null]].forEach(
```

to:

```js
    [["tts-engine", "tts-engine-detail"], ["tts-stream-engine", "tts-stream-engine-detail"], ["t2v-tts-engine", "t2v-engine-detail"], ["tp-engine", null]].forEach(
```

(this drops the now-deleted `pf-tts-engine` select and adds the new `tp-engine` select to the same auto-populate loop).

Then remove the now-dead `pf-tts-engine` branch inside `updateTtsEngine()` — delete this block entirely:

```js
  if (selId === "pf-tts-engine") {
    const isVieneu = engine === "vieneu";
    const wrap = el("pf-tts-voice-wrap");
    if (wrap) wrap.classList.toggle("hidden", !isVieneu);
    if (isVieneu) {
      fetch("/v1/tts/voices?engine=vieneu").then(r => r.json()).then(b => {
        const sel = el("pf-tts-voice");
        if (!sel) return;
        sel.innerHTML = '<option value="">(auto)</option>';
        b.data.forEach(v => { const o = document.createElement("option"); o.value = v.voice; o.textContent = v.label; sel.appendChild(o); });
      }).catch(() => {});
    }
  }
```

Note: `loadTtsEngines()`'s per-select loop calls `updateTtsEngine(selId, detId)` for `tp-engine` too, which will just set `.textContent` on a `null` detail element (guarded by `if (det)`) and fall through the `if (selId === "tts-engine")` / `"t2v-tts-engine"` branches harmlessly — no special-case needed for `tp-engine` there since the TTS Profile form manages its own voice dropdown via `tts-profiles.js`'s `loadTtsProfileVoiceOptions`.

- [ ] **Step 5: Update `profiles.js` to use the TTS Profile dropdown instead of engine/voice inputs**

In `apps/api_gateway/app/static/js/profiles.js`:

Change the import block at the top:

```js
import { el, print } from "./helpers.js";
import { mcpServerData } from "./mcp-servers.js";
import { setCurrentSessionId } from "./chat.js";
```

to:

```js
import { el, print } from "./helpers.js";
import { mcpServerData } from "./mcp-servers.js";
import { ttsProfileData } from "./tts-profiles.js";
import { setCurrentSessionId } from "./chat.js";
```

Add a new exported function (place it right after `renderProfileSelect`):

```js
export function renderProfileTtsSelect() {
  const sel = el("pf-tts-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">(inherit global)</option>';
  Object.keys(ttsProfileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (ttsProfileData[prev]) sel.value = prev;
}
```

In `openProfilePanel`, replace:

```js
  let selectedMcpServers = [];

  if (mode === "new") {
    el("pf-name").value = "";
    el("pf-name").disabled = false;
    el("pf-nickname").value = "";
    el("pf-system-prompt").value = "";
    el("pf-llm-url").value = "";
    el("pf-llm-model").value = "";
    el("pf-llm-key").value = "";
    if (el("pf-tts-engine")) el("pf-tts-engine").value = "";
    el("pf-delete-btn").classList.add("hidden");
    el("pf-mem-enabled").checked = true;
    el("pf-mem-mode").value = "all";
    el("pf-mem-list").innerHTML = "";
  } else {
    const p = profileData[name];
    if (!p) return;
    el("pf-name").value = name;
    el("pf-name").disabled = true;
    el("pf-nickname").value = p.nickname || "";
    el("pf-system-prompt").value = p.system_prompt || "";
    el("pf-llm-url").value = p.llm?.base_url || "";
    el("pf-llm-model").value = p.llm?.model || "";
    el("pf-llm-key").value = "";
    if (el("pf-tts-engine")) el("pf-tts-engine").value = p.tts?.engine || "";
    el("pf-delete-btn").classList.remove("hidden");
    selectedMcpServers = p.mcp_servers || [];
    el("pf-mem-enabled").checked = p.memory?.enabled ?? true;
    el("pf-mem-mode").value = p.memory?.mode || "all";
    loadMemories(name);
  }

  el("pf-status").textContent = "";
  panel.classList.remove("hidden");
  pfUpdateTtsVoice();
  renderProfileMcpList(selectedMcpServers);
}
```

with:

```js
  let selectedMcpServers = [];
  renderProfileTtsSelect();

  if (mode === "new") {
    el("pf-name").value = "";
    el("pf-name").disabled = false;
    el("pf-nickname").value = "";
    el("pf-system-prompt").value = "";
    el("pf-llm-url").value = "";
    el("pf-llm-model").value = "";
    el("pf-llm-key").value = "";
    if (el("pf-tts-profile")) el("pf-tts-profile").value = "";
    el("pf-delete-btn").classList.add("hidden");
    el("pf-mem-enabled").checked = true;
    el("pf-mem-mode").value = "all";
    el("pf-mem-list").innerHTML = "";
  } else {
    const p = profileData[name];
    if (!p) return;
    el("pf-name").value = name;
    el("pf-name").disabled = true;
    el("pf-nickname").value = p.nickname || "";
    el("pf-system-prompt").value = p.system_prompt || "";
    el("pf-llm-url").value = p.llm?.base_url || "";
    el("pf-llm-model").value = p.llm?.model || "";
    el("pf-llm-key").value = "";
    if (el("pf-tts-profile")) el("pf-tts-profile").value = p.tts?.profile_name || "";
    el("pf-delete-btn").classList.remove("hidden");
    selectedMcpServers = p.mcp_servers || [];
    el("pf-mem-enabled").checked = p.memory?.enabled ?? true;
    el("pf-mem-mode").value = p.memory?.mode || "all";
    loadMemories(name);
  }

  el("pf-status").textContent = "";
  panel.classList.remove("hidden");
  renderProfileMcpList(selectedMcpServers);
}
```

Delete the now-unused `pfUpdateTtsVoice` function entirely:

```js
export function pfUpdateTtsVoice() {
  const eng = el("pf-tts-engine");
  const wrap = el("pf-tts-voice-wrap");
  if (!eng || !wrap) return;
  wrap.classList.toggle("hidden", eng.value !== "vieneu");
}
```

In `saveProfile`, replace:

```js
    tts: {
      engine: el("pf-tts-engine")?.value || "",
      voice: el("pf-tts-voice")?.value || "",
    },
```

with:

```js
    tts: {
      profile_name: el("pf-tts-profile")?.value || "",
    },
```

At the bottom, remove the now-dead listener line:

```js
if (el("pf-tts-engine")) el("pf-tts-engine").addEventListener("change", pfUpdateTtsVoice);
```

(delete it entirely — there's no per-engine voice toggle in the LLM profile panel anymore).

- [ ] **Step 6: Wire `tts-profiles.js` into the app bootstrap**

In `apps/api_gateway/app/static/js/main.js`, add the import next to the other `loadX` imports:

```js
import { loadProfiles } from "./profiles.js";
import { loadTtsProfiles } from "./tts-profiles.js";
import { loadMcpServers } from "./mcp-servers.js";
```

and call it next to the other eager loads at the bottom:

```js
loadProfiles();
loadTtsProfiles();
loadMcpServers();
```

- [ ] **Step 7: Syntax-check every touched/created JS file**

Run:
```bash
node --check apps/api_gateway/app/static/js/tts-profiles.js
node --check apps/api_gateway/app/static/js/profiles.js
node --check apps/api_gateway/app/static/js/tts-engines.js
node --check apps/api_gateway/app/static/js/main.js
```
Expected: no output, exit code 0 for each.

- [ ] **Step 8: Run the full backend test suite once more (index.html/js changes shouldn't affect it, but confirm nothing else broke)**

Run: `pytest tests/ -q`
Expected: all pass

- [ ] **Step 9: Manual verification in a browser**

Run: `make dev` (starts uvicorn with --reload)

In the browser:
1. Go to the "TTS" sidebar section → confirm the new "TTS Profiles" card renders (empty state: "No TTS profiles yet.").
2. Create a preset-voice profile: pick an available engine, leave "Preset voice" selected, optionally pick a voice, Save. Confirm it appears in the list.
3. Create a clone profile: select "Clone from reference audio", fill in a `ref_audio_path` and `ref_text`, Save. Confirm it appears in the list with "cloned voice" summary.
4. Click "Edit" on each, confirm the form repopulates correctly (including the preset/clone radio state and voice dropdown).
5. Go to the Chat section → "+ New" profile → confirm the "TTS Profile" dropdown lists both profiles created above, select one, Save, confirm `GET /v1/profiles/<name>` reflects `tts.profile_name`.
6. Delete a TTS profile from the TTS section; confirm it disappears from the LLM Profile panel's dropdown next time it's opened.

- [ ] **Step 10: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/tts-profiles.js apps/api_gateway/app/static/js/profiles.js apps/api_gateway/app/static/js/tts-engines.js apps/api_gateway/app/static/js/main.js
git commit -m "feat(ui): TTS Profiles management panel; LLM Profile panel picks a TTS profile by name"
```

---

## Self-Review Notes

- **Spec coverage:** §1 Data Model/Storage → Task 1–2. §2 API → Task 3. §3 Resolution → Tasks 5–6 (both conversation.py and livehost.py covered). §4 UI → Task 7 (both the dropdown replacement and the new management panel). §5 Tests → each task carries its own tests; Task 4 explicitly updates the three existing profile test files the spec called out. §6 Out of scope confirmed not implemented (no audio upload UI, no migration code, no per-engine field validation).
- **Type consistency:** `tts_engine`, `voice`, `ref_audio_path`, `ref_text`, `tts_instruct`, `tts_speed`, `tts_language` are the exact names used in both Task 5 and Task 6's resolution blocks and their `TTSRequest(...)` calls — checked they match `TTSRequest`'s actual field names (`engine`, `voice`, `ref_audio_path`, `ref_text`, `instruct`, `speed`, `language` — `instruct`/`speed`/`language` are aliased through the differently-named locals to avoid shadowing the `Settings.conversation_...` pattern, but the keyword arguments passed to `TTSRequest(...)` use the schema's real field names).
- **No placeholders:** every step has complete, runnable code; no "add appropriate X" phrasing.
