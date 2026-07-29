from pydantic import BaseModel


class McpServerRequest(BaseModel):
    name: str
    url: str
    headers: dict[str, str] = {}
    enabled: bool = True


class McpServerEnabledRequest(BaseModel):
    enabled: bool
