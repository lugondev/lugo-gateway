# Profile Pre-Flight Health Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject a WebSocket session up front — before the user speaks — when the profile's resolved STT or TTS engine is genuinely unavailable, instead of surfacing "All connection attempts failed" mid-conversation.

**Architecture:** A new `probe_service_health()` helper does a real `GET {base}/health` against `http_stt`/`http_tts` registry entries. `STTService.check_engine()` / `TTSService.check_engine()` wrap that plus each engine's existing config-check to produce a 3-state `EngineHealth`. A `check_profile_health()` service resolves a profile's engines the same way the WS routes do and runs both checks concurrently via `asyncio.gather`. Both WS routes gate on it; a new HTTP endpoint exposes it to the admin UI.

**Tech Stack:** Python 3.12, FastAPI, httpx (`MockTransport` for tests), pydantic v2, pytest + pytest-asyncio.

## Global Constraints

- Live network probes are added **only** for `http_stt` and `http_tts`. Every other engine (local in-process, and cloud/API-key engines `qwencloud`/`whisper_or`/`qwen3_asr_or`) keeps its existing config-only check — no new live API calls, to avoid burning quota/cost on every session start.
- No caching of health results — every session start does a fresh probe.
- Probe timeout is `3.0` seconds.
- Status is one of exactly three string literals: `"ok"`, `"not_ready"`, `"unavailable"`.
- Only `"unavailable"` blocks a session. `"not_ready"` (a local engine still warming) never blocks.
- `probe_service_health()` treats **any** HTTP response — including 404 and 401 — as reachable. Only connection-level failures mean unreachable.
- Run tests with the repo venv: `.venv/bin/pytest`. Scope test runs to this repo only.
- Commit as `lugondev <lugondev@gmail.com>` (repo default; do not override).

**Spec:** `docs/superpowers/specs/2026-07-28-profile-health-check-design.md`

---

## File Structure

**Create:**
- `apps/api_gateway/app/services/model_registry/health_probe.py` — the network reachability probe. One responsibility: "is a process listening at this base_url".
- `apps/api_gateway/app/schemas/health.py` — `EngineHealth` / `ProfileHealth` pydantic models.
- `apps/api_gateway/app/services/health.py` — `check_profile_health()`: profile → resolved engines → concurrent checks.
- `tests/unit/test_health_probe.py`
- `tests/unit/test_engine_health_check.py`
- `tests/unit/test_profile_health.py`
- `tests/unit/test_session_health_gate.py`

**Modify:**
- `apps/api_gateway/app/services/tts/providers/http_tts_provider.py` — add missing `available()` override.
- `apps/api_gateway/app/services/stt/service.py` — add `check_engine()`.
- `apps/api_gateway/app/services/tts/service.py` — add `check_engine()`.
- `apps/api_gateway/app/api/routes/profiles.py` — add `GET /v1/profiles/{name}/health`.
- `apps/api_gateway/app/api/routes/conversation.py:346-352` — extend the existing gate.
- `apps/api_gateway/app/api/routes/lugo.py` — add the equivalent gate before `session.start()`.

---

### Task 1: Health probe helper

**Files:**
- Create: `apps/api_gateway/app/services/model_registry/health_probe.py`
- Test: `tests/unit/test_health_probe.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `async def probe_service_health(base_url: str, api_key: str, timeout: float = 3.0) -> tuple[bool, str | None]`. Returns `(True, None)` when reachable, `(False, "<reason>")` when not.

**Background:** `base_url` values in the registry look like `http://127.0.0.1:8100/v1`. `apps/model_service` serves `/health` at the root, not under `/v1` (see `apps/model_service/app/main.py:50`), so the `/v1` suffix must be stripped before appending `/health`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_health_probe.py`:

