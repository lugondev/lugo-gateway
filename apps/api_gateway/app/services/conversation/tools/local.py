"""Built-in tools that run locally inside the service process."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .base import Tool, ToolContext, ToolSource


class LocalToolSource(ToolSource):
    """Provides get_time and device_command as built-in tools.

    ``clock`` is an optional zero-arg callable that returns a datetime; it
    defaults to ``datetime.now()`` at call time and can be injected in tests.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or datetime.now

    def list_tools(self) -> list[Tool]:
        clock = self._clock
        return [
            Tool(
                name="get_time",
                description="Return the current local time.",
                parameters={"type": "object", "properties": {}},
                run=_make_get_time(clock),
            ),
            Tool(
                name="device_command",
                description=(
                    "Send a command to the connected device. "
                    "action is required (e.g. 'led_on'); params is an optional dict."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "Command name"},
                        "params": {"type": "object", "description": "Optional parameters"},
                    },
                    "required": ["action"],
                },
                run=_device_command,
            ),
        ]


def _make_get_time(clock: Callable[[], datetime]):
    async def get_time(args: dict, ctx: ToolContext) -> str:
        now = clock()
        return now.strftime("%H:%M")

    return get_time


async def _device_command(args: dict, ctx: ToolContext) -> str:
    action = args.get("action")
    if not action:
        return "Error: 'action' is required"
    params = args.get("params") or {}
    await ctx.send_command({"event": "device_command", "action": action, "params": params})
    return f"Command '{action}' sent to device"
