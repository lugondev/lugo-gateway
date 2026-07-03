# LLM Profiles & MCP HTTP Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add named LLM profiles (each bundling endpoint + system prompt + TTS voice + per-profile MCP servers) and wire up MCP HTTP transport — profiles activate per-session via `?profile=<name>`.

**Architecture:** `ProfileStore` and `McpServerStore` persist JSON with atomic writes; `McpConnectionPool` lazy-connects to MCP HTTP servers per URL, caches tool definitions with TTL, and shares connections across sessions. Conversation `?profile=<name>` resolves the profile's config, merges global + per-profile MCP servers, builds a `ToolRegistry`, and constructs the responder with profile overrides.

**Tech Stack:** FastAPI, Pydantic v2, httpx (already in deps), pytest + pytest-asyncio (asyncio_mode = "auto"), `unittest.mock` for HTTP mocking.

## Global Constraints

- Python ≥ 3.10; project uses 3.12 locally.
- `pythonpath = ["apps/api_gateway"]` — imports are from that root.
- `asyncio_mode = "auto"` — all async tests run without `@pytest.mark.asyncio`.
- Run tests: `pytest tests/ -x -q` from project root.
- Atomic file write: write to `.tmp` then `Path.replace()`.
- No new PyPI deps — use `httpx` (already in deps) and `unittest.mock`.
- Spec: `docs/superpowers/specs/2026-07-01-llm-profiles-mcp-design.md`

---

## File Map

**New files:**
- `apps/api_gateway/app/services/profiles/__init__.py`
- `apps/api_gateway/app/services/profiles/models.py` — Pydantic models: `LlmConfig`, `TtsConfig`, `McpServer`, `Profile`
- `apps/api_gateway/app/services/profiles/store.py` — `ProfileStore` + `profile_store` singleton
- `apps/api_gateway/app/services/mcp/__init__.py`
- `apps/api_gateway/app/services/mcp/models.py` — `McpServer` (shared between profiles + global store)
- `apps/api_gateway/app/services/mcp/server_store.py` — `McpServerStore` + `mcp_server_store` singleton
- `apps/api_gateway/app/services/mcp/client.py` — `McpHttpClient`, `McpConnectionError`
- `apps/api_gateway/app/services/mcp/pool.py` — `McpConnectionPool` + `mcp_pool` singleton
- `apps/api_gateway/app/api/routes/profiles.py` — Profile CRUD REST routes
- `apps/api_gateway/app/api/routes/mcp.py` — Global MCP server CRUD + tool-discover routes
- `tests/unit/test_profiles_store.py`
- `tests/unit/test_mcp_server_store.py`
- `tests/unit/test_mcp_client.py`
- `tests/unit/test_mcp_pool.py`
- `tests/unit/test_profiles_routes.py`
- `tests/unit/test_mcp_routes.py`

**Modified files:**
- `apps/api_gateway/app/core/settings.py` — add 7 new fields
- `apps/api_gateway/app/services/conversation/responder.py` — add `build_responder_ex()`
- `apps/api_gateway/app/api/routes/conversation.py` — profile resolution + MCP wiring + fix audio-path tool_registry bug
- `apps/api_gateway/app/main.py` — register profiles + mcp routers

---

## Task 1: Settings + Data Models

**Files:**
- Modify: `apps/api_gateway/app/core/settings.py`
- Create: `apps/api_gateway/app/services/profiles/__init__.py`
- Create: `apps/api_gateway/app/services/profiles/models.py`
- Create: `apps/api_gateway/app/services/mcp/__init__.py`
- Create: `apps/api_gateway/app/services/mcp/models.py`
- Test: `tests/unit/test_profiles_models.py`

**Interfaces:**
- Produces:
  - `settings.profiles_path: str`
  - `settings.mcp_servers_path: str`
  - `settings.mcp_tool_cache_ttl_seconds: int`
  - `settings.mcp_connection_timeout_seconds: float`
  - `settings.mcp_tool_timeout_seconds: float`
  - `settings.conversation_tools_enabled: bool`
  - `settings.conversation_tool_max_iters: int`
  - `McpServer(name: str, url: str)` from `app.services.mcp.models`
  - `LlmConfig(base_url: str, api_key: str, model: str)` from `app.services.profiles.models`
  - `TtsConfig(engine: str, voice: str)` from `app.services.profiles.models`
  - `Profile(name, llm, system_prompt, tts, mcp_servers)` from `app.services.profiles.models`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_profiles_models.py
from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, Profile, TtsConfig


def test_profile_defaults():
    p = Profile(name="x")
    assert p.llm.base_url == ""
    assert p.tts.engine == ""
    assert p.mcp_servers == []
    assert p.system_prompt == ""


def test_profile_full():
    p = Profile(
        name="home",
        llm=LlmConfig(base_url="http://localhost:11434/v1", api_key="", model="llama3.2"),
        system_prompt="You are a home assistant.",
        tts=TtsConfig(engine="vieneu", voice=""),
        mcp_servers=[McpServer(name="ha", url="http://localhost:3001/mcp")],
    )
    assert p.name == "home"
    assert p.llm.model == "llama3.2"
    assert len(p.mcp_servers) == 1


def test_mcpserver_model():
    s = McpServer(name="fs", url="http://localhost:3002/mcp")
    assert s.name == "fs"
    assert s.url == "http://localhost:3002/mcp"


def test_profile_roundtrip():
    p = Profile(name="test", system_prompt="hello")
    data = p.model_dump()
    p2 = Profile.model_validate(data)
    assert p2.system_prompt == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_profiles_models.py -v
```
Expected: `ModuleNotFoundError: No module named 'app.services.mcp'`

- [ ] **Step 3: Create `app/services/mcp/models.py`**

```python
# apps/api_gateway/app/services/mcp/models.py
from pydantic import BaseModel


