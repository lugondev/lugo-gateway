from pydantic import BaseModel


class TikTokConnectRequest(BaseModel):
    unique_id: str
