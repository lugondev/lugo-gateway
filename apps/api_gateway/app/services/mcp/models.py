from urllib.parse import urlparse

from pydantic import BaseModel, field_validator


class McpServer(BaseModel):
    name: str
    owner_id: str | None = None
    url: str
    headers: dict[str, str] = {}
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: str) -> str:
        if urlparse(v).scheme not in ("http", "https"):
            raise ValueError("MCP server URL must use http or https scheme")
        return v
