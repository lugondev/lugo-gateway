"""Tool/function-calling primitives for the voice assistant.

A ``Tool`` is a named, JSON-schema-described async callable the LLM can invoke.
``ToolSource`` is the pluggable seam (local Python tools, an MCP adapter, …) and
``ToolRegistry`` aggregates sources and dispatches calls. ``ToolContext`` gives a
tool a way to reach the connected device (``send_command``) without coupling it to
the WebSocket. See docs/superpowers/specs/2026-07-01-voice-function-calling-design.md
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Per-turn context handed to a tool's ``run``.

    ``emit_command`` (async) pushes an event to the connected device over the WS;
    it may be None (e.g. a text-only client) — ``send_command`` then no-ops.

    ``end_conversation`` arms "hang up once you have finished speaking". It does
    NOT disconnect on the spot: the reply the model is about to give IS the
    goodbye, so the connection closes when that utterance has been played, not
    while it is being written. None on transports with no such notion (a browser
    tab stays open) — ``request_end`` then no-ops.
    """

    emit_command: Callable[[dict], Awaitable[None]] | None = None
    language: str | None = None
    end_conversation: Callable[[str], None] | None = None

    async def send_command(self, payload: dict) -> None:
        if self.emit_command is not None:
            await self.emit_command(payload)

    def request_end(self, reason: str) -> bool:
        """Returns whether this transport can actually hang up."""
        if self.end_conversation is None:
            return False
        self.end_conversation(reason)
        return True


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema for the arguments object
    run: Callable[[dict, ToolContext], Awaitable[str]]

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolSource(ABC):
    """A provider of tools. Implementations: LocalToolSource, McpToolSource."""

    @abstractmethod
    def list_tools(self) -> list[Tool]:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self, sources: list[ToolSource] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for source in sources or []:
            self.add_source(source)

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def add_source(self, source: ToolSource) -> None:
        for tool in source.list_tools():
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __len__(self) -> int:
        return len(self._tools)

    def openai_schema(self) -> list[dict]:
        return [tool.openai_schema() for tool in self._tools.values()]

    async def run(self, name: str, args: dict, ctx: ToolContext) -> str:
        """Dispatch a tool call. Unknown tool or a raised error becomes an error
        string (the model sees it and can recover) — never propagates."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'"
        try:
            return await tool.run(args or {}, ctx)
        except Exception as exc:  # noqa: BLE001 - a tool failure must not crash the turn
            logger.warning("tool %s failed: %s", name, exc)
            return f"Error running {name}: {exc}"
