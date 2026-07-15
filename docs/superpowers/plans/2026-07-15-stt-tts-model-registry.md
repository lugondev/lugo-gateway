# STT/TTS config → Model Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Remote STT (`whisper_service`, `eventlab`), STT Local device/compute_type (`whisper_local`, `qwen3_asr`), and OmniVoice config off `SystemConfig` and onto Model Registry entries (`kind="stt"`/`kind="tts"`), mirroring the Conversation LLM migration already done for `kind="llm"`.

**Architecture:** `ModelRegistryStore` already stores `(kind, engine, model_id, base_url, api_key, config: dict)` rows and has an in-memory cache. Add two synchronous, cache-only read methods (`find_sync`, `find_enabled_sync`) so STT/TTS provider code — much of which runs off the event loop via `asyncio.to_thread`, or at module-import time before anything has awaited the store — can read a registry entry without touching `asyncio.Lock`/event-loop machinery. A new `app/services/model_registry/resolve.py` module reconstructs the *existing* `SttLocalConfig`/`OmnivoiceConfig`/`RemoteSttConfig` shapes from registry entries, so provider code keeps its current attribute-style reads (`cfg.omnivoice_device`) and only the **one line that fetches `cfg`** changes per call site — not every attribute access.

**Tech Stack:** FastAPI, SQLAlchemy async (aiosqlite), Pydantic, pytest + pytest-asyncio, vanilla JS (no framework) for the admin UI.

## Global Constraints

- TDD: write the failing test first, then the minimal implementation, for every step (repo convention, see `superpowers:test-driven-development`).
- Commit after each task (repo convention: small, focused commits — see recent `git log`).
- Never make network calls in tests; every new test must go through the existing hermetic fixtures (`tests/conftest.py`).
- `whisper_manager`'s size-selection mechanism (tiny/small/medium/large) is out of scope — untouched.
- No backward-compat shim: once migrated, the old `SystemConfig` fields are deleted, not deprecated.
- `docs/superpowers/specs/2026-07-15-stt-tts-model-registry-design.md` is the approved spec this plan implements — resolve any ambiguity by re-reading it.

---

### Task 1: Sync, cache-only reads on `ModelRegistryStore`

**Files:**
- Modify: `apps/api_gateway/app/services/model_registry/store.py`
- Test: `tests/unit/test_model_registry_store.py`

**Interfaces:**
- Produces: `ModelRegistryStore.find_sync(kind: str, engine: str, model_id: str) -> dict | None`, `ModelRegistryStore.find_enabled_sync(kind: str, engine: str | None = None) -> dict | None` — both read `self._by_id` directly (no `await`, no lock), returning `None` if the cache hasn't been warmed yet (nothing has `await`ed any store method since process start / last `invalidate()`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_model_registry_store.py`:

```python
def test_find_sync_returns_none_before_cache_warmed():
    store = ModelRegistryStore()
    assert store.find_sync("stt", "whisper_local", "") is None
    assert store.find_enabled_sync("stt", "whisper_local") is None


@pytest.mark.asyncio
async def test_find_sync_reads_the_warmed_cache():
    store = ModelRegistryStore()
    created = await store.create("stt", "whisper_local", "", "Whisper Local", config={"device": "cuda"})
    assert store.find_sync("stt", "whisper_local", "") == created
    assert store.find_enabled_sync("stt", "whisper_local") == created
    assert store.find_enabled_sync("stt", "qwen3_asr") is None


@pytest.mark.asyncio
async def test_find_enabled_sync_skips_disabled_entries():
    store = ModelRegistryStore()
    entry = await store.create("tts", "omnivoice", "k2-fsa/OmniVoice", "OmniVoice")
    await store.set_fields(entry["id"], enabled=False)
    assert store.find_enabled_sync("tts", "omnivoice") is None
```

Check the top of `tests/unit/test_model_registry_store.py` already imports `ModelRegistryStore` and `pytest` — if not, add:
```python
import pytest

from app.services.model_registry.store import ModelRegistryStore
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_model_registry_store.py -k find_sync -v`
Expected: FAIL with `AttributeError: 'ModelRegistryStore' object has no attribute 'find_sync'`

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/model_registry/store.py`, add these two methods to `ModelRegistryStore` right after `find_enabled`:

```python
    def find_sync(self, kind: str, engine: str, model_id: str) -> dict | None:
        """Synchronous, cache-only equivalent of `find()` for call sites that
        can't `await` -- provider code that builds a model off the event loop
        via `asyncio.to_thread`, or module-level singletons constructed at
        import time before anything has awaited this store. Returns None if
        the cache hasn't been warmed yet (nothing has awaited any store
        method since process start / last `invalidate()`) -- callers must
        treat that exactly like "no matching entry", the same fallback they
        already need for a genuinely-missing entry."""
        if self._by_id is None:
            return None
        for entry in self._by_id.values():
            if entry["kind"] == kind and entry["engine"] == engine and entry["model_id"] == model_id:
                return entry
        return None

    def find_enabled_sync(self, kind: str, engine: str | None = None) -> dict | None:
        """Synchronous, cache-only equivalent of `find_enabled()` -- see
        `find_sync` for why a sync path is needed."""
        if self._by_id is None:
            return None
        for entry in self._by_id.values():
            if entry["kind"] == kind and entry["enabled"] and (engine is None or entry["engine"] == engine):
                return entry
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_model_registry_store.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/store.py tests/unit/test_model_registry_store.py
git commit -m "feat(model-registry): add sync cache-only find helpers"
```

---

### Task 2: Registry-backed config resolvers

**Files:**
- Create: `apps/api_gateway/app/services/model_registry/resolve.py`
- Test: `tests/unit/test_model_registry_resolve.py`

**Interfaces:**
- Consumes: `ModelRegistryStore.find_sync`/`find_enabled_sync` (Task 1); `app.services.system_config.SttLocalConfig`/`OmnivoiceConfig`/`RemoteSttConfig` (still exist as plain shape classes after Task 7 — importing them here does not create a cycle since `resolve.py` is new and `system_config.py` never imports it).
- Produces: `resolve_stt_local_device(engine: str) -> dict` (`{"device": str, "compute_type": str}`), `resolve_omnivoice_config() -> OmnivoiceConfig`, `resolve_remote_stt_config() -> RemoteSttConfig`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_model_registry_resolve.py`:

