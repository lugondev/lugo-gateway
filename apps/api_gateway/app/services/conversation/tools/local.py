"""Built-in tools that run locally inside the service process."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .base import Tool, ToolContext, ToolSource


class LocalToolSource(ToolSource):
    """Provides get_time, device_command and end_conversation as built-in tools.

    ``utilities`` gates get_time/device_command behind
    settings.conversation_tools_enabled, the switch for optional local tools.
    ``end_conversation`` is NOT one of those: hanging up when the user says
    goodbye is conversation behaviour, not a utility, and a deployment with
    utilities switched off still needs it -- the alternative is an assistant that
    says "goodbye" and leaves the microphone open. (Found the hard way: with
    utilities off the tool was never registered, so the model was being told to
    call something that did not exist.)

    ``clock`` is an optional zero-arg callable that returns a datetime; it
    defaults to ``datetime.now()`` at call time and can be injected in tests.
    """

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        utilities: bool = True,
        end_conversation: bool = False,
    ) -> None:
        self._clock = clock or datetime.now
        self._utilities = utilities
        self._end_conversation = end_conversation

    def list_tools(self) -> list[Tool]:
        clock = self._clock
        tools: list[Tool] = []
        if self._end_conversation:
            tools.append(_END_CONVERSATION_TOOL)
        if not self._utilities:
            return tools
        return tools + [
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


async def _end_conversation(args: dict, ctx: ToolContext) -> str:
    """Arms the hang-up; the model's next words are what the user actually hears.

    Deliberately no `say_goodbye` argument: whatever the model would put in it is
    the same thing it is about to say anyway, and a tool argument would either be
    spoken twice or replace a reply written with the conversation in view."""
    if not ctx.request_end("user_goodbye"):
        # A browser tab has no "hang up" -- say so rather than let the model
        # promise something that will not happen.
        return "This client stays connected; just say goodbye without disconnecting."
    return "Ending after you speak. Say a short goodbye now, in your own words."


_END_CONVERSATION_TOOL = Tool(
    name="end_conversation",
    description=(
        "Call this when the user is done talking -- they say goodbye, ask you to "
        "stop, to be quiet, to turn off, or otherwise signal the conversation is "
        "over ('tạm biệt', 'tắt đi', 'thôi nhé', 'that's all', 'bye'). Say your "
        "goodbye in the reply that follows this call: the device plays it and "
        "disconnects once you have finished speaking. Do NOT call it when the user "
        "is merely quiet, changing the subject, or thanking you."
    ),
    parameters={"type": "object", "properties": {}},
    run=_end_conversation,
)


async def _device_command(args: dict, ctx: ToolContext) -> str:
    action = args.get("action")
    if not action:
        return "Error: 'action' is required"
    params = args.get("params") or {}
    await ctx.send_command({"event": "device_command", "action": action, "params": params})
    return f"Command '{action}' sent to device"
