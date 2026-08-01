"""Synthesizing one sentence, and degrading instead of unwinding the turn.

Both voice paths -- conversation/turn_stream.py and api/routes/livehost.py --
feed a sentence iterator through TTS, and both must obey the same rule: a
failure anywhere in synthesis becomes a value, never an exception. Raising
propagates out of prefetch_synthesis and unwinds the whole turn, which throws
away every not-yet-sent sentence's ``response_text`` -- the LLM's words have to
survive a TTS outage.

That rule was encoded twice. ``build_tts_request_or_degrade`` already shared the
construction half; ``synthesize_or_degrade`` now covers the rest of it (provider
call, metering, Opus encode), so the contract has one home.

Still NOT shared, deliberately: the pacing loop that releases those packets.
session/turn_stream.py runs one global clock for the whole reply;
api/routes/livehost.py re-prebuffers per sentence. That is a real behavioural
difference, not an accident of copying -- see turn_stream.py's pacer comment for
which one is the fix and why.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.audio import wav_bytes_to_pcm16
from app.schemas.tts import TTSRequest

logger = logging.getLogger(__name__)


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


async def synthesize_or_degrade(
    sentence: str,
    *,
    provider,
    record_usage,
    opus_encoder=None,
    output_sample_rate: int = 24000,
    log_label: str = "TTS",
    engine: str,
    model_id: str,
    voice: str | None,
    ref_audio_path: str | None,
    ref_text: str | None,
    instruct: str | None,
    speed: float | None,
    language: str | None,
) -> tuple[tuple[bytes, str] | None, list[bytes] | None, Exception | None]:
    """Synthesize one sentence as ``(result, packets, error)``; exactly one is set.

    * ``packets`` when the caller negotiated Opus -- the WAV is decoded and
      re-encoded on worker threads, never on the event loop.
    * ``result`` is ``(audio_bytes, media_type)`` otherwise.
    * ``error`` for ANY failure, including TTSRequest construction. The caller
      emits this sentence's ``response_text`` regardless and a ``tts_error`` for
      the audio alone.

    ``record_usage`` is an async callable taking the sentence. It is expected to
    swallow its own errors: metering must never break a turn, and each caller
    already attributes the row differently (the session reads its own cfg, the
    livehost socket its own locals).

    ``asyncio.CancelledError`` is deliberately re-raised rather than degraded --
    it means barge-in or a superseded turn, which MUST unwind.
    """
    request, build_exc = build_tts_request_or_degrade(
        text=sentence, engine=engine, model_id=model_id, voice=voice,
        ref_audio_path=ref_audio_path, ref_text=ref_text,
        instruct=instruct, speed=speed, language=language,
    )
    if build_exc is not None:
        logger.warning("%s synth failed (engine=%s) for %r: %s", log_label, engine, sentence, build_exc)
        return None, None, build_exc
    try:
        audio, media_type = await provider.render_audio(request)
        await record_usage(sentence)
        if opus_encoder is not None:
            pcm = await asyncio.to_thread(wav_bytes_to_pcm16, audio, output_sample_rate)
            packets = await asyncio.to_thread(opus_encoder.encode_pcm16, pcm)
            return None, packets, None
        return (audio, media_type), None, None
    except asyncio.CancelledError:
        raise  # barge-in / turn supersede -- must propagate to unwind the turn
    except Exception as exc:  # noqa: BLE001 - degrade to text-only, don't lose the reply
        logger.warning("%s synth failed (engine=%s) for %r: %s", log_label, engine, sentence, exc)
        return None, None, exc
