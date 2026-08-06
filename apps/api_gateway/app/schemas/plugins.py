from pydantic import BaseModel

from app.services.plugins.models import PluginMount


class PluginRequest(BaseModel):
    name: str
    url: str
    secret: str
    enabled: bool = True
    kind: str = "feature"
    mounts: list[PluginMount] = []


class TicketRequest(BaseModel):
    plugin: str
