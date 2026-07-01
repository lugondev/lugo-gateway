# LLM Profiles & MCP HTTP Tooling — Design Spec
**Date:** 2026-07-01

## Overview

Add named LLM profiles (each bundling an endpoint, system prompt, and TTS voice) and wire up MCP HTTP/SSE transport for tool calling. Profiles activate per-session via `?profile=<name>` on the conversation WebSocket.

---

## 1. Data Model & Storage

### profiles.json (`settings.profiles_path`, default `profiles.json`)

```json
{
  "profiles": {
    "home-assistant": {
      "name": "home-assistant",
      "llm": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
        "model": "llama3.2"
      },
      "system_prompt": "You are a home automation assistant. Reply concisely in 1-2 sentences.",
      "tts": { "engine": "vieneu", "voice": "" },
      "mcp_servers": [
        { "name": "ha-tools", "url": "http://localhost:3001/mcp" }
      ]
    }
  }
}
```

- `mcp_servers[]` in a profile: additional MCP servers **on top of** global pool.
- `name` in per-profile `mcp_servers` references a local alias; `url` is the HTTP endpoint.
- All fields optional except `name`. Missing `llm`/`system_prompt`/`tts` fall back to `.env` defaults.

### mcp_servers.json (`settings.mcp_servers_path`, default `mcp_servers.json`)

```json
{
  "servers": {
    "filesystem": { "name": "filesystem", "url": "http://localhost:3002/mcp" },
    "web-search":  { "name": "web-search",  "url": "http://localhost:3003/mcp" }
  }
}
```

- Global pool, available to every session regardless of profile.
- Both files: atomic JSON write-on-change, created empty on first access.

### Tool resolution order (per session)

1. Collect global MCP server URLs from `mcp_servers.json`.
2. Collect per-profile MCP server URLs from the activated profile (if any).
3. Merge by name — per-profile entry wins on name collision.
4. `McpConnectionPool.get_tools(url)` for each URL → build `McpToolSource`.
5. Final `ToolRegistry` = `LocalToolSource` + one `McpToolSource` per reachable server.

---

## 2. New Python Components

### `services/profiles/store.py` — `ProfileStore`

- `list() → dict[str, Profile]`
- `get(name) → Profile | None`
- `upsert(profile: Profile) → None` (creates or replaces)
- `delete(name) → None`
- Thread-safe read/write with a `threading.Lock`; atomic file write via temp-file rename.

### `services/mcp/server_store.py` — `McpServerStore`

Same CRUD interface as `ProfileStore` but for the global MCP server pool.

### `services/mcp/client.py` — `McpHttpClient`

Thin async `httpx.AsyncClient` wrapper:

```
GET  {url}/tools              → list[{name, description, inputSchema}]
POST {url}/tools/{tool_name}  → {"result": str}   body: {"arguments": {...}}
```

- `connect_timeout`: `settings.mcp_connection_timeout_seconds` (default 10 s)
- `tool_timeout`: `settings.mcp_tool_timeout_seconds` (default 30 s)
- Raises `McpConnectionError` on network failure; callers log and skip.

### `services/mcp/pool.py` — `McpConnectionPool`

```python
async def get_tools(url: str) -> list[dict]        # discover + cache
async def invoke(url: str, name: str, args: dict) -> str
def invalidate(url: str) -> None                   # called on server config change
```

- Lazy-init one `McpHttpClient` per URL, stored in `dict[str, McpHttpClient]`.
- Tool definitions cached per-URL with TTL = `settings.mcp_tool_cache_ttl_seconds` (default 300 s).
- `get_tools` failure → log warning, return `[]` (server skipped for this session).
- Singleton `mcp_pool` instance, imported where needed.

---

## 3. API Routes

### Profile routes — `api/routes/profiles.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/profiles` | List all profiles |
| POST | `/v1/profiles` | Create profile |
| GET | `/v1/profiles/{name}` | Get profile |
| PUT | `/v1/profiles/{name}` | Update profile |
| DELETE | `/v1/profiles/{name}` | Delete profile |

### Global MCP server routes — `api/routes/mcp.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/mcp/servers` | List global MCP servers |
| POST | `/v1/mcp/servers` | Add server |
| GET | `/v1/mcp/servers/{name}` | Get server |
| PUT | `/v1/mcp/servers/{name}` | Update server |
| DELETE | `/v1/mcp/servers/{name}` | Remove server |
| GET | `/v1/mcp/servers/{name}/tools` | Live-discover tools (no cache) |