```python
import pytest

from app.services.model_registry.resolve import (
    resolve_omnivoice_config,
    resolve_remote_stt_config,
    resolve_stt_local_device,
)
from app.services.model_registry.store import model_registry_store
from app.services.system_config import OmnivoiceConfig, RemoteSttConfig


def test_resolve_stt_local_device_defaults_when_no_entry():
    assert resolve_stt_local_device("whisper_local") == {"device": "", "compute_type": "int8"}


@pytest.mark.asyncio
async def test_resolve_stt_local_device_reads_registry_config():
    await model_registry_store.create(
        "stt", "whisper_local", "", "Whisper Local",
        config={"device": "cuda", "compute_type": "float16"},
    )
    assert resolve_stt_local_device("whisper_local") == {"device": "cuda", "compute_type": "float16"}


def test_resolve_omnivoice_config_defaults_when_no_entry():
    assert resolve_omnivoice_config() == OmnivoiceConfig()


@pytest.mark.asyncio
async def test_resolve_omnivoice_config_reads_registry_entry():
    await model_registry_store.create(
        "tts", "omnivoice", "k2-fsa/OmniVoice-custom", "OmniVoice",
        config={"omnivoice_device": "mps", "omnivoice_dtype": "bfloat16"},
    )
    cfg = resolve_omnivoice_config()
    assert cfg.omnivoice_model_id == "k2-fsa/OmniVoice-custom"
    assert cfg.omnivoice_device == "mps"
    assert cfg.omnivoice_dtype == "bfloat16"
    assert cfg.omnivoice_server_host == "127.0.0.1"  # untouched default


def test_resolve_remote_stt_config_defaults_when_no_entries():
    assert resolve_remote_stt_config() == RemoteSttConfig()


@pytest.mark.asyncio
async def test_resolve_remote_stt_config_reads_both_registry_entries():
    await model_registry_store.create(
        "stt", "whisper_service", "gpt-4o-transcribe", "Whisper Service",
        base_url="https://api.example.com/v1", api_key="sk-abc",
        config={"timeout_seconds": 90.0},
    )
    await model_registry_store.create(
        "stt", "eventlab", "whisper-1", "Eventlab",
        base_url="https://eventlab.example.com", api_key="sk-def",
    )
    cfg = resolve_remote_stt_config()
    assert cfg.whisper_service_base_url == "https://api.example.com/v1"
    assert cfg.whisper_service_api_key == "sk-abc"
    assert cfg.whisper_service_model == "gpt-4o-transcribe"
    assert cfg.eventlab_base_url == "https://eventlab.example.com"
    assert cfg.eventlab_api_key == "sk-def"
    assert cfg.remote_stt_timeout_seconds == 90.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_model_registry_resolve.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.model_registry.resolve'`

- [ ] **Step 3: Write the implementation**

Create `apps/api_gateway/app/services/model_registry/resolve.py`:

```python
"""Reconstruct the SttLocalConfig/OmnivoiceConfig/RemoteSttConfig shapes from
Model Registry entries instead of SystemConfig. Provider code that already
does `cfg.omnivoice_device` etc. keeps every attribute access unchanged --
only the one line that fetches `cfg` switches to calling a resolver here.

All three resolvers are synchronous and cache-only (see
ModelRegistryStore.find_sync/find_enabled_sync): most call sites run off the
event loop (asyncio.to_thread) or at module-import time, before anything has
awaited the store.
"""

from __future__ import annotations

from app.services.model_registry.store import model_registry_store
from app.services.system_config import OmnivoiceConfig, RemoteSttConfig


def resolve_stt_local_device(engine: str) -> dict:
    """{'device': str, 'compute_type': str} for a local STT engine (only
    whisper_local uses compute_type; qwen3_asr's caller just ignores it)."""
    entry = model_registry_store.find_enabled_sync("stt", engine)
    config = (entry or {}).get("config") or {}
    return {
        "device": config.get("device", ""),
        "compute_type": config.get("compute_type", "int8"),
    }


def resolve_omnivoice_config() -> OmnivoiceConfig:
    entry = model_registry_store.find_enabled_sync("tts", "omnivoice")
    if entry is None:
        return OmnivoiceConfig()
    config = entry.get("config") or {}
    return OmnivoiceConfig(omnivoice_model_id=entry["model_id"]).model_copy(update=config)


def resolve_remote_stt_config() -> RemoteSttConfig:
    whisper = model_registry_store.find_enabled_sync("stt", "whisper_service")
    eventlab = model_registry_store.find_enabled_sync("stt", "eventlab")
    cfg = RemoteSttConfig()
    if whisper:
        cfg = cfg.model_copy(update={
            "whisper_service_base_url": whisper.get("base_url", ""),
            "whisper_service_api_key": whisper.get("api_key", ""),
            "whisper_service_model": whisper.get("model_id") or "whisper-1",
        })
    if eventlab:
        cfg = cfg.model_copy(update={
            "eventlab_base_url": eventlab.get("base_url", ""),
            "eventlab_api_key": eventlab.get("api_key", ""),
            "eventlab_model": eventlab.get("model_id") or "whisper-1",
        })
    timeout = (
        (whisper or {}).get("config", {}).get("timeout_seconds")
        or (eventlab or {}).get("config", {}).get("timeout_seconds")
    )
    if timeout:
        cfg = cfg.model_copy(update={"remote_stt_timeout_seconds": timeout})
    return cfg
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_model_registry_resolve.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/resolve.py tests/unit/test_model_registry_resolve.py
git commit -m "feat(model-registry): add STT-local/OmniVoice/remote-STT resolvers"
```

---

### Task 3: One-time migration seed + boot wiring

**Files:**
- Modify: `apps/api_gateway/app/services/model_registry/seed.py`
- Modify: `apps/api_gateway/app/main.py`
- Test: `tests/unit/test_model_registry_seed_migration.py`

