"""The TTS knobs one connection runs with, resolved from a TTS profile.

Every voice socket (api/routes/conversation.py, api/routes/livehost.py,
api/routes/lugo.py) resolves the same thing: a visible TtsProfile, if it pins an
engine, supplies engine/model/voice/ref-audio/instruct/speed/language; otherwise
the server default engine does, with the caller's own query params as the only
other source. Two of those three routes carried a byte-identical copy of the
mapping and the third a dict-shaped variant of it, so a field added to
TtsProfile reached some sockets and not others.

Resolving WHICH profile stays at the call site on purpose: each route reads its
own module-level `tts_profile_store` / `system_config_store`, which is what lets
a test scope a stub to one route.
"""

from __future__ import annotations

from typing import NamedTuple


class TtsParams(NamedTuple):
    """Unpacks positionally in the order the routes already bind these locals."""

    engine: str
    model_id: str
    voice: str | None
    ref_audio_path: str | None
    ref_text: str | None
    instruct: str | None
    speed: float | None
    language: str | None


def tts_params_from_profile(tts_profile, *, fallback_voice: str | None = None) -> TtsParams | None:
    """What `tts_profile` contributes, or None when it pins no engine.

    None (rather than a defaulted TtsParams) so the caller's fallback -- which
    reads the server default engine out of its own config store -- is only
    evaluated when it is actually needed, exactly as the inline `if/else` this
    replaces did.

    `fallback_voice` is the route's own ?voice=, used only where the profile
    itself leaves the voice blank; a profile that names a voice always wins.
    """
    if not (tts_profile and tts_profile.engine):
        return None
    return TtsParams(
        engine=tts_profile.engine,
        model_id=tts_profile.model_id or "",
        voice=tts_profile.voice or fallback_voice or None,
        ref_audio_path=tts_profile.ref_audio_path or None,
        ref_text=tts_profile.ref_text or None,
        instruct=tts_profile.instruct or None,
        speed=tts_profile.speed,
        language=tts_profile.language,
    )
