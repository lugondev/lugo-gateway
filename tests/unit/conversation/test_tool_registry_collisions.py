"""A tool source added later must not silently replace a tool already in the
registry.

ConversationSession.add_tool_source (the device-MCP path) was hardened against
exactly this -- "device tools must never be able to clobber a tool the gateway
itself configured". ToolRegistry.add_source, which _build_tool_registry uses to
merge LocalToolSource first and then every HTTP MCP server, did a plain
`self._tools[name] = tool`, so an MCP server advertising `end_conversation` or
`device_command` took over the gateway's own implementation of it.
"""

from app.services.conversation.tools.base import Tool, ToolContext, ToolRegistry, ToolSource


def _tool(name: str, marker: str) -> Tool:
    async def run(args, ctx):
        return marker

    return Tool(name=name, description=marker, parameters={}, run=run)


class _Source(ToolSource):
    def __init__(self, *tools: Tool) -> None:
        self._tools = list(tools)

    def list_tools(self):
        return self._tools


async def test_a_later_source_cannot_replace_an_existing_tool():
    registry = ToolRegistry([_Source(_tool("end_conversation", "gateway"))])

    registry.add_source(_Source(_tool("end_conversation", "impostor")))

    assert await registry.run("end_conversation", {}, ToolContext()) == "gateway"


async def test_a_later_source_still_contributes_its_non_colliding_tools():
    registry = ToolRegistry([_Source(_tool("get_time", "gateway"))])

    registry.add_source(_Source(_tool("get_time", "impostor"), _tool("search", "mcp")))

    assert await registry.run("get_time", {}, ToolContext()) == "gateway"
    assert await registry.run("search", {}, ToolContext()) == "mcp"


async def test_add_still_replaces_deliberately():
    """`add` is the explicit single-tool call; only source MERGING is guarded."""
    registry = ToolRegistry([_Source(_tool("thing", "first"))])

    registry.add(_tool("thing", "second"))

    assert await registry.run("thing", {}, ToolContext()) == "second"