class McpServer(BaseModel):
    name: str
    url: str
```

- [ ] **Step 4: Create `app/services/mcp/__init__.py`**

```python
# apps/api_gateway/app/services/mcp/__init__.py
```
(empty file)

- [ ] **Step 5: Create `app/services/profiles/models.py`**

```python
# apps/api_gateway/app/services/profiles/models.py
from __future__ import annotations

from pydantic import BaseModel

from app.services.mcp.models import McpServer


class LlmConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class TtsConfig(BaseModel):
    engine: str = ""
    voice: str = ""


class Profile(BaseModel):
    name: str
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
```

- [ ] **Step 6: Create `app/services/profiles/__init__.py`**

```python
# apps/api_gateway/app/services/profiles/__init__.py
```
(empty file)

- [ ] **Step 7: Add new settings fields to `settings.py`**

Add after `remote_stt_timeout_seconds` (before `model_config`):

```python
    # LLM profiles + MCP tooling
    profiles_path: str = "profiles.json"
    mcp_servers_path: str = "mcp_servers.json"
    mcp_tool_cache_ttl_seconds: int = 300
    mcp_connection_timeout_seconds: float = 10.0
    mcp_tool_timeout_seconds: float = 30.0
    # Function-calling / tool use
    conversation_tools_enabled: bool = False
    conversation_tool_max_iters: int = 3
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
pytest tests/unit/test_profiles_models.py -v
```
Expected: 4 tests PASS

- [ ] **Step 9: Commit**

```bash
git add apps/api_gateway/app/services/mcp/__init__.py \
        apps/api_gateway/app/services/mcp/models.py \
        apps/api_gateway/app/services/profiles/__init__.py \
        apps/api_gateway/app/services/profiles/models.py \
        apps/api_gateway/app/core/settings.py \
        tests/unit/test_profiles_models.py
git commit -m "feat(profiles): data models + settings for LLM profiles and MCP"
```

---

## Task 2: ProfileStore

**Files:**
- Create: `apps/api_gateway/app/services/profiles/store.py`
- Test: `tests/unit/test_profiles_store.py`

**Interfaces:**
- Consumes: `Profile` from `app.services.profiles.models`, `settings.profiles_path`
- Produces:
  - `profile_store: ProfileStore` singleton from `app.services.profiles.store`
  - `ProfileStore.list() -> dict[str, Profile]`
  - `ProfileStore.get(name: str) -> Profile | None`
  - `ProfileStore.upsert(profile: Profile) -> None`
  - `ProfileStore.delete(name: str) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_profiles_store.py
import pytest

from app.services.profiles.models import LlmConfig, Profile, TtsConfig
from app.services.profiles.store import ProfileStore


@pytest.fixture
def store(tmp_path):
    return ProfileStore(str(tmp_path / "profiles.json"))


def test_empty_store_returns_empty_dict(store):
    assert store.list() == {}


def test_upsert_and_get(store):
    p = Profile(name="test", system_prompt="Hello")
    store.upsert(p)
    result = store.get("test")
    assert result is not None
    assert result.system_prompt == "Hello"


def test_get_missing_returns_none(store):
    assert store.get("nonexistent") is None


def test_list_multiple_profiles(store):
    store.upsert(Profile(name="a"))
    store.upsert(Profile(name="b"))
    profiles = store.list()
    assert set(profiles.keys()) == {"a", "b"}


def test_upsert_overwrites_existing(store):
    store.upsert(Profile(name="x", system_prompt="old"))
    store.upsert(Profile(name="x", system_prompt="new"))
    assert store.get("x").system_prompt == "new"


def test_delete_removes_profile(store):
    store.upsert(Profile(name="del"))
    store.delete("del")
    assert store.get("del") is None


def test_delete_nonexistent_is_noop(store):
    store.delete("ghost")  # should not raise


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "profiles.json")
    s1 = ProfileStore(path)
    s1.upsert(Profile(name="persist", system_prompt="stay"))
    s2 = ProfileStore(path)
    assert s2.get("persist").system_prompt == "stay"


def test_auto_creates_file(tmp_path):
    store = ProfileStore(str(tmp_path / "new.json"))
    assert store.list() == {}


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

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_profiles_store.py -v
```
Expected: `ModuleNotFoundError: No module named 'app.services.profiles.store'`

- [ ] **Step 3: Implement `ProfileStore`**

```python
# apps/api_gateway/app/services/profiles/store.py
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.core.settings import settings
from app.services.profiles.models import Profile


class ProfileStore:
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

    def list(self) -> dict[str, Profile]:
        with self._lock:
            return {k: Profile.model_validate(v) for k, v in self._read().items()}

    def get(self, name: str) -> Profile | None:
        return self.list().get(name)

    def upsert(self, profile: Profile) -> None:
        with self._lock:
            profiles = self._read()
            profiles[profile.name] = profile.model_dump()
            self._write(profiles)

    def delete(self, name: str) -> None:
        with self._lock:
            profiles = self._read()
            profiles.pop(name, None)
            self._write(profiles)


profile_store = ProfileStore(settings.profiles_path)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_profiles_store.py -v
```
Expected: 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/profiles/store.py \
        tests/unit/test_profiles_store.py
git commit -m "feat(profiles): ProfileStore with JSON persistence"
```

---

## Task 3: McpServerStore

**Files:**
- Create: `apps/api_gateway/app/services/mcp/server_store.py`
- Test: `tests/unit/test_mcp_server_store.py`