```python
import httpx
import pytest

from app.services.model_registry.health_probe import probe_service_health


@pytest.fixture
def mock_transport(monkeypatch):
    """Install a handler as httpx's transport; returns a dict capturing the request."""
    seen = {}

    def install(handler):
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return seen

    return install


@pytest.mark.asyncio
async def test_strips_v1_suffix_and_hits_health(mock_transport):
    seen = mock_transport(lambda req: (
        seen.__setitem__("url", str(req.url)),
        httpx.Response(200, json={"status": "ok"}),
    )[1])
    ok, reason = await probe_service_health("http://127.0.0.1:8100/v1", "tok")
    assert ok is True
    assert reason is None
    assert seen["url"] == "http://127.0.0.1:8100/health"


@pytest.mark.asyncio
async def test_sends_bearer_token_when_api_key_present(mock_transport):
    captured = {}

    def handler(req):
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200)

    mock_transport(handler)
    await probe_service_health("http://host:8100/v1", "s3cret")
    assert captured["auth"] == "Bearer s3cret"


@pytest.mark.asyncio
async def test_no_auth_header_when_api_key_blank(mock_transport):
    captured = {}

    def handler(req):
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200)

    mock_transport(handler)
    await probe_service_health("http://host:8100/v1", "")
    assert captured["auth"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 404, 500])
async def test_any_http_response_counts_as_reachable(mock_transport, status):
    """A process that answers at all is up -- even if it has no /health route
    or rejects our token. We are checking liveness, not the route contract."""
    mock_transport(lambda req: httpx.Response(status))
    ok, reason = await probe_service_health("http://host:8100/v1", "tok")
    assert ok is True
    assert reason is None


@pytest.mark.asyncio
async def test_connect_error_is_unreachable(mock_transport):
    def handler(req):
        raise httpx.ConnectError("All connection attempts failed")

    mock_transport(handler)
    ok, reason = await probe_service_health("http://host:8100/v1", "tok")
    assert ok is False
    assert "All connection attempts failed" in reason


@pytest.mark.asyncio
async def test_timeout_is_unreachable(mock_transport):
    def handler(req):
        raise httpx.ConnectTimeout("timed out")

    mock_transport(handler)
    ok, reason = await probe_service_health("http://host:8100/v1", "tok")
    assert ok is False
    assert "timed out" in reason


@pytest.mark.asyncio
async def test_blank_base_url_is_unreachable_without_calling(mock_transport):
    called = {"n": 0}

    def handler(req):
        called["n"] += 1
        return httpx.Response(200)

    mock_transport(handler)
    ok, reason = await probe_service_health("  ", "tok")
    assert ok is False
    assert reason == "no base_url configured"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_passes_timeout_to_client(mock_transport):
    seen = mock_transport(lambda req: httpx.Response(200))
    await probe_service_health("http://host:8100/v1", "tok", timeout=1.5)
    assert seen["timeout"] == 1.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_health_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.model_registry.health_probe'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/api_gateway/app/services/model_registry/health_probe.py`:

```python
"""Liveness probe for self-hosted OpenAI-compatible speech services.

Answers one question: is a process actually listening at this base_url? That
is deliberately weaker than "does it implement /health correctly" -- a 404 or
401 still proves something is alive and answering, and the failure this exists
to catch is the model_service process being down entirely, which surfaces as a
connection-level error rather than an HTTP status.
"""

from __future__ import annotations

import httpx

DEFAULT_PROBE_TIMEOUT = 3.0


def _health_url(base_url: str) -> str:
    """apps/model_service serves /health at the root, while registry base_urls
    point at the OpenAI-compatible /v1 prefix -- strip it before appending."""
    trimmed = base_url.strip().rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return f"{trimmed}/health"


async def probe_service_health(
    base_url: str, api_key: str, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> tuple[bool, str | None]:
    """(reachable, reason). reason is None when reachable."""
    if not base_url.strip():
        return False, "no base_url configured"

    headers = {"Authorization": f"Bearer {api_key.strip()}"} if api_key.strip() else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.get(_health_url(base_url), headers=headers)
    except httpx.HTTPError as exc:
        return False, str(exc) or type(exc).__name__
    return True, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_health_probe.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/health_probe.py tests/unit/test_health_probe.py
git commit -m "feat(health): add service liveness probe for self-hosted speech engines"
```

---

### Task 2: Fix `HttpTtsProvider.available()`

**Files:**
- Modify: `apps/api_gateway/app/services/tts/providers/http_tts_provider.py`
- Test: `tests/unit/test_http_tts_provider.py` (append)

**Interfaces:**
- Consumes: `model_registry_store.find_enabled_sync(kind, engine)` (existing, `store.py:157`).
- Produces: `HttpTtsProvider.available() -> bool`.

**Background:** `TTSProvider.available()` (`tts/base.py:24`) hardcodes `return True` and `HttpTtsProvider` never overrode it, so `GET /v1/tts/engines` has always reported `http_tts` as available even with no registry row. `TTSService.list_engines()` is sync (`tts/service.py:32`), so this override must be sync too — hence `find_enabled_sync`, not the async `find_enabled`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_http_tts_provider.py`:

```python
def test_available_false_when_no_enabled_entry(monkeypatch):
    """Regression: this inherited TTSProvider.available()'s hardcoded True,
    so the admin dashboard reported http_tts usable with zero registry rows."""
    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find_enabled_sync",
        lambda kind, engine=None: None,
    )
    assert HttpTtsProvider().available() is False


def test_available_true_when_enabled_entry_has_base_url(monkeypatch):
    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find_enabled_sync",
        lambda kind, engine=None: dict(_ENTRY),
    )
    assert HttpTtsProvider().available() is True


def test_available_false_when_entry_has_blank_base_url(monkeypatch):
    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find_enabled_sync",
        lambda kind, engine=None: {**_ENTRY, "base_url": "  "},
    )
    assert HttpTtsProvider().available() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_http_tts_provider.py -v -k available`
Expected: FAIL — `test_available_false_when_no_enabled_entry` asserts `False` but gets `True`.

- [ ] **Step 3: Write minimal implementation**

In `apps/api_gateway/app/services/tts/providers/http_tts_provider.py`, add this method to `HttpTtsProvider` immediately after `detail()` (currently line 79-80):

```python
    def available(self) -> bool:
        """Configured = some enabled registry row carries a base_url. Mirrors
        what STTService.list_engines() already computes for http_stt; sync
        because TTSService.list_engines() is sync."""
        row = model_registry_store.find_enabled_sync("tts", self.name)
        return bool((row or {}).get("base_url", "").strip())
