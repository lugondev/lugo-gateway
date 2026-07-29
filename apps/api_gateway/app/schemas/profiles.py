from pydantic import BaseModel

from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, MemoryConfig, SessionConfig, SttConfig, TtsConfig


class ProfileRequest(BaseModel):
    name: str
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    voice_optimized: bool = False
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
    session: SessionConfig = SessionConfig()