**Interfaces:**
- Consumes: `McpServer` from `app.services.mcp.models`, `settings.mcp_servers_path`
- Produces:
  - `mcp_server_store: McpServerStore` singleton from `app.services.mcp.server_store`
  - `McpServerStore.list() -> dict[str, McpServer]`
  - `McpServerStore.get(name: str) -> McpServer | None`
  - `McpServerStore.upsert(entry: McpServer) -> None`
  - `McpServerStore.delete(name: str) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_mcp_server_store.py
import pytest

from app.services.mcp.models import McpServer
from app.services.mcp.server_store import McpServerStore


@pytest.fixture
def store(tmp_path):
    return McpServerStore(str(tmp_path / "mcp.json"))


def test_empty_store(store):
    assert store.list() == {}


def test_upsert_and_get(store):
    s = McpServer(name="fs", url="http://localhost:3002/mcp")
    store.upsert(s)
    result = store.get("fs")
    assert result is not None
    assert result.url == "http://localhost:3002/mcp"


def test_get_missing_returns_none(store):
    assert store.get("ghost") is None


def test_list_multiple(store):
    store.upsert(McpServer(name="a", url="http://a"))
    store.upsert(McpServer(name="b", url="http://b"))
    assert set(store.list().keys()) == {"a", "b"}


def test_upsert_overwrites(store):
    store.upsert(McpServer(name="x", url="http://old"))
    store.upsert(McpServer(name="x", url="http://new"))
    assert store.get("x").url == "http://new"


def test_delete(store):
    store.upsert(McpServer(name="del", url="http://del"))
    store.delete("del")
    assert store.get("del") is None


def test_delete_nonexistent_noop(store):
    store.delete("ghost")


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "mcp.json")
    s1 = McpServerStore(path)
    s1.upsert(McpServer(name="p", url="http://persist"))
    s2 = McpServerStore(path)
    assert s2.get("p").url == "http://persist"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_mcp_server_store.py -v
```
Expected: `ModuleNotFoundError: No module named 'app.services.mcp.server_store'`

- [ ] **Step 3: Implement `McpServerStore`**

```python
# apps/api_gateway/app/services/mcp/server_store.py
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.core.settings import settings
from app.services.mcp.models import McpServer


class McpServerStore:
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
            return data.get("servers", {})
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _write(self, servers: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"servers": servers}, indent=2))
        tmp.replace(self._path)

    def list(self) -> dict[str, McpServer]:
        with self._lock:
            return {k: McpServer.model_validate(v) for k, v in self._read().items()}

    def get(self, name: str) -> McpServer | None:
        return self.list().get(name)

    def upsert(self, entry: McpServer) -> None:
        with self._lock:
            servers = self._read()
            servers[entry.name] = entry.model_dump()
            self._write(servers)

    def delete(self, name: str) -> None:
        with self._lock:
            servers = self._read()
            servers.pop(name, None)
            self._write(servers)


mcp_server_store = McpServerStore(settings.mcp_servers_path)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_mcp_server_store.py -v
```
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/mcp/server_store.py \
        tests/unit/test_mcp_server_store.py
git commit -m "feat(mcp): McpServerStore with JSON persistence"
```

---

## Task 4: McpHttpClient

**Files:**
- Create: `apps/api_gateway/app/services/mcp/client.py`
- Test: `tests/unit/test_mcp_client.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient`
- Produces:
  - `McpConnectionError(Exception)` from `app.services.mcp.client`
  - `McpHttpClient(url: str, connect_timeout: float, tool_timeout: float)`
  - `McpHttpClient.list_tools() -> list[dict]` — raises `McpConnectionError` on failure
  - `McpHttpClient.invoke(tool_name: str, arguments: dict) -> str` — raises `McpConnectionError` on failure

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_mcp_client.py
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.mcp.client import McpConnectionError, McpHttpClient


def _make_mock_client(get_response=None, post_response=None):
    """Build a mock async context-manager httpx.AsyncClient."""
    mock = AsyncMock()
    if get_response is not None:
        mock.get = AsyncMock(return_value=get_response)
    if post_response is not None:
        mock.post = AsyncMock(return_value=post_response)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _json_response(data, status=200):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=data)
    resp.status_code = status
    return resp


async def test_list_tools_list_format():
    tools = [{"name": "search", "description": "Search", "inputSchema": {"type": "object"}}]
    mock_client = _make_mock_client(get_response=_json_response(tools))
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        result = await client.list_tools()
    assert len(result) == 1
    assert result[0]["name"] == "search"


async def test_list_tools_dict_format():
    tools = [{"name": "time", "description": "Get time", "inputSchema": {}}]
    mock_client = _make_mock_client(get_response=_json_response({"tools": tools}))
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        result = await client.list_tools()
    assert result[0]["name"] == "time"


async def test_list_tools_strips_trailing_slash():
    mock_client = _make_mock_client(get_response=_json_response([]))
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test/")
        await client.list_tools()
    mock_client.get.assert_called_once_with("http://mcp.test/tools")


async def test_list_tools_connection_error_raises():
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        with pytest.raises(McpConnectionError, match="mcp.test"):
            await client.list_tools()


async def test_invoke_returns_result_string():
    mock_client = _make_mock_client(post_response=_json_response({"result": "found it"}))
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        result = await client.invoke("search", {"query": "hello"})
    assert result == "found it"
    mock_client.post.assert_called_once_with(
        "http://mcp.test/tools/search", json={"arguments": {"query": "hello"}}
    )


async def test_invoke_connection_error_raises():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=mock_client):
        client = McpHttpClient("http://mcp.test")
        with pytest.raises(McpConnectionError):
            await client.invoke("search", {})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_mcp_client.py -v
```
Expected: `ModuleNotFoundError: No module named 'app.services.mcp.client'`

- [ ] **Step 3: Implement `McpHttpClient`**