```

`model_registry_store` is already imported at the top of the file (line 22) — no new import needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_http_tts_provider.py -v`
Expected: PASS (all existing tests plus the 3 new ones)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/tts/providers/http_tts_provider.py tests/unit/test_http_tts_provider.py
git commit -m "fix(tts): http_tts reported available with no registry row"
```

---

### Task 3: `EngineHealth` / `ProfileHealth` schemas

**Files:**
- Create: `apps/api_gateway/app/schemas/health.py`
- Test: `tests/unit/test_engine_health_check.py` (created here, extended in Task 4)

**Interfaces:**
- Consumes: nothing.
- Produces: `EngineHealth(engine: str, status: EngineStatus, detail: str = "")` and `ProfileHealth(profile: str, stt: EngineHealth, tts: EngineHealth)`, plus `EngineStatus = Literal["ok", "not_ready", "unavailable"]` and `EngineHealth.blocks_session` (property → `bool`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_engine_health_check.py`:

```python
from app.schemas.health import EngineHealth, ProfileHealth


def test_unavailable_blocks_session():
    assert EngineHealth(engine="http_stt", status="unavailable", detail="down").blocks_session is True


def test_ok_does_not_block():
    assert EngineHealth(engine="vosk", status="ok").blocks_session is False


def test_not_ready_does_not_block():
    """A local engine still loading its model is not a failure -- session_started
    already reports stt_ready/tts_ready for this case."""
    assert EngineHealth(engine="whisper", status="not_ready").blocks_session is False


def test_detail_defaults_to_empty_string():
    assert EngineHealth(engine="vosk", status="ok").detail == ""


def test_profile_health_serializes_nested_engines():
    payload = ProfileHealth(
        profile="default",
        stt=EngineHealth(engine="http_stt", status="unavailable", detail="unreachable"),
        tts=EngineHealth(engine="vieneu", status="ok"),
    ).model_dump()
    assert payload["profile"] == "default"
    assert payload["stt"]["status"] == "unavailable"
    assert payload["stt"]["detail"] == "unreachable"
    assert payload["tts"]["engine"] == "vieneu"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_engine_health_check.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.health'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/api_gateway/app/schemas/health.py`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# "not_ready" is a local engine still running warm() -- distinct from
# "unavailable" (misconfigured, or a remote host that isn't answering) because
# only the latter is worth refusing a session over.
EngineStatus = Literal["ok", "not_ready", "unavailable"]


class EngineHealth(BaseModel):
    engine: str
    status: EngineStatus
    detail: str = ""

    @property
    def blocks_session(self) -> bool:
        return self.status == "unavailable"


class ProfileHealth(BaseModel):
    profile: str
    stt: EngineHealth
    tts: EngineHealth
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_engine_health_check.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/schemas/health.py tests/unit/test_engine_health_check.py
git commit -m "feat(health): add EngineHealth/ProfileHealth schemas"
```

---

### Task 4: `check_engine()` on both services

**Files:**
- Modify: `apps/api_gateway/app/services/stt/service.py`
- Modify: `apps/api_gateway/app/services/tts/service.py`
- Test: `tests/unit/test_engine_health_check.py` (append)

**Interfaces:**
- Consumes: `probe_service_health()` (Task 1), `EngineHealth` (Task 3), `model_registry_store.find` / `find_enabled` (existing), `app.services.warmup.is_ready` / `_needs_warming` (existing, `warmup.py:70,77`), `HttpTtsProvider.available()` (Task 2).
- Produces: `async STTService.check_engine(engine: str, model: str = "") -> EngineHealth` and `async TTSService.check_engine(engine: str, model_id: str = "") -> EngineHealth`.

**Background:** `STTService.list_engines()` (`stt/service.py:98`) is async and already computes a per-engine `available` bool. `TTSService.list_engines()` (`tts/service.py:32`) is sync and calls `provider.available()`. `check_engine()` is async in both so the two can be `gather`ed uniformly. The engines needing a live probe are exactly `http_stt` (STT) and `http_tts` (TTS).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_engine_health_check.py`:

```python
import pytest

from app.services.stt.service import stt_service
from app.services.tts.service import tts_service

_STT_ROW = {
    "id": "s1", "kind": "stt", "engine": "http_stt", "model_id": "Qwen/Qwen3-ASR-0.6B",
    "label": "local", "enabled": True, "stage": "stable",
    "api_key": "tok", "base_url": "http://127.0.0.1:8100/v1", "config": {},
}
_TTS_ROW = {
    "id": "t1", "kind": "tts", "engine": "http_tts", "model_id": "vieneu",
    "label": "local", "enabled": True, "stage": "stable",
    "api_key": "tok", "base_url": "http://127.0.0.1:8101/v1", "config": {},
}


def _patch_probe(monkeypatch, target: str, ok: bool, reason: str | None):
    async def fake_probe(base_url, api_key, timeout=3.0):
        return ok, reason

    monkeypatch.setattr(target, fake_probe)


@pytest.mark.asyncio
async def test_stt_unknown_engine_is_unavailable():
    health = await stt_service.check_engine("no_such_engine")
    assert health.status == "unavailable"
    assert health.engine == "no_such_engine"


@pytest.mark.asyncio
async def test_stt_http_stt_unavailable_when_no_row(monkeypatch):
    async def no_row(kind, engine, model_id=""):
        return None

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", no_row)
    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find_enabled",
        lambda kind, engine=None: no_row(kind, engine))
    health = await stt_service.check_engine("http_stt")
    assert health.status == "unavailable"
    assert "not configured" in health.detail


@pytest.mark.asyncio
async def test_stt_http_stt_unavailable_when_probe_fails(monkeypatch):
    async def row(kind, engine, model_id=""):
        return dict(_STT_ROW)

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", row)
    _patch_probe(monkeypatch, "app.services.stt.service.probe_service_health",
                 False, "All connection attempts failed")
    health = await stt_service.check_engine("http_stt", "Qwen/Qwen3-ASR-0.6B")
    assert health.status == "unavailable"
    assert "All connection attempts failed" in health.detail


@pytest.mark.asyncio
async def test_stt_http_stt_ok_when_probe_succeeds(monkeypatch):
    async def row(kind, engine, model_id=""):
        return dict(_STT_ROW)

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", row)
    _patch_probe(monkeypatch, "app.services.stt.service.probe_service_health", True, None)
    health = await stt_service.check_engine("http_stt", "Qwen/Qwen3-ASR-0.6B")
    assert health.status == "ok"


@pytest.mark.asyncio
async def test_stt_local_engine_not_ready_while_warming(monkeypatch):
    monkeypatch.setattr("app.services.stt.service.is_ready", lambda p: False)
    monkeypatch.setattr("app.services.stt.service._needs_warming", lambda p: True)
    health = await stt_service.check_engine("vosk")
    assert health.status == "not_ready"


@pytest.mark.asyncio
async def test_stt_local_engine_ok_when_warm(monkeypatch):
    monkeypatch.setattr("app.services.stt.service.is_ready", lambda p: True)
    monkeypatch.setattr("app.services.stt.service._needs_warming", lambda p: True)
    health = await stt_service.check_engine("vosk")
    assert health.status == "ok"


@pytest.mark.asyncio
async def test_stt_cloud_engine_is_never_probed(monkeypatch):
    """qwencloud has no free health endpoint -- config check only, no network."""
    called = {"n": 0}

    async def spy(base_url, api_key, timeout=3.0):
        called["n"] += 1
        return True, None

    monkeypatch.setattr("app.services.stt.service.probe_service_health", spy)
    await stt_service.check_engine("qwencloud")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_tts_unknown_engine_is_unavailable():
    health = await tts_service.check_engine("no_such_engine")
    assert health.status == "unavailable"


@pytest.mark.asyncio
async def test_tts_http_tts_unavailable_when_probe_fails(monkeypatch):
    async def row(kind, engine, model_id=""):
        return dict(_TTS_ROW)

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", row)
    _patch_probe(monkeypatch, "app.services.tts.service.probe_service_health",
                 False, "All connection attempts failed")
    health = await tts_service.check_engine("http_tts", "vieneu")
    assert health.status == "unavailable"
    assert "All connection attempts failed" in health.detail


@pytest.mark.asyncio
async def test_tts_http_tts_ok_when_probe_succeeds(monkeypatch):
    async def row(kind, engine, model_id=""):
        return dict(_TTS_ROW)

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", row)
    _patch_probe(monkeypatch, "app.services.tts.service.probe_service_health", True, None)
    health = await tts_service.check_engine("http_tts", "vieneu")
    assert health.status == "ok"


@pytest.mark.asyncio
async def test_tts_local_engine_unavailable_when_provider_says_so(monkeypatch):
    monkeypatch.setattr(
        tts_service.providers["vieneu"], "available", lambda: False, raising=False)
    health = await tts_service.check_engine("vieneu")
    assert health.status == "unavailable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_engine_health_check.py -v`
Expected: FAIL — `AttributeError: 'STTService' object has no attribute 'check_engine'`

- [ ] **Step 3: Write minimal implementation**

In `apps/api_gateway/app/services/stt/service.py`, add these imports at the top (next to the existing imports):

```python
from app.schemas.health import EngineHealth
from app.services.model_registry.health_probe import probe_service_health
from app.services.warmup import _needs_warming, is_ready
```

Then add this method to `STTService`, immediately after `get_provider()` (line 96):

