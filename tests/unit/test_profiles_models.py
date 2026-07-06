from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, Profile, TtsConfig


def test_profile_defaults():
    p = Profile(name="x")
    assert p.llm.base_url == ""
    assert p.tts.profile_name == ""
    assert p.mcp_servers == []
    assert p.system_prompt == ""


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