```python
# apps/api_gateway/app/services/mcp/client.py
from __future__ import annotations

import httpx


class McpConnectionError(Exception):
    pass


class McpHttpClient:
    """Thin async HTTP adapter for a single MCP HTTP server.

    GET  {url}/tools              → list of tool defs
    POST {url}/tools/{name}       → {"result": str}   body: {"arguments": {}}
    """

    def __init__(
        self,
        url: str,
        connect_timeout: float = 10.0,
        tool_timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._tool_timeout = tool_timeout

    async def list_tools(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=self._connect_timeout) as client:
                resp = await client.get(f"{self.url}/tools")
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else data.get("tools", [])
        except Exception as exc:
            raise McpConnectionError(f"Failed to list tools from {self.url}: {exc}") from exc

    async def invoke(self, tool_name: str, arguments: dict) -> str:
        try:
            async with httpx.AsyncClient(timeout=self._tool_timeout) as client:
                resp = await client.post(
                    f"{self.url}/tools/{tool_name}",
                    json={"arguments": arguments},
                )
                resp.raise_for_status()
                data = resp.json()
                return str(data.get("result", data))
        except Exception as exc:
            raise McpConnectionError(
                f"Failed to invoke '{tool_name}' on {self.url}: {exc}"
            ) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_mcp_client.py -v
```
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/mcp/client.py \
        tests/unit/test_mcp_client.py
git commit -m "feat(mcp): McpHttpClient with HTTP/SSE transport adapter"
```

---

## Task 5: McpConnectionPool

**Files:**
- Create: `apps/api_gateway/app/services/mcp/pool.py`
- Test: `tests/unit/test_mcp_pool.py`

**Interfaces:**
- Consumes: `McpHttpClient`, `McpConnectionError` from `app.services.mcp.client`, settings
- Produces:
  - `mcp_pool: McpConnectionPool` singleton from `app.services.mcp.pool`
  - `McpConnectionPool.get_tools(url: str) -> list[dict]` — returns `[]` on error (no raise)
  - `McpConnectionPool.invoke(url: str, tool_name: str, args: dict) -> str` — raises `McpConnectionError`
  - `McpConnectionPool.invalidate(url: str) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_mcp_pool.py
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.services.mcp.pool import McpConnectionPool


TOOLS = [{"name": "search", "description": "Search", "inputSchema": {"type": "object"}}]


def _mock_client(tools=None, invoke_result="ok"):
    client = AsyncMock()
    client.list_tools = AsyncMock(return_value=tools or TOOLS)
    client.invoke = AsyncMock(return_value=invoke_result)
    return client


async def test_get_tools_returns_tool_list():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        tools = await pool.get_tools("http://mcp.test")
    assert tools == TOOLS
    mock.list_tools.assert_called_once()


async def test_get_tools_caches_result():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        await pool.get_tools("http://mcp.test")
        await pool.get_tools("http://mcp.test")
    mock.list_tools.assert_called_once()  # cached on second call


async def test_get_tools_cache_expires():
    pool = McpConnectionPool(cache_ttl=0.01)  # 10ms TTL
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        await pool.get_tools("http://mcp.test")
        time.sleep(0.02)
        await pool.get_tools("http://mcp.test")
    assert mock.list_tools.call_count == 2


async def test_get_tools_returns_empty_on_error():
    from app.services.mcp.client import McpConnectionError
    pool = McpConnectionPool(cache_ttl=60)
    mock = AsyncMock()
    mock.list_tools = AsyncMock(side_effect=McpConnectionError("refused"))
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        tools = await pool.get_tools("http://mcp.test")
    assert tools == []


async def test_invoke_returns_result():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client(invoke_result="search result")
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        result = await pool.invoke("http://mcp.test", "search", {"query": "hello"})
    assert result == "search result"
    mock.invoke.assert_called_once_with("search", {"query": "hello"})


async def test_invalidate_clears_cache():
    pool = McpConnectionPool(cache_ttl=60)
    mock = _mock_client()
    with patch("app.services.mcp.pool.McpHttpClient", return_value=mock):
        await pool.get_tools("http://mcp.test")
        pool.invalidate("http://mcp.test")
        await pool.get_tools("http://mcp.test")
    assert mock.list_tools.call_count == 2


async def test_different_urls_are_independent():
    pool = McpConnectionPool(cache_ttl=60)
    mock_a = _mock_client(tools=[{"name": "a"}])
    mock_b = _mock_client(tools=[{"name": "b"}])
    with patch("app.services.mcp.pool.McpHttpClient", side_effect=[mock_a, mock_b]):
        tools_a = await pool.get_tools("http://a.test")
        tools_b = await pool.get_tools("http://b.test")
    assert tools_a[0]["name"] == "a"
    assert tools_b[0]["name"] == "b"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_mcp_pool.py -v
```
Expected: `ModuleNotFoundError: No module named 'app.services.mcp.pool'`

- [ ] **Step 3: Implement `McpConnectionPool`**

```python
# apps/api_gateway/app/services/mcp/pool.py
from __future__ import annotations

import asyncio
import logging
import time

from app.core.settings import settings
from app.services.mcp.client import McpConnectionError, McpHttpClient

logger = logging.getLogger(__name__)