```python
    async def check_engine(self, engine: str, model: str = "") -> EngineHealth:
        """Whether a session can start on this engine right now.

        Only http_stt gets a live network probe -- it is the one STT engine
        backed by a self-hosted process that can silently die. Cloud engines
        are config-checked only, to avoid paying for an API call per session.
        """
        from app.services.model_registry.store import model_registry_store
        from app.services.providers.resolve import resolve_credentials

        provider = self.providers.get(engine)
        if provider is None:
            return EngineHealth(
                engine=engine, status="unavailable", detail=f"unknown STT engine: {engine}"
            )

        if engine == "http_stt":
            entry = (
                await model_registry_store.find("stt", engine, model)
                if model
                else await model_registry_store.find_enabled("stt", engine)
            )
            if not entry:
                return EngineHealth(
                    engine=engine, status="unavailable",
                    detail="not configured: no enabled Model Registry entry",
                )
            base_url, api_key = await resolve_credentials(entry)
            reachable, reason = await probe_service_health(base_url, api_key)
            if not reachable:
                return EngineHealth(
                    engine=engine, status="unavailable",
                    detail=f"unreachable at {base_url or '(no base_url)'}: {reason}",
                )
            return EngineHealth(engine=engine, status="ok", detail=base_url)

        # Everything else: reuse the availability semantics list_engines()
        # already publishes, so the gate and the dashboard never disagree.
        for listed in await self.list_engines():
            if listed["engine"] != engine:
                continue
            if not listed["available"]:
                return EngineHealth(
                    engine=engine, status="unavailable",
                    detail=listed.get("detail") or "not configured",
                )
            break

        if _needs_warming(provider) and not is_ready(provider):
            return EngineHealth(engine=engine, status="not_ready", detail="model still loading")
        return EngineHealth(engine=engine, status="ok")
```

In `apps/api_gateway/app/services/tts/service.py`, add these imports at the top:

```python
from app.schemas.health import EngineHealth
from app.services.model_registry.health_probe import probe_service_health
from app.services.warmup import _needs_warming, is_ready
```

And add this method to `TTSService`, immediately after `get_provider()` (line 30):

```python
    async def check_engine(self, engine: str, model_id: str = "") -> EngineHealth:
        """Whether a session can start on this engine right now. See
        STTService.check_engine -- same three-state contract."""
        from app.services.model_registry.store import model_registry_store
        from app.services.providers.resolve import resolve_credentials

        provider = self.providers.get(engine)
        if provider is None:
            return EngineHealth(
                engine=engine, status="unavailable", detail=f"unknown TTS engine: {engine}"
            )

        if engine == "http_tts":
            entry = (
                await model_registry_store.find("tts", engine, model_id)
                if model_id
                else await model_registry_store.find_enabled("tts", engine)
            )
            if not entry:
                return EngineHealth(
                    engine=engine, status="unavailable",
                    detail="not configured: no enabled Model Registry entry",
                )
            base_url, api_key = await resolve_credentials(entry)
            reachable, reason = await probe_service_health(base_url, api_key)
            if not reachable:
                return EngineHealth(
                    engine=engine, status="unavailable",
                    detail=f"unreachable at {base_url or '(no base_url)'}: {reason}",
                )
            return EngineHealth(engine=engine, status="ok", detail=base_url)

        if not provider.available():
            return EngineHealth(
                engine=engine, status="unavailable", detail=provider.install_hint() or "not available"
            )
        if _needs_warming(provider) and not is_ready(provider):
            return EngineHealth(engine=engine, status="not_ready", detail="model still loading")
        return EngineHealth(engine=engine, status="ok")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_engine_health_check.py -v`
Expected: PASS (16 tests total — 5 from Task 3, 11 new)

- [ ] **Step 5: Run the neighboring suites for regressions**

Run: `.venv/bin/pytest tests/unit/test_http_tts_provider.py tests/unit/test_http_stt_provider.py tests/unit/test_tts_engines_mode.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/stt/service.py apps/api_gateway/app/services/tts/service.py tests/unit/test_engine_health_check.py
git commit -m "feat(health): add check_engine() to STT and TTS services"
```

---

### Task 5: `check_profile_health()` service

**Files:**
- Create: `apps/api_gateway/app/services/health.py`
- Test: `tests/unit/test_profile_health.py`

**Interfaces:**
- Consumes: `stt_service.check_engine` / `tts_service.check_engine` (Task 4), `ProfileHealth`/`EngineHealth` (Task 3), `resolve_stt` (`app/services/stt/profile.py:11`), `profile_store` / `tts_profile_store` / `system_config_store` (existing).
- Produces: `async def check_profile_health(profile_name: str | None) -> ProfileHealth`, and `async def check_resolved_engines(stt_engine, stt_model, tts_engine, tts_model) -> tuple[EngineHealth, EngineHealth]` (the concurrent primitive the WS routes call directly with engines they already resolved).

**Background:** Engine resolution must match `conversation.py:299-320` and `lugo.py:_resolve` exactly. `check_profile_health` re-resolves from the profile (for the HTTP endpoint); the WS routes have already resolved and call `check_resolved_engines` directly to avoid resolving twice.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_profile_health.py`:

```python
import asyncio

import pytest

from app.schemas.health import EngineHealth
from app.services.health import check_profile_health, check_resolved_engines


