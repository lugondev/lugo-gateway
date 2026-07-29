from pydantic import BaseModel


class PairInitRequest(BaseModel):
    serial: str


class PairClaimRequest(BaseModel):
    code: str
    name: str