class McpConnectionPool:
    """Lazy-connecting pool of MCP HTTP clients, one per URL.

    Tool definitions are cached per URL with a configurable TTL to avoid
    re-fetching on every session. Call ``invalidate(url)`` when a server's
    config changes to force a fresh fetch.
    """

    def __init__(
        self,
        cache_ttl: float = 300.0,
        connect_timeout: float = 10.0,
        tool_timeout: float = 30.0,
    ) -> None:
        self._clients: dict[str, McpHttpClient] = {}
        self._cache: dict[str, tuple[float, list[dict]]] = {}  # url -> (timestamp, tools)
        self._ttl = cache_ttl
        self._connect_timeout = connect_timeout
        self._tool_timeout = tool_timeout
        self._lock = asyncio.Lock()

    def _get_client(self, url: str) -> McpHttpClient:
        if url not in self._clients:
            self._clients[url] = McpHttpClient(url, self._connect_timeout, self._tool_timeout)
        return self._clients[url]

    async def get_tools(self, url: str) -> list[dict]:
        async with self._lock:
            cached = self._cache.get(url)
            if cached and (time.monotonic() - cached[0]) < self._ttl:
                return cached[1]

        try:
            client = self._get_client(url)
            tools = await client.list_tools()
            async with self._lock:
                self._cache[url] = (time.monotonic(), tools)
            return tools
        except McpConnectionError as exc:
            logger.warning("MCP server %s unreachable: %s", url, exc)
            return []

    async def invoke(self, url: str, tool_name: str, args: dict) -> str:
        client = self._get_client(url)
        return await client.invoke(tool_name, args)

    def invalidate(self, url: str) -> None:
        self._cache.pop(url, None)
        self._clients.pop(url, None)


mcp_pool = McpConnectionPool(
    cache_ttl=settings.mcp_tool_cache_ttl_seconds,
    connect_timeout=settings.mcp_connection_timeout_seconds,
    tool_timeout=settings.mcp_tool_timeout_seconds,
)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_mcp_pool.py -v
```
Expected: 7 tests PASS

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
pytest tests/ -x -q --ignore=tests/integration
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/mcp/pool.py \
        tests/unit/test_mcp_pool.py
git commit -m "feat(mcp): McpConnectionPool with lazy connect and TTL cache"
```

---

## Task 6: Profile CRUD Routes + responder extension

**Files:**
- Create: `apps/api_gateway/app/api/routes/profiles.py`
- Modify: `apps/api_gateway/app/services/conversation/responder.py` — add `build_responder_ex()`
- Modify: `apps/api_gateway/app/main.py` — register profiles router
- Test: `tests/unit/test_profiles_routes.py`

**Interfaces:**
- Consumes: `profile_store`, `Profile`, `LlmConfig`, `TtsConfig` from profiles modules
- Produces:
  - `GET /v1/profiles` → `{"success": true, "data": {name: profile_dict}}`
  - `POST /v1/profiles` → `{"success": true, "data": profile_dict}`
  - `GET /v1/profiles/{name}` → `{"success": true, "data": profile_dict}` or 404
  - `PUT /v1/profiles/{name}` → `{"success": true, "data": profile_dict}`
  - `DELETE /v1/profiles/{name}` → `{"success": true, "data": {"name": ..., "deleted": true}}`
  - `build_responder_ex(base_url, api_key, model, system_prompt) -> Responder` from `app.services.conversation.responder`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_profiles_routes.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.profiles.store import ProfileStore, profile_store


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.profiles.profile_store", fresh)
    monkeypatch.setattr("app.services.profiles.store.profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


def test_list_profiles_empty(client):
    resp = client.get("/v1/profiles")
    assert resp.status_code == 200
    assert resp.json()["data"] == {}


def test_create_profile(client):
    payload = {
        "name": "test",
        "system_prompt": "Be brief.",
        "llm": {"base_url": "http://localhost:11434/v1", "api_key": "", "model": "llama3.2"},
        "tts": {"engine": "vieneu", "voice": ""},
        "mcp_servers": [],
    }
    resp = client.post("/v1/profiles", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == "test"
    assert data["system_prompt"] == "Be brief."


def test_get_profile(client):
    client.post("/v1/profiles", json={"name": "x", "system_prompt": "hello"})
    resp = client.get("/v1/profiles/x")
    assert resp.status_code == 200
    assert resp.json()["data"]["system_prompt"] == "hello"


def test_get_missing_profile_404(client):
    resp = client.get("/v1/profiles/ghost")
    assert resp.status_code == 404


def test_update_profile(client):
    client.post("/v1/profiles", json={"name": "upd", "system_prompt": "old"})
    resp = client.put("/v1/profiles/upd", json={"name": "upd", "system_prompt": "new"})
    assert resp.status_code == 200
    assert resp.json()["data"]["system_prompt"] == "new"


def test_update_uses_path_name(client):
    # path param wins over body name
    resp = client.put("/v1/profiles/canonical", json={"name": "ignored", "system_prompt": "x"})
    assert resp.json()["data"]["name"] == "canonical"


def test_delete_profile(client):
    client.post("/v1/profiles", json={"name": "del"})
    resp = client.delete("/v1/profiles/del")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    assert client.get("/v1/profiles/del").status_code == 404


def test_list_shows_created_profile(client):
    client.post("/v1/profiles", json={"name": "visible"})
    resp = client.get("/v1/profiles")
    assert "visible" in resp.json()["data"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_profiles_routes.py -v
```
Expected: `ImportError` or route not found errors

- [ ] **Step 3: Create `api/routes/profiles.py`**

```python
# apps/api_gateway/app/api/routes/profiles.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, Profile, TtsConfig
from app.services.profiles.store import profile_store

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])


class ProfileRequest(BaseModel):
    name: str
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []


@router.get("")
async def list_profiles() -> dict:
    profiles = profile_store.list()
    return {"success": True, "data": {k: v.model_dump() for k, v in profiles.items()}}


@router.post("")
async def create_profile(payload: ProfileRequest) -> dict:
    profile = Profile(**payload.model_dump())
    profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


@router.get("/{name}")
async def get_profile(name: str) -> dict:
    profile = profile_store.get(name)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return {"success": True, "data": profile.model_dump()}


@router.put("/{name}")
async def update_profile(name: str, payload: ProfileRequest) -> dict:
    data = payload.model_dump()
    data["name"] = name
    profile = Profile(**data)
    profile_store.upsert(profile)
    return {"success": True, "data": profile.model_dump()}


@router.delete("/{name}")
async def delete_profile(name: str) -> dict:
    profile_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}