@pytest.mark.asyncio
async def test_runs_both_checks_concurrently(monkeypatch):
    """Both engines can be remote; running them in series would double the
    worst-case connect latency a user waits through."""
    order = []

    async def slow_stt(engine, model=""):
        order.append("stt_start")
        await asyncio.sleep(0.05)
        order.append("stt_end")
        return EngineHealth(engine=engine, status="ok")

    async def slow_tts(engine, model_id=""):
        order.append("tts_start")
        await asyncio.sleep(0.05)
        order.append("tts_end")
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", slow_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", slow_tts)

    await check_resolved_engines("http_stt", "", "http_tts", "")
    # Interleaved starts prove gather, not sequential awaits.
    assert order[:2] == ["stt_start", "tts_start"]


@pytest.mark.asyncio
async def test_returns_both_healths_in_order(monkeypatch):
    async def fake_stt(engine, model=""):
        return EngineHealth(engine=engine, status="unavailable", detail="down")

    async def fake_tts(engine, model_id=""):
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", fake_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", fake_tts)

    stt, tts = await check_resolved_engines("http_stt", "m1", "vieneu", "")
    assert stt.engine == "http_stt" and stt.status == "unavailable"
    assert tts.engine == "vieneu" and tts.status == "ok"


@pytest.mark.asyncio
async def test_check_profile_health_resolves_from_profile(monkeypatch, tmp_path):
    from app.services.profiles.models import Profile, SttConfig
    from app.services.profiles.store import ProfileStore

    store = ProfileStore(str(tmp_path / "p.json"))
    store.upsert(Profile(name="dev", stt=SttConfig(engine="http_stt", model="m1")))
    monkeypatch.setattr("app.services.health.profile_store", store)

    seen = {}

    async def fake_stt(engine, model=""):
        seen["stt"] = (engine, model)
        return EngineHealth(engine=engine, status="ok")

    async def fake_tts(engine, model_id=""):
        seen["tts"] = (engine, model_id)
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", fake_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", fake_tts)

    health = await check_profile_health("dev")
    assert health.profile == "dev"
    assert seen["stt"] == ("http_stt", "m1")


@pytest.mark.asyncio
async def test_check_profile_health_unknown_profile_uses_defaults(monkeypatch, tmp_path):
    from app.services.profiles.store import ProfileStore

    monkeypatch.setattr("app.services.health.profile_store", ProfileStore(str(tmp_path / "p.json")))

    async def fake_stt(engine, model=""):
        return EngineHealth(engine=engine, status="ok")

    async def fake_tts(engine, model_id=""):
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", fake_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", fake_tts)

    health = await check_profile_health("ghost")
    assert health.profile == "ghost"
    assert health.stt.status == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_profile_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.health'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/api_gateway/app/services/health.py`:

```python
"""Pre-flight health for a profile's resolved STT/TTS engines.

Exists so a WS connect can be refused before the user speaks, instead of the
first utterance failing mid-turn. Engine resolution here mirrors what
api/routes/conversation.py and api/routes/lugo.py do, so the HTTP endpoint and
the WS gate can never disagree about which engine a profile actually uses.
"""

from __future__ import annotations

import asyncio

from app.schemas.health import EngineHealth, ProfileHealth
from app.services.profiles.store import profile_store
from app.services.stt.profile import resolve_stt
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.profile_store import tts_profile_store
from app.services.tts.service import tts_service


async def check_resolved_engines(
    stt_engine: str, stt_model: str, tts_engine: str, tts_model: str
) -> tuple[EngineHealth, EngineHealth]:
    """Check both engines concurrently -- they hit different registry rows and
    different hosts, so serializing them would double the worst-case wait a
    connecting client sits through."""
    return await asyncio.gather(
        stt_service.check_engine(stt_engine, stt_model),
        tts_service.check_engine(tts_engine, tts_model),
    )


async def check_profile_health(profile_name: str | None) -> ProfileHealth:
    profile = profile_store.get(profile_name) if profile_name else None
    stt_engine, _language, stt_model = resolve_stt(profile)

    tts_name = (profile.tts.profile_name if profile else "") or None
    tts_profile = tts_profile_store.get(tts_name) if tts_name else None
    if tts_profile and tts_profile.engine:
        tts_engine, tts_model = tts_profile.engine, tts_profile.model_id or ""
    else:
        tts_engine = system_config_store.get().engines.default_tts_engine
        tts_model = ""

    stt_health, tts_health = await check_resolved_engines(
        stt_engine, stt_model, tts_engine, tts_model
    )
    return ProfileHealth(profile=profile_name or "", stt=stt_health, tts=tts_health)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_profile_health.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/health.py tests/unit/test_profile_health.py
git commit -m "feat(health): add concurrent profile engine health check"
```

---

### Task 6: `GET /v1/profiles/{name}/health` endpoint

**Files:**
- Modify: `apps/api_gateway/app/api/routes/profiles.py`
- Test: `tests/unit/test_profile_health.py` (append)

**Interfaces:**
- Consumes: `check_profile_health()` (Task 5).
- Produces: `GET /v1/profiles/{name}/health` → `{"data": {profile, stt: {...}, tts: {...}}}`.

