"""Shared TTS-request build + degrade classification (Task 6 / A1 dedup).

Lifts ONLY the ``TTSRequest(...)`` construction + degrade-classification step
that used to be duplicated at the top of api/routes/livehost.py's ``_synth``
closure and services/conversation/session.py's ``_synth`` closure -- the part
that catches the ref_audio_path-containment / any-other-construction-failure
(``TTSRequest``'s field_validator, schemas/tts.py) and turns it into a
per-sentence ``tts_error`` degrade instead of raising and unwinding the whole
turn (which would swallow already-generated response text).

Deliberately narrow: the provider call (`.render_audio()` / `.render_wav()`),
usage metering, opus encode, and pacing loop are each route's OWN
responsibility and stay exactly where they are -- session.py's global-clock
pacer differs from livehost's per-sentence pacer, and
tests/unit/test_paid_call_site_inventory.py pins the paid call sites
(`.render_audio()` et al.) to their current files. This helper never touches
either.
"""

from __future__ import annotations

from app.schemas.tts import TTSRequest


def build_tts_request_or_degrade(
    *,
    text: str,
    engine: str,
    model_id: str,
    voice: str | None,
    ref_audio_path: str | None,
    ref_text: str | None,
    instruct: str | None,
    speed: float | None,
    language: str | None,
) -> tuple[TTSRequest | None, Exception | None]:
    """(request, None) on success, (None, exc) when TTSRequest construction
    itself fails.

    Built INSIDE this guard (never before it, at the caller): a stored
    profile's ref_audio_path fails the artifacts-dir containment check, or
    any other future validation on this model, must still degrade to
    tts_error at the caller instead of raising and swallowing the
    already-generated LLM text, exactly like a downstream provider-call
    failure already does.
    """
    try:
        return (
            TTSRequest(
                text=text, engine=engine, model_id=model_id, voice=voice,
                ref_audio_path=ref_audio_path, ref_text=ref_text,
                instruct=instruct, speed=speed, language=language,
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to tts_error, don't unwind the turn
        return None, exc