**Interfaces:**
- Consumes: `system_config_store.get_raw_group(group: str) -> dict` (already exists, used by `migrate_conversation_llm_to_registry`); `model_registry_store.create`/`find_enabled` (Task 1's async originals, not the sync ones — seeding runs once at boot inside an async context).
- Produces: `migrate_remote_stt_to_registry() -> None`, `migrate_stt_local_device_to_registry() -> None`, `migrate_omnivoice_to_registry() -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_model_registry_seed_migration.py`:

```python
import pytest

from app.services.model_registry.seed import (
    migrate_omnivoice_to_registry,
    migrate_remote_stt_to_registry,
    migrate_stt_local_device_to_registry,
)
from app.services.model_registry.store import model_registry_store
from app.services.system_config import system_config_store


@pytest.mark.asyncio
async def test_migrate_remote_stt_seeds_from_existing_config():
    system_config_store.set(
        system_config_store.get().model_copy(update={
            "remote_stt": system_config_store.get().remote_stt.model_copy(update={
                "whisper_service_base_url": "https://api.example.com",
                "whisper_service_api_key": "sk-old",
                "whisper_service_model": "whisper-1",
            })
        })
    )
    await migrate_remote_stt_to_registry()
    entry = await model_registry_store.find_enabled("stt", "whisper_service")
    assert entry is not None
    assert entry["base_url"] == "https://api.example.com"
    assert entry["api_key"] == "sk-old"


@pytest.mark.asyncio
async def test_migrate_remote_stt_is_a_noop_once_migrated():
    await model_registry_store.create(
        "stt", "whisper_service", "whisper-1", "Whisper Service (manual)",
        base_url="https://manual.example.com",
    )
    await migrate_remote_stt_to_registry()
    entries = [e for e in await model_registry_store.list_all() if e["engine"] == "whisper_service"]
    assert len(entries) == 1
    assert entries[0]["base_url"] == "https://manual.example.com"


@pytest.mark.asyncio
async def test_migrate_stt_local_device_seeds_whisper_local_and_qwen3_asr():
    system_config_store.set(
        system_config_store.get().model_copy(update={
            "stt_local": system_config_store.get().stt_local.model_copy(update={
                "whisper_local_device": "cuda", "whisper_local_compute_type": "float16",
                "qwen3_asr_device": "mps",
            })
        })
    )
    await migrate_stt_local_device_to_registry()
    whisper_entry = await model_registry_store.find_enabled("stt", "whisper_local")
    qwen_entry = await model_registry_store.find_enabled("stt", "qwen3_asr")
    assert whisper_entry["config"] == {"device": "cuda", "compute_type": "float16"}
    assert qwen_entry["config"] == {"device": "mps"}


@pytest.mark.asyncio
async def test_migrate_omnivoice_seeds_from_existing_config():
    system_config_store.set(
        system_config_store.get().model_copy(update={
            "omnivoice": system_config_store.get().omnivoice.model_copy(update={
                "omnivoice_device": "mps", "omnivoice_dtype": "bfloat16",
            })
        })
    )
    await migrate_omnivoice_to_registry()
    entry = await model_registry_store.find_enabled("tts", "omnivoice")
    assert entry["model_id"] == "k2-fsa/OmniVoice"
    assert entry["config"]["omnivoice_device"] == "mps"
    assert entry["config"]["omnivoice_dtype"] == "bfloat16"
```

Note: these tests read `system_config_store.get().remote_stt`/`.stt_local`/`.omnivoice` — that's fine, they still exist until Task 7 removes them from the schema. Once Task 7 lands, rewrite these `system_config_store.set(...)` setup blocks to use `system_config_store._put()`-via-`get_raw_group` style instead (see Task 7's own test updates) — **do not** do that rewrite now; these tests are correct for the current (pre-Task-7) schema and Task 7 explicitly revisits this file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_model_registry_seed_migration.py -v`
Expected: FAIL with `ImportError: cannot import name 'migrate_remote_stt_to_registry'`

- [ ] **Step 3: Write the implementation**

Add to `apps/api_gateway/app/services/model_registry/seed.py` (after `migrate_conversation_llm_to_registry`):

```python
async def migrate_remote_stt_to_registry() -> None:
    """One-time: whisper_service/eventlab used to live in
    SystemConfig.remote_stt. Seed a registry entry per engine from the
    current values if none is enabled yet for that engine -- no-op once
    migrated (including a fresh install with nothing configured)."""
    remote_stt = system_config_store.get().remote_stt
    if (
        await model_registry_store.find_enabled("stt", "whisper_service") is None
        and remote_stt.whisper_service_base_url.strip()
    ):
        await model_registry_store.create(
            "stt", "whisper_service", remote_stt.whisper_service_model,
            "Whisper Service (migrated from System settings)",
            base_url=remote_stt.whisper_service_base_url,
            api_key=remote_stt.whisper_service_api_key,
            config={"timeout_seconds": remote_stt.remote_stt_timeout_seconds},
        )
    if (
        await model_registry_store.find_enabled("stt", "eventlab") is None
        and remote_stt.eventlab_base_url.strip()
    ):
        await model_registry_store.create(
            "stt", "eventlab", remote_stt.eventlab_model,
            "Eventlab (migrated from System settings)",
            base_url=remote_stt.eventlab_base_url,
            api_key=remote_stt.eventlab_api_key,
            config={"timeout_seconds": remote_stt.remote_stt_timeout_seconds},
        )


async def migrate_stt_local_device_to_registry() -> None:
    """One-time: whisper_local/qwen3_asr device+compute_type used to live in
    SystemConfig.stt_local. Seed one engine-level registry entry each
    (model_id="" -- distinct from the per-size governance rows
    seed_known_models() already creates) from the current values. No-op once
    an enabled entry already exists for that engine."""
    stt_local = system_config_store.get().stt_local
    if await model_registry_store.find_enabled("stt", "whisper_local") is None:
        await model_registry_store.create(
            "stt", "whisper_local", "", "Whisper Local (device/compute config)",
            config={
                "device": stt_local.whisper_local_device,
                "compute_type": stt_local.whisper_local_compute_type,
            },
        )
    if await model_registry_store.find_enabled("stt", "qwen3_asr") is None:
        await model_registry_store.create(
            "stt", "qwen3_asr", "", "Qwen3-ASR (device config)",
            config={"device": stt_local.qwen3_asr_device},
        )


async def migrate_omnivoice_to_registry() -> None:
    """One-time: OmniVoice's whole config used to live in
    SystemConfig.omnivoice (a single sidecar, so a single registry entry).
    No-op once an enabled tts/omnivoice entry already exists."""
    if await model_registry_store.find_enabled("tts", "omnivoice") is not None:
        return
    omnivoice = system_config_store.get().omnivoice
    config = omnivoice.model_dump()
    config.pop("omnivoice_model_id")  # lives in the entry's model_id column, not config
    await model_registry_store.create(
        "tts", "omnivoice", omnivoice.omnivoice_model_id,
        "OmniVoice (migrated from System settings)",
        config=config,
    )
