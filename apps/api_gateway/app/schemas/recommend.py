from pydantic import BaseModel


class InstallRequest(BaseModel):
    package: str
