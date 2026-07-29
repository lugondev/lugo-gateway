from pydantic import BaseModel


class DownloadRequest(BaseModel):
    name: str


class WhisperRequest(BaseModel):
    size: str


class OmniModelRequest(BaseModel):
    id: str


class VieneuModeRequest(BaseModel):
    mode: str


class LlmModelRequest(BaseModel):
    model: str