```

Now wire these into boot. Open `apps/api_gateway/app/main.py` and find where `migrate_conversation_llm_to_registry` (or the equivalent LLM migration call) already runs in the lifespan — add the three new calls right next to it:

```python
    from app.services.model_registry.seed import (
        migrate_conversation_llm_to_registry,
        migrate_omnivoice_to_registry,
        migrate_remote_stt_to_registry,
        migrate_stt_local_device_to_registry,
        seed_known_models,
    )

    await seed_known_models()
    await migrate_conversation_llm_to_registry()
    await migrate_remote_stt_to_registry()
    await migrate_stt_local_device_to_registry()
    await migrate_omnivoice_to_registry()
```

(Match this against whatever the exact existing import/call block looks like in `main.py` right now — add the three new imports and three new calls alongside the existing ones, keeping the existing calls exactly as they are.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_model_registry_seed_migration.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/model_registry/seed.py apps/api_gateway/app/main.py tests/unit/test_model_registry_seed_migration.py
git commit -m "feat(model-registry): seed remote-STT/STT-local/OmniVoice entries from System settings"
```

---

### Task 4: Remote STT reads from the registry

**Files:**
- Modify: `apps/api_gateway/app/services/stt/service.py`
- Modify: `apps/api_gateway/app/services/recommend/service.py`
- Test: `tests/unit/test_stt_service_openrouter.py` (extend) or a new `tests/unit/test_stt_remote_registry.py`

**Interfaces:**
- Consumes: `resolve_remote_stt_config()` (Task 2).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_stt_remote_registry.py`:

```python
import pytest

from app.services.model_registry.store import model_registry_store
from app.services.stt.service import STTService


@pytest.mark.asyncio
async def test_stt_service_reads_remote_stt_from_registry():
    await model_registry_store.create(
        "stt", "whisper_service", "whisper-1", "Whisper Service",
        base_url="https://api.example.com", api_key="sk-abc",
    )
    service = STTService()
    provider = service.get_provider("whisper_service")
    assert provider.base_url == "https://api.example.com"
    assert provider.api_key == "sk-abc"
```

Check `RemoteWhisperProvider`'s constructor stores `base_url`/`api_key` as plain attributes (it already must, since the current code passes them positionally/by keyword) — if the attribute names differ, adjust the assertions to match (e.g. `provider._base_url`).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_stt_remote_registry.py -v`
Expected: FAIL — `provider.base_url == ""` (still reading `SystemConfig`, registry entry ignored)

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/stt/service.py`:

Replace the import block's `from app.services.system_config import system_config_store` line — keep it (still used elsewhere in this file for `list_engines`'s vosk/whisper paths — check before removing) and add:
```python
from app.services.model_registry.resolve import resolve_remote_stt_config
```

Replace line 19 (`remote_stt = system_config_store.get().remote_stt`) with:
```python
        remote_stt = resolve_remote_stt_config()
