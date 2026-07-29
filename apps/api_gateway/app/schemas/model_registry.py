from pydantic import BaseModel


class CreateEntryRequest(BaseModel):
    kind: str
    engine: str
    model_id: str
    label: str
    stage: str = "stable"
    base_url: str = ""
    api_key: str = ""
    config: dict = {}
    sample_text: str = "xin chào"
    is_default: bool = False


class UpdateEntryRequest(BaseModel):
    enabled: bool | None = None
    stage: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    config: dict | None = None
    is_default: bool | None = None


class PriceItem(BaseModel):
    id: str
    price: dict | None = None


class BulkPriceRequest(BaseModel):
    prices: list[PriceItem]
