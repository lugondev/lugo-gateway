from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)


class LlmConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
