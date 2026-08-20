from pydantic import BaseModel

from app.services.mcp.models import McpServer
from app.services.profiles.models import (
    KnowledgeConfig,
    LlmConfig,
    MemoryConfig,
    SessionConfig,
    SttConfig,
    TtsConfig,
)


class ProfileRequest(BaseModel):
    name: str
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    voice_optimized: bool = False
    # Admin-only; silently forced to False for anyone else (see
    # api/routes/profiles.py), the same way mcp_servers is.
    shared: bool = False
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
    # Omitting this key on PUT PRESERVES the stored block rather than resetting
    # it (see update_profile), the same shape `shared` and `llm.api_key` use.
    # No client written before this branch sends it -- static/js/profiles.js
    # included -- so resetting would mean every unrelated profile save silently
    # switched the knowledge base back off. An explicit block still replaces
    # wholesale, so it remains turn-off-able.
    knowledge: KnowledgeConfig = KnowledgeConfig()
    session: SessionConfig = SessionConfig()
