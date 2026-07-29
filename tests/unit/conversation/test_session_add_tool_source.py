from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.conversation.tools.base import Tool, ToolSource, ToolContext


class _OneToolSource(ToolSource):
    def list_tools(self):
        async def run(args, ctx): return "ok"
        return [Tool(name="t1", description="", parameters={"type": "object"}, run=run)]


def _cfg():
    return SessionRuntimeConfig(
        session_id="s", profile_name=None, stt_engine="x", language=None,
        tts_engine="x", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=16000,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=False,
        want_text=True, audio_out="url", denoise=False, resume_sid=None,
    )


async def _noop_emit(*a, **k): ...


def test_add_tool_source_creates_registry_when_none():
    s = ConversationSession(_cfg(), _noop_emit, _noop_emit)
    assert s.tool_registry is None
    s.add_tool_source(_OneToolSource())
    assert s.tool_registry is not None
    assert s.tool_registry.get("t1") is not None


def test_add_tool_source_appends_to_existing_registry():
    from app.services.conversation.tools.base import ToolRegistry
    s = ConversationSession(_cfg(), _noop_emit, _noop_emit)
    s.tool_registry = ToolRegistry([])
    s.add_tool_source(_OneToolSource())
    assert s.tool_registry.get("t1") is not None


class _CollidingToolSource(ToolSource):
    """Advertises a tool named 't1' with different (device-side) behavior."""
    def list_tools(self):
        async def run(args, ctx): return "device version"
        return [Tool(name="t1", description="", parameters={"type": "object"}, run=run)]


def test_add_tool_source_does_not_shadow_existing_tool_on_collision():
    from app.services.conversation.tools.base import ToolRegistry

    s = ConversationSession(_cfg(), _noop_emit, _noop_emit)
    s.tool_registry = ToolRegistry([_OneToolSource()])
    assert s.tool_registry.get("t1") is not None

    s.add_tool_source(_CollidingToolSource())

    tool = s.tool_registry.get("t1")
    assert tool is not None
    import asyncio
    result = asyncio.run(tool.run({}, ToolContext()))
    assert result == "ok"
