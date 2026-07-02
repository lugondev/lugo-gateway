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
