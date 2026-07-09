from __future__ import annotations

from pydantic import BaseModel

from app.services.mcp.models import McpServer


class LlmConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class TtsConfig(BaseModel):
    profile_name: str = ""   # name of a TtsProfile (services/tts/profile_store.py); "" = server defaults


class SttConfig(BaseModel):
    # Language preset (services/stt/profile.py: vi|en|multi|en_vi) — sets engine +
    # language together. "" = inherit the server-wide default (settings.stt_profile).
    profile: str = ""
    # Explicit overrides, for when the preset isn't enough. "" = derive from the
    # preset / server default. engine is a registered STT engine name; language is
    # a hint ("" = auto-detect via the preset).
    engine: str = ""
    language: str = ""


class MemoryConfig(BaseModel):
    enabled: bool = True        # auto-extract memories after a session ends
    mode: str = "all"           # "all" | "semantic"
    top_k: int = 5              # semantic mode: how many memories to inject
    extractor_model: str = ""   # "" = use the profile's own LLM model
    embed_model: str = ""       # semantic mode: OpenAI-compatible embedding model


class SessionConfig(BaseModel):
    idle_timeout_s: int = 30    # seconds of inactivity before the server disconnects; 0 = never


class Profile(BaseModel):
    name: str
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    # When true, append a built-in directive telling the LLM to emit plain,
    # speakable text (no markdown/emoji/symbols/URLs) so TTS reads it cleanly.
    voice_optimized: bool = False
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
    session: SessionConfig = SessionConfig()
