"""Prompt construction and output cleaning for spoken announcements.

The line a device says when a conversation rotates or when it is about to go idle
is written by the profile's own LLM, in that profile's voice, with the tail of the
conversation as context -- not a fixed phrase. These tests pin the parts worth
pinning: what the model is asked, and what is done to what it answers.
"""

import pytest

from app.services.conversation.announce import (
    CONTEXT_CHAR_BUDGET,
    CONTEXT_MESSAGES,
    MAX_LINE_CHARS,
    generate_line,
)


class _CapturingResponder:
    """Records the history it was handed and replies with a canned string."""

    name = "capturing"

    def __init__(self, reply_text: str = "Mình bắt đầu lại nha!"):
        self.reply_text = reply_text
        self.seen: list[dict] = []

    async def reply(self, history: list[dict]) -> str:
        self.seen = list(history)
        return self.reply_text


class _FailingResponder:
    name = "failing"

    async def reply(self, history: list[dict]) -> str:
        raise RuntimeError("llm down")


def _text(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages)


@pytest.mark.asyncio
async def test_the_prompt_carries_the_persona_and_the_conversation_tail():
    responder = _CapturingResponder()
    history = [
        {"role": "user", "content": "mình thích ăn phở"},
        {"role": "assistant", "content": "phở bò hay phở gà?"},
    ]

    await generate_line(
        responder=responder, persona="Bạn là Lugo, một trợ lý vui vẻ.",
        history=history, language="vi", event="new_session",
    )

    prompt = _text(responder.seen)
    # In character: without the profile's own system prompt the line would be
    # generic assistant-speak, which is the whole thing this replaces.
    assert "Lugo" in prompt
    # With context: the point of paying for context is that the line can refer to
    # what was just said.
    assert "phở" in prompt


@pytest.mark.asyncio
async def test_only_the_last_messages_are_sent_as_context():
    responder = _CapturingResponder()
    history = [{"role": "user", "content": f"message-{i}"} for i in range(CONTEXT_MESSAGES + 6)]

    await generate_line(
        responder=responder, persona="p", history=history, language="vi", event="idle_goodbye",
    )

    prompt = _text(responder.seen)
    assert f"message-{len(history) - 1}" in prompt   # newest kept
    assert "message-0" not in prompt                 # oldest dropped


@pytest.mark.asyncio
async def test_context_is_capped_by_character_budget():
    responder = _CapturingResponder()
    # Every message alone nearly fills the budget, so only the newest can survive
    # even though CONTEXT_MESSAGES would allow more.
    big = "x" * (CONTEXT_CHAR_BUDGET - 10)
    history = [
        {"role": "user", "content": "OLDEST" + big},
        {"role": "assistant", "content": "NEWEST" + big},
    ]

    await generate_line(
        responder=responder, persona="p", history=history, language="vi", event="new_session",
    )

    prompt = _text(responder.seen)
    assert "NEWEST" in prompt
    assert "OLDEST" not in prompt


@pytest.mark.asyncio
async def test_the_language_is_pinned_when_the_profile_sets_one():
    responder = _CapturingResponder()
    await generate_line(
        responder=responder, persona="p", history=[], language="vi", event="new_session",
    )
    assert "vi" in _text(responder.seen)


@pytest.mark.asyncio
async def test_with_no_profile_language_the_model_is_told_to_mirror_the_user():
    """A profile with no language must not silently become English: the model is
    told to answer in whatever language the context is in."""
    responder = _CapturingResponder()
    await generate_line(
        responder=responder, persona="p", history=[{"role": "user", "content": "xin chào"}],
        language="", event="new_session",
    )
    prompt = _text(responder.seen).lower()
    assert "same language" in prompt


@pytest.mark.asyncio
async def test_the_two_events_ask_for_different_things():
    rotate = _CapturingResponder()
    await generate_line(responder=rotate, persona="p", history=[], language="vi",
                        event="new_session")
    goodbye = _CapturingResponder()
    await generate_line(responder=goodbye, persona="p", history=[], language="vi",
                        event="idle_goodbye")

    assert _text(rotate.seen) != _text(goodbye.seen)


@pytest.mark.asyncio
async def test_quotes_and_extra_lines_are_stripped():
    """Models like to answer with a quoted sentence, sometimes with a preamble.
    Speaking the quote marks out loud is a TTS artifact, not a greeting."""
    responder = _CapturingResponder('"Chào nha!"\n\n(một câu ngắn gọn)')
    line = await generate_line(responder=responder, persona="p", history=[],
                               language="vi", event="new_session")
    assert line == "Chào nha!"


@pytest.mark.asyncio
async def test_an_over_long_answer_is_truncated_at_a_word_boundary():
    responder = _CapturingResponder(" ".join(["word"] * 200))
    line = await generate_line(responder=responder, persona="p", history=[],
                               language="vi", event="new_session")
    assert len(line) <= MAX_LINE_CHARS
    assert not line.endswith("wor")   # cut between words, not mid-word


@pytest.mark.asyncio
async def test_an_empty_answer_raises_so_the_caller_can_report_it():
    """Silence must be reported, not passed off as a spoken line: announce()
    turns this into an error event the device shows on its panel."""
    responder = _CapturingResponder("   \n  ")
    with pytest.raises(ValueError):
        await generate_line(responder=responder, persona="p", history=[],
                            language="vi", event="new_session")


@pytest.mark.asyncio
async def test_an_llm_failure_propagates():
    with pytest.raises(RuntimeError):
        await generate_line(responder=_FailingResponder(), persona="p", history=[],
                            language="vi", event="new_session")
