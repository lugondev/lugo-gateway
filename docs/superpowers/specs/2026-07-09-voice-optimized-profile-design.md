# Voice-optimized responses per profile

**Date:** 2026-07-09
**Status:** Approved

## Problem

Assistant responses are read aloud via TTS. LLMs emit markdown, emoji, symbols,
bullet lists, raw URLs, and glyphs like `%`/`$` that the voice engine mispronounces
or reads as literal punctuation. We need a per-profile switch that instructs the LLM
to produce plain, speakable text.

## Scope

- One new per-profile boolean flag.
- A built-in (non-configurable) directive appended to the system prompt when on.
- Applies to every voice path: conversation REST, conversation WebSocket (lugo
  device), and livehost co-host.

Out of scope: user-editable directive text, per-language variants.

## Data model

`services/profiles/models.py` — add to `Profile`:

```python
voice_optimized: bool = False   # append the speakable-text directive to the system prompt
```

Mirror the field in `ProfileRequest` (`api/routes/profiles.py`).

## Injection

Single chokepoint: `resolve_system_prompt` in
`services/conversation/responder.py`. Signature becomes:

```python
def resolve_system_prompt(system_prompt: str | None, voice_optimized: bool = False) -> str:
```

When `voice_optimized` is true, append `VOICE_OPTIMIZATION_DIRECTIVE` (a module-level
constant) to the end of the composed prompt. Because `inject_memories` *prepends* its
block and `base_context` is prepended, the directive stays last in every path:

```
[memories] + base_context + persona + VOICE_OPTIMIZATION_DIRECTIVE
```

### Threading the flag (3 call sites)

- `build_responder_ex(..., voice_optimized: bool = False)` forwards it to
  `resolve_system_prompt`. Covers `api/routes/conversation.py` (REST) and
  `api/routes/livehost.py`.
- `services/conversation/session.py` calls `resolve_system_prompt` directly — pass
  the flag there.
- Each call site reads `profile.voice_optimized` (guarding for `profile is None`).

## The directive (Vietnamese)

A constant instructing the LLM to output plain speakable Vietnamese:
- No markdown / formatting characters (`* _ # \` ~ | > -`, headings, bold/italic, code blocks).
- No emoji, symbols, or special glyphs.
- No bullet or numbered lists; enumerate inside sentences ("thứ nhất", "thứ hai").
- Spell out `%`, `$`, numbers, units, dates ("50 phần trăm", "đô la").
- No raw URLs/links; describe them in words instead.
- Short, natural, conversational sentences.

## UI

`static/js/profiles.js` + its form template: a checkbox bound into the
create/update payload, defaulting to unchecked.

## Testing (TDD)

Unit tests:
- `resolve_system_prompt` appends the directive iff `voice_optimized=True`; omits it otherwise.
- `Profile.voice_optimized` defaults to `False`.
- Round-trip through the profiles route + store preserves the flag.
- `build_responder_ex` forwards the flag (directive present in the built responder's prompt).
