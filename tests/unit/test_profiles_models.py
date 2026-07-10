from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, Profile, TtsConfig


def test_profile_defaults():
    p = Profile(name="x")
    assert p.llm.base_url == ""
    assert p.tts.profile_name == ""
    assert p.stt.profile == ""
    assert p.stt.engine == ""
    assert p.stt.language == ""
    assert p.mcp_servers == []
    assert p.system_prompt == ""
    assert p.voice_optimized is False


def test_profile_voice_optimized_round_trip():
    p = Profile(name="x", voice_optimized=True)
    assert p.voice_optimized is True
    p2 = Profile.model_validate(p.model_dump())
    assert p2.voice_optimized is True


def test_profile_voice_optimized_back_compat_old_json():
    # a profile saved before the flag existed still validates, defaulting to False
    p = Profile.model_validate({"name": "legacy"})
    assert p.voice_optimized is False


def test_profile_stt_config():
    from app.services.profiles.models import SttConfig

    p = Profile(name="x", stt=SttConfig(profile="vi"))
    assert p.stt.profile == "vi"
    p2 = Profile.model_validate(p.model_dump())
    assert p2.stt.profile == "vi"


def test_profile_stt_back_compat_old_json():
    # a profile saved before the stt section existed still validates with defaults
    p = Profile.model_validate({"name": "legacy", "tts": {"profile_name": "v"}})
    assert p.stt.profile == ""


def test_profile_stt_model_defaults_empty():
    from app.services.profiles.models import SttConfig

    p = Profile(name="x")
    assert p.stt.model == ""


def test_profile_stt_model_round_trip():
    from app.services.profiles.models import SttConfig

    p = Profile(name="x", stt=SttConfig(engine="qwen3_asr", model="0.6b"))
    assert p.stt.model == "0.6b"
    p2 = Profile.model_validate(p.model_dump())
    assert p2.stt.model == "0.6b"


def test_profile_stt_model_back_compat_old_json():
    # a profile saved before the model field existed still validates, defaulting to ""
    p = Profile.model_validate({"name": "legacy", "stt": {"engine": "whisper"}})
    assert p.stt.model == ""


def test_profile_full():
    p = Profile(
        name="home",
        llm=LlmConfig(base_url="http://localhost:11434/v1", api_key="", model="llama3.2"),
        system_prompt="You are a home assistant.",
        tts=TtsConfig(profile_name="cohost-voice"),
        mcp_servers=[McpServer(name="ha", url="http://localhost:3001/mcp")],
    )
    assert p.name == "home"
    assert p.llm.model == "llama3.2"
    assert len(p.mcp_servers) == 1


def test_mcpserver_model():
    s = McpServer(name="fs", url="http://localhost:3002/mcp")
    assert s.name == "fs"
    assert s.url == "http://localhost:3002/mcp"


def test_profile_roundtrip():
    p = Profile(name="test", system_prompt="hello")
    data = p.model_dump()
    p2 = Profile.model_validate(data)
    assert p2.system_prompt == "hello"


def test_profile_defaults_memory_and_nickname():
    from app.services.profiles.models import Profile

    p = Profile(name="x")
    assert p.nickname == ""
    assert p.memory.enabled is True
    assert p.memory.mode == "all"
    assert p.memory.top_k == 5
    assert p.memory.extractor_model == ""
    assert p.memory.embed_model == ""


def test_profile_back_compat_old_json():
    from app.services.profiles.models import Profile

    # a profile saved before memory/nickname existed still validates
    old = {"name": "legacy", "system_prompt": "hi", "llm": {"model": "m"}}
    p = Profile.model_validate(old)
    assert p.memory.enabled is True
    assert p.nickname == ""