```

Replace line 52's `reinit_remote_providers(self, remote_stt) -> None:` signature and body — it's called from `model_registry.py`'s PATCH route now (Task 7), not from `system.py`, so it no longer takes a parameter:
```python
    def reinit_remote_providers(self) -> None:
        """Rebuild whisper_service/eventlab/qwen3_asr_or/whisper_or with fresh
        settings — these providers cache base_url/api_key/model/timeout as
        instance attributes at construction and never re-read afterward."""
        remote_stt = resolve_remote_stt_config()
        self.providers["whisper_service"] = RemoteWhisperProvider(
            name="whisper_service",
            base_url=remote_stt.whisper_service_base_url,
            api_key=remote_stt.whisper_service_api_key,
            model=remote_stt.whisper_service_model,
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
        self.providers["eventlab"] = RemoteWhisperProvider(
            name="eventlab",
            base_url=remote_stt.eventlab_base_url,
            api_key=remote_stt.eventlab_api_key,
            model=remote_stt.eventlab_model,
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
        self.providers["qwen3_asr_or"] = OpenRouterSttProvider(
            name="qwen3_asr_or",
            model="qwen/qwen3-asr-flash-2026-02-10",
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
        self.providers["whisper_or"] = OpenRouterSttProvider(
            name="whisper_or",
            model="openai/whisper-large-v3-turbo",
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
```

Replace line 102 (`remote_stt = system_config_store.get().remote_stt` inside `list_engines`) with:
```python
        remote_stt = resolve_remote_stt_config()
```

In `apps/api_gateway/app/services/recommend/service.py`, find line 88 (`remote_stt = system_config_store.get().remote_stt`) and replace with:
```python
    from app.services.model_registry.resolve import resolve_remote_stt_config
    remote_stt = resolve_remote_stt_config()
```
(add the import at the top of the function or the module, following whatever import style — top-of-function lazy import — the surrounding code in that file already uses; check the file first.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_stt_remote_registry.py tests/unit/test_stt_service_openrouter.py -v`
Expected: all PASS

Also run the full recommend test file to catch the `recommend/service.py` change: `.venv/bin/pytest tests/test_recommend_route.py tests/test_recommender.py -v`
Expected: all PASS (or update any test that asserted on the old `system_config_store.get().remote_stt` call path — inspect failures and fix inline, this is a mechanical read-source swap, not a behavior change).

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/stt/service.py apps/api_gateway/app/services/recommend/service.py tests/unit/test_stt_remote_registry.py
git commit -m "feat(stt): resolve remote STT (whisper_service/eventlab) from Model Registry"
```

---

### Task 5: STT Local device/compute_type from the registry

**Files:**
- Modify: `apps/api_gateway/app/services/stt/providers/whisper_provider.py`
- Modify: `apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py`
- Test: `tests/unit/test_stt_model_param_isolation.py` (extend), `tests/unit/test_qwen3_asr_model.py` (extend)

**Interfaces:**
- Consumes: `resolve_stt_local_device(engine: str) -> dict` (Task 2).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_stt_model_param_isolation.py` (or a new small test file if that one doesn't fit — check its existing fixtures first):

```python
@pytest.mark.asyncio
async def test_cache_key_uses_registry_device_and_compute_type(monkeypatch):
    from app.services.model_registry.store import model_registry_store
    from app.services.stt.providers import whisper_provider

    await model_registry_store.create(
        "stt", "whisper_local", "", "Whisper Local",
        config={"device": "cuda", "compute_type": "float16"},
    )
    provider = whisper_provider.WhisperProvider()
    assert provider._cache_key("medium") == "medium:cuda:float16"
```

Add to `tests/unit/test_qwen3_asr_model.py`:

```python
@pytest.mark.asyncio
async def test_uses_registry_device_over_default(monkeypatch):
    from app.services.model_registry.store import model_registry_store
    from app.services.stt.providers import qwen3_asr_provider

    await model_registry_store.create("stt", "qwen3_asr", "", "Qwen3-ASR", config={"device": "mps"})
    # Adjust this assertion to match however this test file already exercises
    # the model-build path (e.g. monkeypatching the underlying model class and
    # asserting on the captured device_map kwarg) -- check the file's existing
    # tests for the established pattern before writing this one.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_stt_model_param_isolation.py -k registry -v`
Expected: FAIL — `_cache_key` still returns `"medium::int8"` (empty device, SystemConfig default compute_type)

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/stt/providers/whisper_provider.py`, add the import:
```python
from app.services.model_registry.resolve import resolve_stt_local_device
```

Replace the `_cache_key` method:
```python
    def _cache_key(self, model: str) -> str:
        device_cfg = resolve_stt_local_device("whisper_local")
        return ":".join([model, device_cfg["device"], device_cfg["compute_type"]])
```

Replace both occurrences inside `_load_model` that read `stt_local = system_config_store.get().stt_local` then use `.whisper_local_device`/`.whisper_local_compute_type` — e.g.:
```python
                    stt_local = system_config_store.get().stt_local
                    _MODEL_CACHE[key] = WhisperModel(
                        resolve_whisper_model(model_name),
                        device=stt_local.whisper_local_device,
                        compute_type=stt_local.whisper_local_compute_type,
                    )
```
becomes:
```python
                    device_cfg = resolve_stt_local_device("whisper_local")
                    _MODEL_CACHE[key] = WhisperModel(
                        resolve_whisper_model(model_name),
                        device=device_cfg["device"],
                        compute_type=device_cfg["compute_type"],
                    )
```
(Read the file first to find the exact current line numbers/surrounding code — earlier exploration in this session showed this pattern around what was then line ~80, but re-check before editing.)

In `apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py`, add the import and replace line ~145:
```python
from app.services.model_registry.resolve import resolve_stt_local_device
```
```python
                device_map=resolve_stt_local_device("qwen3_asr")["device"] or "cuda:0",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_stt_model_param_isolation.py tests/unit/test_qwen3_asr_model.py tests/unit/test_provider_single_flight_load.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/stt/providers/whisper_provider.py apps/api_gateway/app/services/stt/providers/qwen3_asr_provider.py tests/unit/test_stt_model_param_isolation.py tests/unit/test_qwen3_asr_model.py
git commit -m "feat(stt): resolve whisper_local/qwen3_asr device+compute_type from Model Registry"
```

---

### Task 6: OmniVoice reads its whole config from the registry

**Files:**
- Modify: `apps/api_gateway/app/services/tts/providers/omnivoice_provider.py`
- Test: `tests/unit/test_omnivoice_provider.py` (extend)

**Interfaces:**
- Consumes: `resolve_omnivoice_config() -> OmnivoiceConfig` (Task 2).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_omnivoice_provider.py`:

```python
@pytest.mark.asyncio
async def test_available_reads_python_path_from_registry(monkeypatch, tmp_path):
    from app.services.model_registry.store import model_registry_store

    fake_python = tmp_path / "python"
    fake_python.write_text("")
    await model_registry_store.create(
        "tts", "omnivoice", "k2-fsa/OmniVoice", "OmniVoice",
        config={"omnivoice_python": str(fake_python)},
    )
    provider = OmniVoiceProvider()
    assert provider.available() is True
```

(Check the file's existing imports for `OmniVoiceProvider` and its established async-test style before adding this — match the pattern already used there.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_omnivoice_provider.py -k registry -v`
Expected: FAIL — `available()` still reads the (default, nonexistent-path) `SystemConfig.omnivoice`

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/tts/providers/omnivoice_provider.py`:

1. Add the import:
```python
from app.services.model_registry.resolve import resolve_omnivoice_config
```

2. Replace **every** occurrence of `system_config_store.get().omnivoice` with `resolve_omnivoice_config()` — there are 13 occurrences (lines 50, 89, 100, 109, 115, 134, 147, 163, 190, 203, 214, 242, 261 as of this plan's writing; re-grep before editing since line numbers shift as you go):

```bash
grep -n "system_config_store.get().omnivoice" apps/api_gateway/app/services/tts/providers/omnivoice_provider.py
```

For each match, replace `system_config_store.get().omnivoice` with `resolve_omnivoice_config()` verbatim — the attribute access after it (`.omnivoice_device`, `.omnivoice_use_server`, etc.) is unchanged since `resolve_omnivoice_config()` returns the same `OmnivoiceConfig` shape. A safe way to do this as one edit:

```bash
sed -i '' 's/system_config_store\.get()\.omnivoice/resolve_omnivoice_config()/g' apps/api_gateway/app/services/tts/providers/omnivoice_provider.py
```

3. `system_config_store` may now be unused in this file — check:
```bash
grep -n "system_config_store" apps/api_gateway/app/services/tts/providers/omnivoice_provider.py
```
If the only remaining reference is the `import` line itself, remove that import line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_omnivoice_provider.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/tts/providers/omnivoice_provider.py tests/unit/test_omnivoice_provider.py
git commit -m "feat(tts): resolve OmniVoice config from Model Registry"
```

---

### Task 7: Remove the old SystemConfig fields + update routes

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py`
- Modify: `apps/api_gateway/app/api/routes/system.py`
- Modify: `apps/api_gateway/app/api/routes/model_registry.py`
- Modify: `apps/api_gateway/app/services/model_registry/store.py`
- Test: `tests/unit/test_system_config_store.py`, `tests/unit/test_system_config_routes.py`, `tests/unit/test_model_registry_routes.py` (all extend/trim)
- Test: `tests/unit/test_model_registry_seed_migration.py` (fix the setup blocks per Task 3's note)

**Interfaces:**
- Consumes: everything from Tasks 1-6 (this task removes the last SystemConfig fallback, so it must run last among the resolution tasks).
- Produces: `ModelRegistryStore.create(..., config: dict | None = None)` already exists (Task-1-unrelated, pre-existing) — this task adds `config` to the HTTP-facing `CreateEntryRequest`/`UpdateEntryRequest` schemas so the UI (Task 8) can set it.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_system_config_store.py`, remove any test asserting on `SystemConfig().stt_local.whisper_local_device`, `.qwen3_asr_device`, `.omnivoice`, or `.remote_stt` (grep first to find them):
```bash
grep -n "whisper_local_device\|whisper_local_compute_type\|qwen3_asr_device\|\.omnivoice\b\|\.remote_stt\b" tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py
```
For each hit, either delete the assertion (if it's testing exactly the field being removed) or update it to the new shape (if it's testing something else that happens to touch the field, e.g. round-tripping the whole `SystemConfig` — those should just drop the removed keys from their expected dict).

Add to `tests/unit/test_system_config_store.py`:
```python
def test_system_config_has_no_stt_local_device_fields():
    dumped = SystemConfig().model_dump()
    assert "whisper_local_device" not in dumped["stt_local"]
    assert "whisper_local_compute_type" not in dumped["stt_local"]
    assert "qwen3_asr_device" not in dumped["stt_local"]

def test_system_config_has_no_omnivoice_or_remote_stt_groups():
    dumped = SystemConfig().model_dump()
    assert "omnivoice" not in dumped
    assert "remote_stt" not in dumped
```

Add to `tests/unit/test_model_registry_routes.py`:
```python
@pytest.mark.asyncio
async def test_create_stt_entry_accepts_base_url_and_config(client, _with_password):
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "whisper_service", "model_id": "whisper-1",
        "label": "Whisper Service", "base_url": "https://api.example.com",
        "config": {"timeout_seconds": 45.0},
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["base_url"] == "https://api.example.com"

@pytest.mark.asyncio
async def test_patch_entry_can_update_config(client, _with_password):
    from app.services.model_registry.store import model_registry_store
    entry = await model_registry_store.create("tts", "omnivoice", "k2-fsa/OmniVoice", "OmniVoice")
    resp = client.patch(f"/v1/model_registry/{entry['id']}", json={"config": {"omnivoice_device": "mps"}})
    assert resp.status_code == 200
    assert resp.json()["data"]["config"] == {"omnivoice_device": "mps"}
```
(Match `client`/`_with_password` fixture names against whatever `test_model_registry_routes.py` already defines/imports — check the file first.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_system_config_store.py tests/unit/test_model_registry_routes.py -v`
Expected: the two new `test_system_config_has_no_*` tests FAIL (fields still present); the two new registry-route tests FAIL (`base_url` stays `""`, `config` PATCH has no effect since `UpdateEntryRequest` has no `config` field)

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/services/system_config.py`:

Remove `OmnivoiceConfig` and `RemoteSttConfig` classes entirely — wait, **do not delete the classes**: `resolve_omnivoice_config()`/`resolve_remote_stt_config()` (Task 2) still construct instances of them as the return type. Only remove them **as fields of `SystemConfig`**:

```python
class SystemConfig(BaseModel):
    base_context: str = ""
    engines: EngineDefaults = EngineDefaults()
    stt_local: SttLocalConfig = SttLocalConfig()
    conversation: ConversationTuningConfig = ConversationTuningConfig()
    preprocessing: PreprocessingConfig = PreprocessingConfig()
```
(drop the `omnivoice: OmnivoiceConfig = OmnivoiceConfig()` and `remote_stt: RemoteSttConfig = RemoteSttConfig()` lines — `OmnivoiceConfig`/`RemoteSttConfig` class *definitions* stay in this file, just no longer composed into `SystemConfig`.)

Remove the 3 device/compute_type fields from `SttLocalConfig`:
```python
class SttLocalConfig(BaseModel):
    stt_model_dir: str = "models/stt"
    vosk_model_path: str = "models/stt/vosk-model-small-en-us-0.15"
    vosk_model_base_url: str = "https://alphacephei.com/vosk/models"
    stt_stream_sample_rate: int = 16000
    whisper_local_model: str = "phowhisper-medium"
    whisper_vad_filter: bool = True
    whisper_beam_size: int = 1
    whisper_condition_on_previous_text: bool = False
    whisper_initial_prompt: str = ""
    stt_glossary_path: str = ""
    stt_profile: str = ""
    whisper_mlx_model_path: str = "models/stt/phowhisper-medium-mlx"
    qwen3_asr_model: str = "Qwen/Qwen3-ASR-0.6B"
    stt_segment_long_enabled: bool = False
    stt_segment_min_seconds: float = 30.0
    stt_segment_concurrency: int = 4
```
(removed: `whisper_local_device`, `whisper_local_compute_type`, `qwen3_asr_device`)

In `apps/api_gateway/app/api/routes/system.py`:

- `system_status()`: replace `stt_local = system_config_store.get().stt_local` block's use of `stt_local.whisper_local_device` and the `omnivoice = system_config_store.get().omnivoice` line:
```python
    from app.services.model_registry.resolve import resolve_omnivoice_config, resolve_stt_local_device

    active_vosk_path = get_active_vosk_path()
    active_whisper = whisper_manager.snapshot()["active"]
    stt_local = system_config_store.get().stt_local
    preprocessing = system_config_store.get().preprocessing
    omnivoice = resolve_omnivoice_config()
    whisper_device_cfg = resolve_stt_local_device("whisper_local")
    data = {
        "app": {"name": settings.app_name, "env": settings.app_env},
        "stt_engines": await stt_service.list_engines(),
        "tts_engines": [{"engine": name} for name in tts_service.providers],
        "tts": {
            "omnivoice_path": omnivoice.omnivoice_path,
            "omnivoice_present": os.path.isdir(omnivoice.omnivoice_path),
        },
        "whisper_local": {
            "active_model": active_whisper,
            "device": whisper_device_cfg["device"],
            "cached": whisper_manager._cached(active_whisper),
        },
```
(the rest of `data` is unchanged)

- `_mask_system_config`: drop the `remote_stt` masking block (those keys no longer exist on `SystemConfig.model_dump()`):
```python
def _mask_system_config(config: SystemConfig) -> dict:
    data = config.model_dump()
    if data["preprocessing"].get("pyannote_auth_token"):
        data["preprocessing"]["pyannote_auth_token"] = "***"
    return data
```

- `_merge_system_config`: drop the `remote_stt` keep-if-blank block:
```python
def _merge_system_config(current: SystemConfig, payload: SystemConfig) -> SystemConfig:
    """Blank or '***' in an incoming secret field means "keep the existing value" --
    the UI never re-sends a real secret it fetched, only a fresh one the user typed."""
    update = payload.model_dump()

    def _keep_if_blank_or_masked(new_value: str, old_value: str) -> str:
        return old_value if (not new_value or new_value == "***") else new_value

    update["preprocessing"]["pyannote_auth_token"] = _keep_if_blank_or_masked(
        update["preprocessing"]["pyannote_auth_token"],
        current.preprocessing.pyannote_auth_token,
    )
    return SystemConfig.model_validate(update)
```

- `set_system_config`: drop the now-dead `qwen3_asr_device`/`remote_stt`/`omnivoice` reinit-trigger blocks (those fields/groups don't exist on `SystemConfig` anymore — updates to them now flow through `PATCH /v1/model_registry/{id}` instead, handled in that route, see below):
```python
    merged = _merge_system_config(current, payload)
    new_config = system_config_store.set(merged)
    if (
        current.preprocessing.pyannote_vad_model != new_config.preprocessing.pyannote_vad_model
        or current.preprocessing.pyannote_auth_token != new_config.preprocessing.pyannote_auth_token
    ):
        from app.services.vad import clear_pyannote_cache

        clear_pyannote_cache()
    return {"success": True, "data": _mask_system_config(new_config)}
```

In `apps/api_gateway/app/api/routes/model_registry.py`:

- Add `config` to both request schemas and allow `base_url` for `kind == "stt"` too:
```python
class CreateEntryRequest(BaseModel):
    kind: str
    engine: str
    model_id: str
    label: str
    stage: str = "stable"
    base_url: str = ""
    api_key: str = ""
    config: dict = {}
    sample_text: str = "xin chào"


class UpdateEntryRequest(BaseModel):
    enabled: bool | None = None
    stage: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    config: dict | None = None
```

- In `create_entry`, allow `base_url` for stt too and pass `config` through:
```python
    created = await model_registry_store.create(
        payload.kind, payload.engine, payload.model_id, payload.label, stage=payload.stage,
        api_key=payload.api_key,
        base_url=payload.base_url if payload.kind in ("llm", "stt") else "",
        config=payload.config,
    )
```

- In `update_entry`, after the existing `set_fields` call, add the reinit side-effects that used to live in `system.py`'s `set_system_config` (moved here since these fields are now edited via this route, not `/v1/system/config`):
```python
@router.patch("/{entry_id}")
async def update_entry(entry_id: str, payload: UpdateEntryRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "api_key" in fields and not fields["api_key"]:
        del fields["api_key"]
    updated = await model_registry_store.set_fields(entry_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"model registry entry '{entry_id}' not found")

    if updated["kind"] == "stt" and updated["engine"] in ("whisper_service", "eventlab"):
        stt_service.reinit_remote_providers()
    elif updated["kind"] == "stt" and updated["engine"] == "qwen3_asr" and "config" in fields:
        from app.services.stt.providers.qwen3_asr_provider import clear_model_cache

        clear_model_cache()
    elif updated["kind"] == "tts" and updated["engine"] == "omnivoice":
        from app.services.tts.providers.omnivoice_provider import reset_voice_ref_and_respawn

        reset_voice_ref_and_respawn()

    updated["api_key"] = _mask_api_key(updated["api_key"])
    return {"success": True, "data": updated}
```

In `apps/api_gateway/app/services/model_registry/store.py`, confirm `create()` already accepts `config: dict | None = None` (it does, per the store's current signature) — no change needed there.

Finally, fix `tests/unit/test_model_registry_seed_migration.py` (Task 3's tests): its setup blocks call `system_config_store.get().remote_stt`/`.omnivoice`, which no longer exist. Rewrite each `system_config_store.set(...)` setup block to write the raw group directly instead, e.g.:
```python
import json

from app.services.db.config_models import SystemRow
from app.services.db.sync_engine import session_scope


def _set_raw_group(group: str, values: dict) -> None:
    with session_scope() as s:
        row = s.get(SystemRow, 1)
        data = json.loads(row.data)
        data[group] = {**data.get(group, {}), **values}
        row.data = json.dumps(data)
    system_config_store._cache = None  # force reload from the DB row


@pytest.mark.asyncio
async def test_migrate_remote_stt_seeds_from_existing_config():
    _set_raw_group("remote_stt", {
        "whisper_service_base_url": "https://api.example.com",
        "whisper_service_api_key": "sk-old",
        "whisper_service_model": "whisper-1",
    })
    await migrate_remote_stt_to_registry()
    entry = await model_registry_store.find_enabled("stt", "whisper_service")
    assert entry is not None
    assert entry["base_url"] == "https://api.example.com"
```
Apply the same `_set_raw_group` rewrite to the other three migration tests in that file (`stt_local`, `omnivoice`) — same shape, different group name and keys.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py tests/unit/test_model_registry_routes.py tests/unit/test_model_registry_seed_migration.py -v`
Expected: all PASS

Run the full unit suite once to catch anything else referencing the removed fields:
```bash
.venv/bin/pytest tests/unit -q -k "not test_conversation_engine_ready"
```
Expected: no `AttributeError`/`KeyError` referencing `omnivoice`, `remote_stt`, `whisper_local_device`, `whisper_local_compute_type`, or `qwen3_asr_device`. Fix any that surface inline (this is expected to catch 1-2 more stragglers beyond what was grepped in Step 1 — the earlier grep in this plan's design phase only searched a subset of directories).

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/system_config.py apps/api_gateway/app/api/routes/system.py apps/api_gateway/app/api/routes/model_registry.py tests/unit/test_system_config_store.py tests/unit/test_system_config_routes.py tests/unit/test_model_registry_routes.py tests/unit/test_model_registry_seed_migration.py
git commit -m "refactor(system-config): remove stt_local device fields + omnivoice/remote_stt groups"
```

---

### Task 8: Admin UI — Model Registry table/form + remove the 3 System settings cards

**Files:**
- Modify: `apps/api_gateway/app/static/js/model-registry.js`
- Modify: `apps/api_gateway/app/static/js/system-config.js`
- Modify: `apps/api_gateway/app/static/index.html`

**Interfaces:**
- Consumes: `POST /v1/model_registry` / `PATCH /v1/model_registry/{id}` now accept `config` and (for `kind=stt`) `base_url` (Task 7).

- [ ] **Step 1: Manual verification plan (no automated frontend tests in this repo — check `tests/` for any existing JS test runner before assuming there is none)**

```bash
find tests -iname "*.test.js" -o -iname "*playwright*" -o -iname "*puppeteer*"
```
If nothing turns up, this task is verified manually per Step 2 below (matches this repo's existing convention for the JS layer — see `docs/superpowers/plans/2026-07-14-profile-llm-registry-select.md` Step 7 for the established manual-check pattern in this codebase).

- [ ] **Step 2: Implement the UI changes**

In `apps/api_gateway/app/static/js/model-registry.js`:

- Un-gate `base_url` for `kind === "stt"` too (table render):
```javascript
      {
        key: "base_url",
        label: "Base URL",
        render: (e) =>
          e.kind === "llm" || e.kind === "stt"
            ? `<input type="text" class="mini" data-registry-baseurl="${escapeHtml(e.id)}"
                 value="${escapeHtml(e.base_url || "")}" placeholder="https://…" />`
            : "—",
      },
```

- Add a `config` column right after `base_url`, rendered as a small JSON textarea (v1: plain JSON editing, no bespoke per-field form — see spec's "Out of scope"):
```javascript
      {
        key: "config",
        label: "Config",
        render: (e) =>
          Object.keys(e.config || {}).length
            ? `<textarea class="mini" rows="2" data-registry-config="${escapeHtml(e.id)}">${escapeHtml(JSON.stringify(e.config))}</textarea>`
            : `<textarea class="mini" rows="2" data-registry-config="${escapeHtml(e.id)}" placeholder="{}"></textarea>`,
      },
```
(insert this object into the `columns: [...]` array right after the existing `base_url` entry, before `actions`)

- Wire the new textarea's change handler alongside the existing `data-registry-baseurl`/`data-registry-apikey` handlers in `renderModelRegistry()`:
```javascript
  table.querySelectorAll("[data-registry-config]").forEach((textarea) =>
    textarea.addEventListener("change", () => {
      let parsed;
      try {
        parsed = textarea.value.trim() ? JSON.parse(textarea.value) : {};
      } catch {
        print(el("model-registry-status"), "Config must be valid JSON", true);
        return;
      }
      patchEntry(textarea.getAttribute("data-registry-config"), { config: parsed });
    })
  );
```

- Update `_updateKindFields` and `createModelRegistryEntry` so `kind === "stt"` also shows/sends `base_url` (currently only `kind === "llm"` does):
```javascript
function _updateKindFields() {
  const kind = el("registry-add-kind").value;
  el("registry-add-llm-fields").classList.toggle("hidden", !(kind === "llm" || kind === "stt"));
  el("registry-add-key-fields").classList.toggle("hidden", kind === "llm" || kind === "stt");
}
```
```javascript
  const payload = { kind, engine, model_id: modelId, label, stage };
  if (kind === "llm" || kind === "stt") {
    payload.base_url = el("registry-add-base-url").value.trim();
    payload.api_key = el("registry-add-api-key").value.trim();
  } else {
    payload.api_key = el("registry-add-key-api-key").value.trim();
  }
```
Check `index.html`'s `#registry-add-llm-fields` block label — if it says "Base URL (LLM only)" or similar, update the copy since it's now also used for stt.

In `apps/api_gateway/app/static/js/system-config.js`, remove the two groups from `GROUPS`:
```javascript
const GROUPS = [
  { key: "engines", label: "Engine Defaults", open: true },
  { key: "stt_local", label: "STT (Local Models)", open: false },
  { key: "conversation", label: "Conversation Tuning", open: false },
  { key: "preprocessing", label: "Preprocessing (VAD/Noise)", open: false },
];
```
(removed `omnivoice` and `remote_stt` entries; `SECRET_FIELDS` no longer needs `remote_stt.whisper_service_api_key`/`remote_stt.eventlab_api_key` — update that set too:
```javascript
const SECRET_FIELDS = new Set([
  "preprocessing.pyannote_auth_token",
]);
```
)

In `apps/api_gateway/app/static/index.html`, find and remove the `<h3 class="sub">OmniVoice</h3>` block (around line 678, per this plan's earlier exploration) and any static markup specific to the `remote_stt`/`omnivoice` system-config cards — check whether these groups render fully dynamically from `GROUPS`/`renderGroupFields` (in which case there's no static HTML to remove beyond what's already handled by the `GROUPS` array change above) or whether `index.html` has hand-written fields for them (the `#omni-name` input seen earlier suggests at least some hand-written OmniVoice markup exists separately from the generic system-config group renderer — read the surrounding HTML block first to determine its actual purpose (it may be the *download/select omnivoice model* UI under `/v1/models/omnivoice/*`, which is unrelated to this migration and must NOT be removed) before deleting anything.

- [ ] **Step 3: Manual browser verification**

```bash
# Follow this repo's existing app-launch convention -- check for a project
# skill or README dev-server instructions before improvising a command.
```
1. Open the admin UI, go to System settings — confirm "STT (Local Models)" card still shows (shrunk: no device/compute_type fields), "OmniVoice (TTS)" and "Remote STT Providers" cards are gone.
2. Go to Model Registry, add a `kind=stt, engine=whisper_service` entry with a `base_url` and `api_key` — confirm it saves and the table shows the base_url.
3. Edit its `config` cell to `{"timeout_seconds": 30}`, confirm it saves (PATCH succeeds, no console error).
4. Restart the app (or hit the relevant reload path) and confirm STT/TTS still function with the migrated values (no crash on boot from the removed SystemConfig fields).

- [ ] **Step 4: Commit**

```bash
git add apps/api_gateway/app/static/js/model-registry.js apps/api_gateway/app/static/js/system-config.js apps/api_gateway/app/static/index.html
git commit -m "feat(ui): edit STT/TTS Model Registry base_url + config, remove migrated System settings cards"
```

---

### Task 9: Docs

**Files:**
- Modify: `docs/api.md`

- [ ] **Step 1: Write the implementation**

Update `docs/api.md`'s description of `/v1/system/config` and `/v1/model_registry` to reflect: `remote_stt`/`omnivoice` groups and `stt_local`'s device/compute_type fields are gone from `SystemConfig`; those now live as `kind="stt"`/`kind="tts"` Model Registry entries (`base_url`/`api_key`/`config`). Follow the existing doc's structure/tone — read the current section before rewriting it.

- [ ] **Step 2: Commit**

```bash
git add docs/api.md
git commit -m "docs: describe STT/TTS Model Registry entries, remove stale SystemConfig field docs"
```
