from pydantic import BaseModel


class CreateQuotaRequest(BaseModel):
    scope: str
    scope_id: str = ""
    limit_usd: float
    period: str = "monthly"
    enabled: bool = True


class UpdateQuotaRequest(BaseModel):
    scope: str | None = None
    scope_id: str | None = None
    limit_usd: float | None = None
    period: str | None = None
    enabled: bool | None = None
