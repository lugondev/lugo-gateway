"""A registered out-of-process plugin.

Deliberately shaped after McpServer (services/mcp/models.py): same name/owner/
url/enabled spine, same url-scheme validation. The one difference is direction.
McpServer.headers is a credential the gateway SENDS when it calls the MCP
server. `secret` here runs the other way -- the gateway never calls a plugin,
the browser does, so the only cross-service call is the plugin calling back
into POST /api/auth/introspect, and `secret` is what authenticates it.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, field_validator


class PluginMount(BaseModel):
    """One path the plugin serves. Advertised to the web client by
    GET /v1/plugins so it knows what to connect to without hardcoding it."""

    path: str
    kind: Literal["ws", "http"]
    public: bool = True


class Plugin(BaseModel):
    name: str
    owner_id: str | None = None
    url: str
    secret: str
    enabled: bool = True
    # "tools" exists so the MCP server registry can fold into this store later.
    # Nothing in this design depends on that happening.
    kind: Literal["feature", "tools"] = "feature"
    mounts: list[PluginMount] = []

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: str) -> str:
        if urlparse(v).scheme not in ("http", "https"):
            raise ValueError("Plugin URL must use http or https scheme")
        return v
