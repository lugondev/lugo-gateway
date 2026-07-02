from pydantic import BaseModel


class McpServer(BaseModel):
    name: str
    url: str
