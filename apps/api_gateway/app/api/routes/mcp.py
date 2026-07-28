from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.actor import current_role, current_user_id
from app.services.mcp.models import McpServer
from app.services.mcp.pool import mcp_pool
from app.services.mcp.presets import PRESET_NAMES
from app.services.mcp.server_store import mcp_server_store

router = APIRouter(prefix="/v1/mcp", tags=["mcp"])


def _visible(server: McpServer, user_id: str | None) -> bool:
    return server.owner_id is None or server.owner_id == user_id


def _can_write(server: McpServer, user_id: str | None, role: str) -> bool:
    if server.owner_id is None:
        return role == "admin"
    return server.owner_id == user_id


def _require_admin(request: Request) -> None:
    """Create/update/delete/clone let the caller point the gateway's outbound
    fetch at an arbitrary url + headers, then GET .../tools makes the gateway
    fetch it and return the response body to the caller -- a full SSRF proxy
    with reflection (e.g. against http://169.254.169.254) for any logged-in
    user before this gate. Admin-only; deliberately NO IP blocklist -- the
    only real server in the live DB (basic-tools) self-hosts on loopback,
    which is the normal deployment pattern here, and a blocklist would only
    raise the bar for an actor who must already be an admin. Read routes
    (list/get/tools) stay open to normal users -- only the write surface is
    gated. See docs/superpowers/sdd/2026-07-28-critical-authz-fixes/task-6-brief.md."""
    if current_role(request) != "admin":
        raise HTTPException(status_code=403, detail="admin only")


class McpServerRequest(BaseModel):
    name: str
    url: str
    headers: dict[str, str] = {}
    enabled: bool = True


class McpServerEnabledRequest(BaseModel):
    enabled: bool


class CloneRequest(BaseModel):
    new_name: str


@router.get("/servers")
async def list_servers(request: Request) -> dict:
    user_id = current_user_id(request)
    servers = mcp_server_store.list()
    visible = {k: v for k, v in servers.items() if _visible(v, user_id)}
    return {"success": True, "data": {k: v.model_dump() for k, v in visible.items()}}


@router.post("/servers")
async def add_server(payload: McpServerRequest, request: Request) -> dict:
    _require_admin(request)
    if mcp_server_store.get(payload.name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    # _require_admin above guarantees role == "admin" here, so this always
    # creates a template (owner_id=None, visible to everyone) -- the old
    # "non-admin creates a private row" branch is dead now that create is
    # gated to admins only.
    entry = McpServer(
        name=payload.name, url=payload.url, headers=payload.headers,
        enabled=payload.enabled, owner_id=None,
    )
    mcp_server_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.get("/servers/{name}")
async def get_server(name: str, request: Request) -> dict:
    entry = mcp_server_store.get(name)
    if not entry or not _visible(entry, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return {"success": True, "data": entry.model_dump()}


@router.put("/servers/{name}")
async def update_server(name: str, payload: McpServerRequest, request: Request) -> dict:
    _require_admin(request)
    old = mcp_server_store.get(name)
    if not old or not _can_write(old, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    mcp_pool.invalidate(old.url)
    entry = McpServer(
        name=name, url=payload.url, headers=payload.headers,
        enabled=payload.enabled, owner_id=old.owner_id,
    )
    mcp_server_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.patch("/servers/{name}/enabled")
async def set_server_enabled(name: str, payload: McpServerEnabledRequest, request: Request) -> dict:
    entry = mcp_server_store.get(name)
    if not entry or not _can_write(entry, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    updated = entry.model_copy(update={"enabled": payload.enabled})
    mcp_server_store.upsert(updated)
    return {"success": True, "data": updated.model_dump()}


@router.delete("/servers/{name}")
async def delete_server(name: str, request: Request) -> dict:
    _require_admin(request)
    if name in PRESET_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' is a built-in preset; disable it instead of deleting it",
        )
    entry = mcp_server_store.get(name)
    if not entry or not _can_write(entry, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    mcp_pool.invalidate(entry.url)
    mcp_server_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}


@router.get("/servers/{name}/tools")
async def list_server_tools(name: str, request: Request) -> dict:
    entry = mcp_server_store.get(name)
    if not entry or not _visible(entry, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    mcp_pool.invalidate(entry.url)
    tools = await mcp_pool.get_tools(entry.url, headers=entry.headers)
    return {"success": True, "data": {"server": name, "url": entry.url, "tools": tools}}


@router.post("/servers/{name}/clone")
async def clone_server(name: str, payload: CloneRequest, request: Request) -> dict:
    _require_admin(request)
    user_id = current_user_id(request)
    source = mcp_server_store.get(name)
    if not source or not _visible(source, user_id):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    if mcp_server_store.get(payload.new_name) is not None:
        raise HTTPException(status_code=409, detail=f"'{payload.new_name}' already exists")
    clone = McpServer(
        name=payload.new_name, url=source.url, headers=source.headers,
        enabled=source.enabled, owner_id=user_id,
    )
    mcp_server_store.upsert(clone)
    return {"success": True, "data": clone.model_dump()}
