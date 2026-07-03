from __future__ import annotations

from pydantic import BaseModel

from app.services.mcp.models import McpServer


class LlmConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class TtsConfig(BaseModel):
    engine: str = ""
    voice: str = ""


class MemoryConfig(BaseModel):
    enabled: bool = True        # auto-extract memories after a session ends
    mode: str = "all"           # "all" | "semantic"
    top_k: int = 5              # semantic mode: how many memories to inject
    extractor_model: str = ""   # "" = use the profile's own LLM model
    embed_model: str = ""       # semantic mode: OpenAI-compatible embedding model


class Profile(BaseModel):
    name: str
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