```

- [ ] **Step 4: Add `build_responder_ex` to `responder.py`**

Add after `build_responder()` (after line 248 in `responder.py`):

```python
def build_responder_ex(
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> Responder:
    """Build a responder with optional overrides; falls back to .env defaults.

    Passing None for any arg uses the current global active config value.
    """
    effective_url = base_url if base_url is not None else get_active_llm_base_url()
    if effective_url:
        return OpenAICompatResponder(
            base_url=effective_url,
            api_key=api_key if api_key is not None else get_active_llm_api_key(),
            model=model if model is not None else get_active_llm_model(),
            system_prompt=(
                system_prompt if system_prompt is not None else settings.conversation_system_prompt
            ),
            timeout=settings.conversation_llm_timeout_seconds,
        )
    return EchoResponder()
```

- [ ] **Step 5: Register profiles router in `main.py`**

Add after the existing imports:
```python
from app.api.routes.profiles import router as profiles_router
```

Add after `app.include_router(agents_docs_router)`:
```python
app.include_router(profiles_router)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
pytest tests/unit/test_profiles_routes.py -v
```
Expected: 9 tests PASS

- [ ] **Step 7: Run full test suite**

```bash
pytest tests/ -x -q --ignore=tests/integration
```
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/api/routes/profiles.py \
        apps/api_gateway/app/services/conversation/responder.py \
        apps/api_gateway/app/main.py \
        tests/unit/test_profiles_routes.py
git commit -m "feat(profiles): Profile CRUD REST routes + build_responder_ex"
```

---

## Task 7: MCP Server Routes

**Files:**
- Create: `apps/api_gateway/app/api/routes/mcp.py`
- Modify: `apps/api_gateway/app/main.py` — register mcp router
- Test: `tests/unit/test_mcp_routes.py`

**Interfaces:**
- Consumes: `mcp_server_store`, `mcp_pool`, `McpServer`
- Produces:
  - `GET /v1/mcp/servers` → `{"success": true, "data": {name: server_dict}}`
  - `POST /v1/mcp/servers` → `{"success": true, "data": server_dict}`
  - `GET /v1/mcp/servers/{name}` → server or 404
  - `PUT /v1/mcp/servers/{name}` → updated server (invalidates pool cache)
  - `DELETE /v1/mcp/servers/{name}` → deleted (invalidates pool cache)
  - `GET /v1/mcp/servers/{name}/tools` → live tool list from server

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_mcp_routes.py
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.mcp.models import McpServer
from app.services.mcp.server_store import McpServerStore, mcp_server_store


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = McpServerStore(str(tmp_path / "mcp.json"))
    monkeypatch.setattr("app.api.routes.mcp.mcp_server_store", fresh)
    monkeypatch.setattr("app.services.mcp.server_store.mcp_server_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


def test_list_servers_empty(client):
    resp = client.get("/v1/mcp/servers")
    assert resp.status_code == 200
    assert resp.json()["data"] == {}


def test_add_server(client):
    resp = client.post("/v1/mcp/servers", json={"name": "fs", "url": "http://localhost:3002/mcp"})
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "fs"


def test_get_server(client):
    client.post("/v1/mcp/servers", json={"name": "ws", "url": "http://ws"})
    resp = client.get("/v1/mcp/servers/ws")
    assert resp.status_code == 200
    assert resp.json()["data"]["url"] == "http://ws"


def test_get_missing_server_404(client):
    assert client.get("/v1/mcp/servers/ghost").status_code == 404


def test_update_server_invalidates_cache(client, monkeypatch):
    invalidated = []
    monkeypatch.setattr("app.api.routes.mcp.mcp_pool.invalidate", lambda u: invalidated.append(u))
    client.post("/v1/mcp/servers", json={"name": "x", "url": "http://old"})
    client.put("/v1/mcp/servers/x", json={"name": "x", "url": "http://new"})
    assert "http://old" in invalidated


def test_delete_server(client):
    client.post("/v1/mcp/servers", json={"name": "del", "url": "http://del"})
    resp = client.delete("/v1/mcp/servers/del")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    assert client.get("/v1/mcp/servers/del").status_code == 404


def test_list_server_tools(client, monkeypatch):
    client.post("/v1/mcp/servers", json={"name": "tool-srv", "url": "http://tool-srv"})
    tools = [{"name": "search", "description": "Search"}]
    mock_pool = AsyncMock()
    mock_pool.get_tools = AsyncMock(return_value=tools)
    mock_pool.invalidate = lambda u: None
    monkeypatch.setattr("app.api.routes.mcp.mcp_pool", mock_pool)
    resp = client.get("/v1/mcp/servers/tool-srv/tools")
    assert resp.status_code == 200
    assert resp.json()["data"]["tools"] == tools


def test_list_tools_missing_server_404(client):
    resp = client.get("/v1/mcp/servers/ghost/tools")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_mcp_routes.py -v
```
Expected: route not found errors

- [ ] **Step 3: Create `api/routes/mcp.py`**

```python
# apps/api_gateway/app/api/routes/mcp.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.mcp.models import McpServer
from app.services.mcp.pool import mcp_pool
from app.services.mcp.server_store import mcp_server_store

router = APIRouter(prefix="/v1/mcp", tags=["mcp"])


class McpServerRequest(BaseModel):
    name: str
    url: str


@router.get("/servers")
async def list_servers() -> dict:
    servers = mcp_server_store.list()
    return {"success": True, "data": {k: v.model_dump() for k, v in servers.items()}}


@router.post("/servers")
async def add_server(payload: McpServerRequest) -> dict:
    entry = McpServer(name=payload.name, url=payload.url)
    mcp_server_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.get("/servers/{name}")
async def get_server(name: str) -> dict:
    entry = mcp_server_store.get(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return {"success": True, "data": entry.model_dump()}


@router.put("/servers/{name}")
async def update_server(name: str, payload: McpServerRequest) -> dict:
    old = mcp_server_store.get(name)
    if old:
        mcp_pool.invalidate(old.url)
    entry = McpServer(name=name, url=payload.url)
    mcp_server_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.delete("/servers/{name}")
async def delete_server(name: str) -> dict:
    entry = mcp_server_store.get(name)
    if entry:
        mcp_pool.invalidate(entry.url)
    mcp_server_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}


@router.get("/servers/{name}/tools")
async def list_server_tools(name: str) -> dict:
    entry = mcp_server_store.get(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    mcp_pool.invalidate(entry.url)
    tools = await mcp_pool.get_tools(entry.url)
    return {"success": True, "data": {"server": name, "url": entry.url, "tools": tools}}
```

- [ ] **Step 4: Register mcp router in `main.py`**

Add import:
```python
from app.api.routes.mcp import router as mcp_router
```

Add after `app.include_router(profiles_router)`:
```python
app.include_router(mcp_router)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/unit/test_mcp_routes.py -v
```
Expected: 8 tests PASS

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/ -x -q --ignore=tests/integration
```
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/api/routes/mcp.py \
        apps/api_gateway/app/main.py \
        tests/unit/test_mcp_routes.py
git commit -m "feat(mcp): MCP server CRUD routes with live tool discovery"
```

---

## Task 8: Conversation Wiring

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py`
- Test: `tests/unit/test_conversation_profile.py`

**Interfaces:**
- Consumes:
  - `profile_store: ProfileStore` from `app.services.profiles.store`
  - `mcp_server_store: McpServerStore` from `app.services.mcp.server_store`
  - `mcp_pool: McpConnectionPool` from `app.services.mcp.pool`
  - `McpToolSource` from `app.services.conversation.tools.mcp`
  - `build_responder_ex()` from `app.services.conversation.responder`
- Produces:
  - `?profile=<name>` on WS and REST chat resolves a profile
  - Unknown profile emits `{"event": "warning", ...}` then continues with defaults
  - `session_started` includes `"profile"` and `"active_tools"` fields
  - Audio-path tool_registry bug fixed (tool calls work in audio turns)

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_conversation_profile.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.mcp.models import McpServer
from app.services.mcp.server_store import McpServerStore
from app.services.profiles.models import LlmConfig, Profile, TtsConfig
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "omnivoice_use_server", False)

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_servers = McpServerStore(str(tmp_path / "mcp.json"))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)
    monkeypatch.setattr("app.api.routes.conversation.mcp_server_store", fresh_servers)
    return fresh_profiles, fresh_servers


@pytest.fixture
def client():
    return TestClient(app)


def test_chat_without_profile_uses_echo(client):
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.json()["data"]["responder"] == "echo"


def test_chat_unknown_profile_falls_back(client, monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    resp = client.post(
        "/v1/conversation/chat?profile=ghost",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["responder"] == "echo"


def test_chat_with_profile_uses_profile_system_prompt(client, monkeypatch, tmp_path):
    from app.services.profiles.store import ProfileStore
    fresh = ProfileStore(str(tmp_path / "p2.json"))
    fresh.upsert(Profile(
        name="greet",
        llm=LlmConfig(base_url="http://localhost:11434/v1", model="llama3"),
        system_prompt="Always say howdy.",
    ))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh)

    captured = []
    original_init = __import__(
        "app.services.conversation.responder", fromlist=["OpenAICompatResponder"]
    ).OpenAICompatResponder.__init__

    def _patched_init(self, base_url, api_key, model, system_prompt, timeout):
        captured.append(system_prompt)
        original_init(self, base_url, api_key, model, system_prompt, timeout)

    with patch(
        "app.services.conversation.responder.OpenAICompatResponder.__init__",
        _patched_init,
    ):
        client.post(
            "/v1/conversation/chat?profile=greet",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert any("howdy" in sp for sp in captured)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_conversation_profile.py -v
```
Expected: some tests fail (profile_store not wired in conversation.py yet)

- [ ] **Step 3: Add imports to `conversation.py`**

Add these imports at the top of `conversation.py` (after existing imports):

```python
from app.services.conversation.responder import (
    build_responder,
    build_responder_ex,          # new
    get_active_llm_api_key,
    get_active_llm_base_url,
    get_active_llm_model,
    reset_active_llm_config,
    set_active_llm_config,
)
from app.services.conversation.tools.mcp import McpToolSource  # new
from app.services.mcp.pool import mcp_pool                     # new
from app.services.mcp.server_store import mcp_server_store     # new
from app.services.profiles.store import profile_store          # new
```

- [ ] **Step 4: Update `conversation_stream()` — profile resolution block**

In `conversation_stream`, immediately after `q = websocket.query_params`, add before any existing variable assignments:

```python
    # --- Profile resolution ---
    profile_name = q.get("profile")
    profile = profile_store.get(profile_name) if profile_name else None
    if profile_name and not profile:
        await websocket.send_json({
            "event": "warning",
            "message": f"profile '{profile_name}' not found, using defaults",
        })

    # LLM config: profile overrides global state / .env
    llm_base_url = (profile.llm.base_url or None) if (profile and profile.llm.base_url) else None
    llm_api_key = profile.llm.api_key if (profile and profile.llm.base_url) else None
    llm_model = (profile.llm.model or None) if (profile and profile.llm.model) else None
    system_prompt = (profile.system_prompt or None) if (profile and profile.system_prompt) else None
```

- [ ] **Step 5: Replace TTS resolution and responder construction in `conversation_stream()`**

Replace the existing lines:
```python
    tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
    voice = q.get("voice") or None
```
with:
```python
    if profile and profile.tts.engine:
        tts_engine = profile.tts.engine
        voice = profile.tts.voice or q.get("voice") or None
    else:
        tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
        voice = q.get("voice") or None
```

Replace the existing line:
```python
    responder = build_responder()
```
with:
```python
    responder = build_responder_ex(
        base_url=llm_base_url,
        api_key=llm_api_key,
        model=llm_model,
        system_prompt=system_prompt,
    )
```

- [ ] **Step 6: Replace tool registry construction in `conversation_stream()`**

Replace the existing block:
```python
    tool_registry: ToolRegistry | None = None
    if settings.conversation_tools_enabled:
        tool_registry = ToolRegistry([LocalToolSource()])
```
with:
```python
    # Merge global MCP servers + per-profile MCP servers (profile wins on name collision)
    global_servers = mcp_server_store.list()
    profile_specific = {s.name: s for s in (profile.mcp_servers if profile else [])}
    merged_servers = {**global_servers, **profile_specific}

    tool_sources: list = []
    has_mcp = bool(merged_servers)
    if settings.conversation_tools_enabled or has_mcp:
        tool_sources.append(LocalToolSource())
        for srv in merged_servers.values():
            tools = await mcp_pool.get_tools(srv.url)
            if tools:
                url = srv.url
                tool_sources.append(
                    McpToolSource(tools, invoker=lambda n, a, u=url: mcp_pool.invoke(u, n, a))
                )

    tool_registry: ToolRegistry | None = ToolRegistry(tool_sources) if tool_sources else None
```

- [ ] **Step 7: Update `session_started` event in `conversation_stream()`**

Add `"profile"` and `"active_tools"` to the existing `session_started` send:

```python
    active_tools = list(tool_registry._tools.keys()) if tool_registry else []
    await websocket.send_json(
        {
            "event": "session_started",
            "session_id": session_id,
            "profile": profile_name,          # new
            "active_tools": active_tools,      # new
            "stt_engine": stt_engine,
            # ... rest unchanged ...
        }
    )
```

- [ ] **Step 8: Fix audio-path tool_registry bug in `_run_turn()`**

Find the audio path reply (near the bottom of `_run_turn`), currently:
```python
        parts = await _stream_to_tts(responder.reply_stream(history), responder.name)
```
Replace with:
```python
        parts = await _stream_to_tts(
            responder.reply_stream(
                history,
                registry=tool_registry,
                ctx=tool_ctx,
                max_iters=settings.conversation_tool_max_iters,
            ),
            responder.name,
        )
```

- [ ] **Step 9: Update REST `chat` endpoint to support `?profile=`**

Replace:
```python
@router.post("/chat")
async def chat(payload: ChatRequest) -> dict:
    """Text chat with the configured conversation responder (LLM or echo)."""
    responder = build_responder()
    history = [{"role": m.role, "content": m.content} for m in payload.messages]
    reply = await responder.reply(history)
    return {
        "success": True,
        "data": {"reply": reply, "responder": responder.name, "model": get_active_llm_model()},
    }
```
with:
```python
@router.post("/chat")
async def chat(payload: ChatRequest, profile: str | None = None) -> dict:
    """Text chat with the configured conversation responder (LLM or echo)."""
    active_profile = profile_store.get(profile) if profile else None
    llm_base_url = (active_profile.llm.base_url or None) if (active_profile and active_profile.llm.base_url) else None
    llm_api_key = active_profile.llm.api_key if (active_profile and active_profile.llm.base_url) else None
    llm_model = (active_profile.llm.model or None) if (active_profile and active_profile.llm.model) else None
    system_prompt = (active_profile.system_prompt or None) if (active_profile and active_profile.system_prompt) else None
    responder = build_responder_ex(
        base_url=llm_base_url,
        api_key=llm_api_key,
        model=llm_model,
        system_prompt=system_prompt,
    )
    history = [{"role": m.role, "content": m.content} for m in payload.messages]
    reply = await responder.reply(history)
    return {
        "success": True,
        "data": {
            "reply": reply,
            "responder": responder.name,
            "model": get_active_llm_model(),
            "profile": profile,
        },
    }
```

- [ ] **Step 10: Run the profile tests**

```bash
pytest tests/unit/test_conversation_profile.py -v
```
Expected: 3 tests PASS

- [ ] **Step 11: Run full test suite**

```bash
pytest tests/ -x -q --ignore=tests/integration
```
Expected: all pass

- [ ] **Step 12: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py \
        tests/unit/test_conversation_profile.py
git commit -m "feat(conversation): profile-aware session wiring + MCP tool resolution"
```

---

## Self-Review Checklist

- [x] **Spec coverage:**
  - Section 1 (data model + storage): Tasks 1, 2, 3
  - Section 2 (Python components): Tasks 2, 3, 4, 5
  - Section 3 (API routes): Tasks 6, 7, 8
  - Section 4 (session wiring): Task 8
  - Section 5 (error handling): pool returns `[]` on error (Task 5), unknown profile → warning (Task 8), all covered
  - Section 6 (new settings): Task 1
  - Section 7 (file layout): matches plan file map exactly
  - Section 8 (testing): all test files planned and implemented
- [x] **No placeholders:** all steps have concrete code
- [x] **Type consistency:** `McpServer` used uniformly across profiles + mcp store; `build_responder_ex` signature matches usage in Tasks 6 and 8; `profile_store` / `mcp_server_store` / `mcp_pool` singletons imported consistently
