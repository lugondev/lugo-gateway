from app.services.mcp.models import McpServer
from app.services.profiles.models import LlmConfig, Profile, TtsConfig


def test_profile_defaults():
    p = Profile(name="x")
    assert p.llm.base_url == ""
    assert p.tts.engine == ""
    assert p.mcp_servers == []
    assert p.system_prompt == ""


def test_profile_full():
    p = Profile(
        name="home",
        llm=LlmConfig(base_url="http://localhost:11434/v1", api_key="", model="llama3.2"),
        system_prompt="You are a home assistant.",
        tts=TtsConfig(engine="vieneu", voice=""),
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
