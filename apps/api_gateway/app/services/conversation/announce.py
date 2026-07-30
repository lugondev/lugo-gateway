"""The one-line utterances the assistant says on its own initiative.

Two moments need one: a conversation has just been rotated away (`new_session`), and
the connection is about to be dropped for inactivity (`idle_goodbye`). Both used to be
silent or a fixed phrase from admin config -- the same sentence for every profile, no
matter what persona that profile defines.

Written by the profile's OWN LLM, with the tail of the conversation as context, so the
line stays in character and can refer to what was actually just said. Kept out of
session.py because the two things worth testing here -- what the model is asked, and
what is done to what it answers -- are testable without a session, a socket, or a
TTS engine.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# How much conversation goes in. Six messages is about three exchanges: enough for
# "the thing we were just talking about", short enough that the extra call stays a
# rounding error against the turns that produced it.
CONTEXT_MESSAGES = 6
CONTEXT_CHAR_BUDGET = 1200
# A spoken interjection, not a paragraph. Anything longer is a model that ignored the
# instruction, and a device reading it aloud would just be tedious.
MAX_LINE_CHARS = 200

_DIRECTIVES = {
    "new_session": (
        "The conversation above has just ended and a brand-new one is starting now, "
        "at the user's request. Say ONE short sentence out loud confirming the fresh "
        "start. You may nod to what was just discussed, but do not continue it and do "
        "not ask a question."
    ),
    "idle_goodbye": (
        "The user has gone quiet and you are about to disconnect. Say ONE short "
        "sentence out loud to say goodbye for now. You may nod to what was just "
        "discussed. Do not ask a question -- nobody will answer."
    ),
}

_OUTPUT_CONTRACT = (
    "You are about to SPEAK this, so: exactly one short sentence, plain text only, "
    "no quotation marks, no emoji, no stage directions, no markdown."
)


def _context_messages(history: list[dict]) -> list[dict]:
    """The newest messages that fit both budgets, oldest dropped first."""
    picked: list[dict] = []
    used = 0
    for message in reversed(history[-CONTEXT_MESSAGES:]):
        content = str(message.get("content") or "")
        if not content:
            continue
        if used + len(content) > CONTEXT_CHAR_BUDGET and picked:
            break
        picked.append({"role": message.get("role") or "user", "content": content})
        used += len(content)
    picked.reverse()
    return picked


def clean_line(raw: str) -> str:
    """Turn a model answer into something worth handing to a TTS engine.

    Models routinely answer with the sentence in quotes, sometimes with a parenthetical
    remark on the next line. Read aloud, the quote marks and the aside are both
    artifacts -- so take the first real line and unwrap it."""
    for candidate in (raw or "").splitlines():
        line = candidate.strip()
        if not line:
            continue
        for quote in ('"', "'", "“", "”", "«", "»"):
            line = line.strip(quote)
        line = line.strip()
        if not line:
            continue
        if len(line) > MAX_LINE_CHARS:
            cut = line[:MAX_LINE_CHARS]
            # Prefer a word boundary: half a word is worse than a short sentence.
            if " " in cut:
                cut = cut[: cut.rindex(" ")]
            line = cut.rstrip()
        return line
    return ""


async def generate_line(
    *,
    responder,
    persona: str | None,
    history: list[dict],
    language: str | None,
    event: str,
) -> str:
    """Ask `responder` for the line to speak for `event`.

    Raises on an LLM failure and on an answer with nothing in it, rather than
    substituting a phrase of its own: the caller reports the failure to the client
    (so a device shows WHY it went quiet) and says nothing. A built-in fallback
    sentence would just be the hardcoded phrase this replaces, one layer down.
    """
    directive = _DIRECTIVES[event]
    if language:
        language_rule = f"Answer in this language: {language}."
    else:
        # No profile language: mirroring the context beats defaulting to English on a
        # Vietnamese device.
        language_rule = "Answer in the same language the user has been speaking."

    messages: list[dict] = []
    if persona:
        messages.append({"role": "system", "content": persona})
    messages.append({"role": "system", "content": f"{_OUTPUT_CONTRACT} {language_rule}"})
    messages.extend(_context_messages(history))
    messages.append({"role": "user", "content": directive})

    raw = await responder.reply(messages)
    line = clean_line(raw)
    if not line:
        raise ValueError(f"{event}: the model returned nothing to say")
    logger.info("announce %s: %s", event, line)
    return line
