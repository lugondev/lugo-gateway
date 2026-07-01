"""MCP (Model Context Protocol) tool adapter.

``mcp_tool_to_tool`` converts a single MCP tool definition dict into a ``Tool``
backed by an injected async invoker.  ``McpToolSource`` wraps a list of such
definitions as a ``ToolSource``.  The transport (how the invoker actually reaches
an MCP server) is injected by the caller — this module stays transport-agnostic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .base import Tool, ToolContext, ToolSource


Invoker = Callable[[str, dict], Awaitable[str]]


def mcp_tool_to_tool(mcp_def: dict, invoker: Invoker) -> Tool:
    """Convert a single MCP tool definition dict into a ``Tool``.

    ``mcp_def`` is expected to have at least ``name``; ``description`` and
    ``inputSchema`` are optional.  ``inputSchema`` maps to ``parameters``; when
    absent ``{"type": "object"}`` is used as a safe default.
    """
    name: str = mcp_def["name"]
    description: str = mcp_def.get("description", "")
    parameters: dict = mcp_def.get("inputSchema") or {"type": "object"}

    async def run(args: dict, ctx: ToolContext) -> str:
        return await invoker(name, args)

    return Tool(name=name, description=description, parameters=parameters, run=run)


class McpToolSource(ToolSource):
    """A ``ToolSource`` backed by a list of MCP tool definitions and an invoker.

    Args:
        tool_defs: List of MCP tool definition dicts (``name``, ``description``,
            ``inputSchema``).
        invoker: ``async (name, args) -> str`` called when a tool runs.
    """

    def __init__(self, tool_defs: list[dict], invoker: Invoker) -> None:
        self._tool_defs = tool_defs
        self._invoker = invoker

    def list_tools(self) -> list[Tool]:
        return [mcp_tool_to_tool(d, self._invoker) for d in self._tool_defs]
