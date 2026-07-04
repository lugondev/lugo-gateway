# Design: `mcp-servers/basic-tools` remote MCP server (timedate + fetch)

## Context

The app's global/per-profile MCP integration (`apps/api_gateway/app/services/mcp/`) talks a
simplified REST contract, not the real MCP JSON-RPC transport:

- `GET  {url}/tools` → list of tool defs (`name`, `description`, `inputSchema`)
- `POST {url}/tools/{name}` body `{"arguments": {...}}` → `{"result": str}`

Official `modelcontextprotocol/servers` (filesystem, fetch, memory, ...) speak stdio/JSON-RPC and
don't plug in directly. Rather than building a real JSON-RPC/stdio transport, we're building small
remote servers that speak this app's existing REST contract, inspired by (not identical to) the
official `fetch` and reference "current time" servers.

## Scope

One standalone service, `mcp-servers/basic-tools/`, exposing two tools:

- `get_current_time(timezone?: str = "UTC")` — current time as ISO 8601, resolved via
  `zoneinfo` (stdlib, no extra dep). Unknown timezone → 400 with a clear message.
- `fetch_url(url: str, max_length?: int = 5000)` — HTTP GET via `httpx`, returns response text
  truncated to `max_length`. Only `http`/`https` allowed. Resolves the hostname and rejects
  loopback/private/link-local/multicast addresses (basic SSRF guard) → 400 if blocked.
  10s timeout.

Both tools live in one FastAPI app / one port, not two separate services — simpler to run and
deploy as a single unit.

## Not in scope

- No real MCP JSON-RPC/stdio transport.
- No filesystem/memory/git/sequential-thinking presets (out of scope for this change).
- No global-MCP-server "preset catalog with enable/disable checkbox" UI (separate, later
  feature — this change only produces the remote server(s) that such presets would point to).
- No auth on the service itself; if exposed beyond localhost, protect it via the existing
  per-server `headers` support (e.g. put an API key header requirement in front of it, or rely on
  network placement) — that's a deploy-time concern, not this service's job.

## Layout

```
mcp-servers/
  basic-tools/
    pyproject.toml      # fastapi, uvicorn, httpx only — independent, deployable on its own
    main.py             # FastAPI app: GET /tools, POST /tools/{name}
    tools/
      timedate.py        # get_current_time impl + tool-def dict
      fetch.py            # fetch_url impl + tool-def dict + SSRF guard
    tests/
      test_timedate.py
      test_fetch.py
      test_routes.py      # GET /tools, POST /tools/{name} dispatch + 404 for unknown tool
    Dockerfile
```

Independent `pyproject.toml`/venv (own pytest run), not merged into the root `pyproject.toml` —
consistent with `esp32-assistant/` and `agent-assistant/` already being independent sibling
projects in this repo, and keeps the main app's dependency set untouched.

## Error handling

- `POST /tools/{name}` for an unknown tool name → 404 `{"detail": "..."}`.
- Bad/missing arguments (e.g. no `url`) → 422 (FastAPI's default Pydantic validation).
- Domain-level failures (bad timezone, blocked/unreachable URL, non-2xx upstream response) →
  400 `{"detail": "..."}`, matching `McpHttpClient`'s expectation that `resp.raise_for_status()`
  turns any non-2xx into an `McpConnectionError` surfaced to the caller.

## Testing

TDD, FastAPI `TestClient`, following the style of `tests/unit/test_mcp_routes.py`. `fetch_url`
tests mock `httpx` (no real network calls in tests, same pattern as
`tests/unit/test_mcp_client.py`).

## Follow-up (explicitly out of scope here)

Registering these as global preset MCP servers with an enable/disable checkbox in the UI is a
separate change once this service exists and has a real deployed URL to point presets at.
