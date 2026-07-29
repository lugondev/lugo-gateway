from pydantic import BaseModel


class CreateProviderRequest(BaseModel):
    name: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    enabled: bool = True
    config: dict = {}


class UpdateProviderRequest(BaseModel):
    name: str | None = None
    label: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    config: dict | None = None
