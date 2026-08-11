"""Registry of out-of-process plugins, plus the ticket a browser trades for a
direct connection to one.

Shaped after api/routes/mcp.py: same admin-gated write surface, same
user-readable list, same masking of the secret field for non-admin readers.
See specs/2026-08-05-gateway-plugin-contract-design.md.
"""

from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_role, current_user_id, require_admin
from app.schemas.plugins import PluginRequest, TicketRequest
from app.services.auth.tokens import PLUGIN_TICKET_TTL_SECONDS, issue_plugin_token
from app.services.plugins.models import Plugin
from app.services.plugins.store import plugin_store

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])

_require_admin = require_admin


def _visible(entry: Plugin, user_id: str | None) -> bool:
    return entry.owner_id is None or entry.owner_id == user_id


def _view(entry: Plugin, role: str) -> dict:
    """`secret` authenticates the plugin's introspection calls, so handing it
    to every logged-in user would let any of them resolve any ticket. Masked
    rather than dropped, so the response shape is stable for clients that read
    the field -- same treatment mcp.py gives `headers`."""
    data = entry.model_dump()
    if role != "admin":
        data["secret"] = "***"
    return data


@router.get("")
async def list_plugins(request: Request) -> dict:
    user_id = current_user_id(request)
    role = current_role(request)
    entries = plugin_store.list()
    visible = {k: v for k, v in entries.items() if _visible(v, user_id)}
    return {"success": True, "data": {k: _view(v, role) for k, v in visible.items()}}


@router.post("")
async def add_plugin(payload: PluginRequest, request: Request) -> dict:
    _require_admin(request)
    # exists(), not get() is None: an unreadable row occupies its name and must
    # 409 rather than be silently claimed. Same H4-class check as mcp.py.
    if plugin_store.exists(payload.name):
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    entry = Plugin(
        name=payload.name,
        url=payload.url,
        secret=payload.secret,
        enabled=payload.enabled,
        kind=payload.kind,
        mounts=payload.mounts,
        owner_id=None,
    )
    plugin_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.post("/ticket")
async def issue_ticket(payload: TicketRequest, request: Request) -> dict:
    """A short-lived, audience-bound ticket the browser presents to the plugin.

    Fixed path with the plugin in the body, NOT /v1/plugins/{name}/ticket:
    /v1/plugins is an admin prefix and auth_guard._USER_EXACT can only carve a
    user route out of it by exact path and method.
    """
    entry = plugin_store.get(payload.plugin)
    if not entry or not entry.enabled or not _visible(entry, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"plugin '{payload.plugin}' not found")
    # An unauthenticated caller (auth disabled in dev) has no user_id; the
    # empty string travels as "no owner", matching WsIdentity.unauthenticated.
    user_id = current_user_id(request) or ""
    return {
        "success": True,
        "data": {
            "url": entry.url,
            "token": issue_plugin_token(user_id, payload.plugin),
            "expires_in": PLUGIN_TICKET_TTL_SECONDS,
        },
    }


@router.get("/{name}")
async def get_plugin(name: str, request: Request) -> dict:
    entry = plugin_store.get(name)
    if not entry or not _visible(entry, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"plugin '{name}' not found")
    return {"success": True, "data": _view(entry, current_role(request))}


@router.put("/{name}")
async def update_plugin(name: str, payload: PluginRequest, request: Request) -> dict:
    _require_admin(request)
    old = plugin_store.get(name)
    if not old:
        raise HTTPException(status_code=404, detail=f"plugin '{name}' not found")
    entry = Plugin(
        name=name,
        url=payload.url,
        secret=payload.secret,
        enabled=payload.enabled,
        kind=payload.kind,
        mounts=payload.mounts,
        owner_id=old.owner_id,
    )
    plugin_store.upsert(entry)
    return {"success": True, "data": entry.model_dump()}


@router.delete("/{name}")
async def delete_plugin(name: str, request: Request) -> dict:
    _require_admin(request)
    entry = plugin_store.get(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"plugin '{name}' not found")
    plugin_store.delete(name)
    return {"success": True, "data": {"name": name, "deleted": True}}