**Background:** Other routes in this file return `{"data": ...}` (see `list_profiles` at `profiles.py:129`). Match that envelope. The route must be declared **before** any conflicting path — `/{name}` at line 151 is a different path shape (`/{name}/health` is more specific and FastAPI matches it fine), so appending at the end of the file is safe.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_profile_health.py`:

```python
from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint_returns_profile_health(monkeypatch, tmp_path):
    from app.services.profiles.models import Profile, SttConfig
    from app.services.profiles.store import ProfileStore

    store = ProfileStore(str(tmp_path / "ep.json"))
    store.upsert(Profile(name="dev", stt=SttConfig(engine="http_stt")))
    monkeypatch.setattr("app.services.health.profile_store", store)

    async def fake_stt(engine, model=""):
        return EngineHealth(engine=engine, status="unavailable", detail="unreachable")

    async def fake_tts(engine, model_id=""):
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", fake_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", fake_tts)

    resp = TestClient(app).get("/v1/profiles/dev/health")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["profile"] == "dev"
    assert data["stt"]["status"] == "unavailable"
    assert data["stt"]["detail"] == "unreachable"
    assert data["tts"]["status"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_profile_health.py -v -k endpoint`
Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Write minimal implementation**

Append to `apps/api_gateway/app/api/routes/profiles.py`:

```python
@router.get("/{name}/health")
async def profile_health(name: str) -> dict:
    """Live health of the STT/TTS engines this profile would actually use.

    Same check the WS connect gate runs, exposed so the admin UI can show a
    profile as broken before a user tries to talk to it."""
    from app.services.health import check_profile_health

    return {"data": (await check_profile_health(name)).model_dump()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_profile_health.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/profiles.py tests/unit/test_profile_health.py
git commit -m "feat(health): expose GET /v1/profiles/{name}/health"
```

---

### Task 7: Gate both WebSocket routes

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py:346-352`
- Modify: `apps/api_gateway/app/api/routes/lugo.py` (before `await session.start()` at line 178)
- Test: `tests/unit/test_session_health_gate.py`

**Interfaces:**
- Consumes: `check_resolved_engines()` (Task 5).
- Produces: nothing consumed by later tasks.

**Background:** `conversation.py` already has a gate at 346-352 catching `AppError` from `get_provider()` and emitting `{"event": "error", ...}`. Extend that same block — do not add a second one. `lugo.py` has no such gate; its error shape is `{"type": "error", "message": ...}` (see line 166), not `{"event": ...}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_session_health_gate.py`:

```python
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.health import EngineHealth


@pytest.fixture
def client():
    return TestClient(app)


def _patch_health(monkeypatch, stt_status: str, tts_status: str, detail: str = "boom"):
    async def fake(stt_engine, stt_model, tts_engine, tts_model):
        return (
            EngineHealth(engine=stt_engine, status=stt_status, detail=detail),
            EngineHealth(engine=tts_engine, status=tts_status, detail=detail),
        )

    monkeypatch.setattr("app.api.routes.conversation.check_resolved_engines", fake)
    monkeypatch.setattr("app.api.routes.lugo.check_resolved_engines", fake)


def test_conversation_ws_rejected_when_stt_unavailable(client, monkeypatch):
    _patch_health(monkeypatch, "unavailable", "ok", detail="unreachable at http://x")
    with client.websocket_connect("/v1/conversation/stream") as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"
        assert "unreachable at http://x" in msg["message"]


def test_conversation_ws_rejected_when_tts_unavailable(client, monkeypatch):
    _patch_health(monkeypatch, "ok", "unavailable", detail="no base_url")
    with client.websocket_connect("/v1/conversation/stream") as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"
        assert "no base_url" in msg["message"]


def test_conversation_ws_allowed_when_engines_not_ready(client, monkeypatch):
    """not_ready = still warming, not broken -- must NOT block the session."""
    _patch_health(monkeypatch, "not_ready", "not_ready")
    with client.websocket_connect("/v1/conversation/stream") as ws:
        msg = ws.receive_json()
        assert msg["event"] != "error"


def test_conversation_ws_allowed_when_all_ok(client, monkeypatch):
    _patch_health(monkeypatch, "ok", "ok")
    with client.websocket_connect("/v1/conversation/stream") as ws:
        msg = ws.receive_json()
        assert msg["event"] != "error"


def test_lugo_ws_rejected_when_stt_unavailable(client, monkeypatch):
    _patch_health(monkeypatch, "unavailable", "ok", detail="unreachable")
    with client.websocket_connect("/v1/lugo/stream") as ws:
        ws.send_text(json.dumps({"type": "wakeup", "profile": None}))
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "unreachable" in msg["message"]


def test_lugo_ws_allowed_when_all_ok(client, monkeypatch):
    _patch_health(monkeypatch, "ok", "ok")
    with client.websocket_connect("/v1/lugo/stream") as ws:
        ws.send_text(json.dumps({"type": "wakeup", "profile": None}))
        msg = ws.receive_json()
        assert msg["type"] != "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_session_health_gate.py -v`
Expected: FAIL — `AttributeError: module 'app.api.routes.conversation' has no attribute 'check_resolved_engines'`

- [ ] **Step 3: Write minimal implementation**

In `apps/api_gateway/app/api/routes/conversation.py`, add the import near the other service imports at the top:

```python
from app.services.health import check_resolved_engines
```

Then replace the existing gate at lines 346-352:

```python
    try:
        stt_service.get_provider(stt_engine)
        tts_service.get_provider(tts_engine)
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return
```

with:

```python
    try:
        stt_service.get_provider(stt_engine)
        tts_service.get_provider(tts_engine)
    except AppError as exc:
        await websocket.send_json({"event": "error", "message": str(exc)})
        await websocket.close()
        return

    # Fail fast on a dead engine here rather than after the user has already
    # spoken an utterance and the first transcribe/synthesize call blows up.
    stt_health, tts_health = await check_resolved_engines(
        stt_engine, stt_model, tts_engine, tts_model
    )
    for health in (stt_health, tts_health):
        if health.blocks_session:
            await websocket.send_json({
                "event": "error",
                "message": f"{health.engine} is unavailable: {health.detail}",
            })
            await websocket.close()
            return
```

In `apps/api_gateway/app/api/routes/lugo.py`, add the import next to the other service imports:

```python
from app.services.health import check_resolved_engines
```

Then, immediately before `session = ConversationSession(cfg, emit, emit_audio)` at line 177, insert:

```python
    stt_health, tts_health = await check_resolved_engines(
        stt_engine, stt_model, tts["engine"], tts["model_id"]
    )
    for health in (stt_health, tts_health):
        if health.blocks_session:
            await websocket.send_json({
                "type": "error",
                "message": f"{health.engine} is unavailable: {health.detail}",
            })
            await websocket.close()
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_session_health_gate.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run all WS suites for regressions**

Run: `.venv/bin/pytest tests/unit/test_conversation.py tests/unit/test_conversation_profile.py tests/unit/test_conversation_engine_ready.py tests/unit/test_conversation_tts_profile.py tests/unit/test_lugo_stream.py tests/unit/test_lugo_stt_resolution.py -v`
Expected: PASS. If any existing WS test now fails because its engines report `unavailable` in the hermetic test environment, that is a real signal — patch `check_resolved_engines` to return two `EngineHealth(status="ok")` values in that test's fixture rather than weakening the gate.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/lugo.py tests/unit/test_session_health_gate.py
git commit -m "feat(health): reject WS sessions whose STT/TTS engine is unavailable"
```

---

### Task 8: Full-suite verification and manual check

**Files:** none (verification only)

- [ ] **Step 1: Run the whole gateway suite**

Run: `.venv/bin/pytest tests/unit -q`
Expected: PASS, no regressions against the pre-change baseline.

- [ ] **Step 2: Reproduce the original bug manually**

With both model_service processes stopped (nothing on :8100/:8101) and the gateway running on :8000:

```bash
curl -s http://127.0.0.1:8000/v1/profiles/default/health | python3 -m json.tool
```

Expected: `stt.status == "unavailable"` with a detail naming the unreachable base_url.

- [ ] **Step 3: Verify the healthy path**

Start the STT service, then re-run the same curl:

```bash
PYTHONPATH=apps/api_gateway:apps SERVICE_KIND=stt SERVICE_ENGINE=qwen3_asr \
  SERVICE_API_TOKEN=dev-token SERVICE_PORT=8100 \
  .venv/bin/uvicorn model_service.app.main:create_app --factory --port 8100
```

Expected: `stt.status` flips to `"ok"` with no gateway restart (entries are resolved per call, nothing is cached).

- [ ] **Step 4: Commit any fixes**

Only if steps 1-3 surfaced problems. Otherwise nothing to commit.

---

## Self-Review

**Spec coverage:**
- `health_probe.py` → Task 1 ✓
- `check_engine()` on both services → Task 4 ✓
- `schemas/health.py` → Task 3 ✓
- `services/health.py` + `asyncio.gather` concurrency → Task 5 ✓
- `GET /v1/profiles/{name}/health` → Task 6 ✓
- `conversation.py` gate → Task 7 ✓
- `lugo.py` gate → Task 7 ✓
- `HttpTtsProvider.available()` bug fix → Task 2 ✓
- 3-state status model, `unavailable` blocks / `not_ready` doesn't → Tasks 3, 4, 7 ✓
- Probe only for `http_stt`/`http_tts`, no cloud probing → Task 4 (incl. `test_stt_cloud_engine_is_never_probed`) ✓
- No caching, 3s timeout → Tasks 1, 4 ✓
- Testing section of spec → Tasks 1-7 all carry their tests ✓

**Type consistency:** `probe_service_health` returns `tuple[bool, str | None]` in Task 1 and is consumed as `reachable, reason` in Task 4 ✓. `check_engine(engine, model="")` (STT) / `check_engine(engine, model_id="")` (TTS) as defined in Task 4 match the call sites in Task 5 ✓. `check_resolved_engines(stt_engine, stt_model, tts_engine, tts_model)` defined in Task 5 matches both call sites in Task 7 ✓. `EngineHealth.blocks_session` defined in Task 3 is used in Task 7 ✓.