PUT/DELETE on a server also calls `mcp_pool.invalidate(url)` to clear stale cache.

### Profile activation

No dedicated activation endpoint. Clients pass `?profile=<name>` on the conversation WebSocket or REST chat:

```
WS  /v1/conversation/stream?profile=home-assistant
POST /v1/conversation/chat?profile=home-assistant
```

---

## 4. Session Wiring (conversation.py changes)

```python
profile_name = q.get("profile")
profile = profile_store.get(profile_name) if profile_name else None

# Resolve LLM config
llm_base_url  = profile.llm.base_url  if profile?.llm?.base_url  else get_active_llm_base_url()
llm_api_key   = profile.llm.api_key   if profile?.llm           else get_active_llm_api_key()
llm_model     = profile.llm.model     if profile?.llm?.model     else get_active_llm_model()
system_prompt = profile.system_prompt  if profile                else settings.conversation_system_prompt
tts_engine    = profile.tts.engine    if profile?.tts?.engine    else (q.get("tts_engine") or ...)
voice         = profile.tts.voice     if profile?.tts?.voice     else q.get("voice")

# Resolve MCP tools
global_servers = mcp_server_store.list().values()         # global pool
profile_servers = profile.mcp_servers if profile else []  # per-profile
all_servers = merge_by_name(global_servers, profile_servers)
tool_sources = [LocalToolSource()]
for srv in all_servers:
    tools = await mcp_pool.get_tools(srv.url)
    if tools:
        tool_sources.append(McpToolSource(tools, invoker=lambda n,a,u=srv.url: mcp_pool.invoke(u,n,a)))
tool_registry = ToolRegistry(tool_sources)

# Build responder with profile config
responder = OpenAICompatResponder(llm_base_url, llm_api_key, llm_model, system_prompt, timeout)
# or EchoResponder if no base_url
```

`session_started` event adds:
```json
{
  "profile": "home-assistant",
  "mcp_tools": ["ha_light_on", "ha_light_off", "get_time"]
}
```

If `?profile=unknown` → emit `{"event": "warning", "message": "profile 'unknown' not found, using defaults"}`, continue with `.env` defaults.

---

## 5. Error Handling

| Scenario | Behavior |
|----------|----------|
| Profile not found | Warning event, session continues with `.env` defaults |
| MCP server unreachable at tool-discover time | Log warning, skip server, session continues with remaining tools |
| MCP tool call failure | `ToolRegistry.run()` returns error string; LLM sees it and can recover |
| profiles.json / mcp_servers.json missing | Auto-create empty file on first access |
| Concurrent profile writes | `threading.Lock` + atomic rename prevents corruption |

---

## 6. New Settings

```python
profiles_path: str = "profiles.json"
mcp_servers_path: str = "mcp_servers.json"
mcp_tool_cache_ttl_seconds: int = 300
mcp_connection_timeout_seconds: float = 10.0
mcp_tool_timeout_seconds: float = 30.0
```

`conversation_tools_enabled` flag is retained. When a profile with MCP servers is active, tools are enabled automatically for that session regardless of the flag.

---

## 7. File Layout (new files)

```
apps/api_gateway/app/
  api/routes/
    profiles.py          # Profile CRUD routes
    mcp.py               # Global MCP server routes
  services/
    profiles/
      __init__.py
      store.py           # ProfileStore (JSON persistence)
      models.py          # Profile, LlmConfig, TtsConfig, McpServerRef pydantic models
    mcp/
      __init__.py
      server_store.py    # McpServerStore (JSON persistence)
      client.py          # McpHttpClient
      pool.py            # McpConnectionPool singleton
```

Existing files modified: `settings.py` (5 new fields), `conversation.py` (profile resolution + MCP wiring), `main.py` (register new routers).

---

## 8. Testing

- `tests/unit/test_profiles_store.py` — CRUD, persistence, concurrent writes
- `tests/unit/test_mcp_server_store.py` — CRUD, persistence
- `tests/unit/test_mcp_pool.py` — lazy connect, TTL cache, error handling (mock httpx)
- `tests/unit/test_mcp_client.py` — HTTP calls, timeout, error mapping
- `tests/unit/test_profiles_routes.py` — REST API happy + error paths
- `tests/integration/test_conversation_profile.py` — WS session with `?profile=`, verify tool list in `session_started`
