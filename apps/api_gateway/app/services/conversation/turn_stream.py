"""Sending one reply out to the client, sentence by sentence.

Lifted out of ``ConversationSession._run_turn``, where it was a ~140-line
closure -- half the function -- capturing six pieces of session state plus the
turn's own bookkeeping. Everything it needs is now named: the session it speaks
for, and the two per-turn values (`turn`, `log_first_chunk`).

It takes the session rather than being a method so that this file can be read,
and the pacing behaviour reasoned about, without ``session.py`` open beside it.
That pacing is the whole reason the module earns its keep: the release clock
below is global to the reply, and getting it wrong is audible.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing

from app.services.conversation.turn_tts import synthesize_or_degrade
from app.services.system_config import system_config_store
from app.services.tts.streaming import prefetch_synthesis

logger = logging.getLogger(__name__)


async def stream_reply(
    session, sentence_aiter, responder_name: str, *, turn: int, log_first_chunk
) -> list[str]:
    """Stream a sentence iterator out to the client, synthesizing as it goes.

    For audio output we synthesize up to ``conversation_tts_lookahead``
    sentences AHEAD of sending (prefetch_synthesis), so the next sentence's
    audio is usually ready before the current one finishes -> gapless playback.
    Text-only just emits sentences as the LLM streams them.
    """
    cfg = session.cfg
    want_audio = cfg.want_audio
    want_text = cfg.want_text
    _log_first_chunk = log_first_chunk
    parts: list[str] = []

    if not want_audio:
        index = 0
        async for sentence in sentence_aiter:
            _log_first_chunk()
            parts.append(sentence)
            if want_text:
                await session.emit("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
            index += 1
        return parts

    async def _synth(sentence: str):
        # (result, packets, error) -- see turn_tts.synthesize_or_degrade for why
        # a synthesis failure has to be a value here and not an exception. Only
        # the pacing loop below is this module's own; livehost paces differently
        # and deliberately.
        return await synthesize_or_degrade(
            sentence,
            provider=session.tts_provider,
            record_usage=session._record_tts_usage,
            opus_encoder=session.opus_encoder,
            output_sample_rate=cfg.output_sample_rate,
            engine=cfg.tts_engine, model_id=cfg.tts_model, voice=cfg.voice,
            ref_audio_path=cfg.ref_audio_path, ref_text=cfg.ref_text,
            instruct=cfg.tts_instruct, speed=cfg.tts_speed, language=cfg.tts_language,
        )

    async with aclosing(
        prefetch_synthesis(
            sentence_aiter, _synth,
            lookahead=system_config_store.get().conversation.conversation_tts_lookahead,
        )
    ) as pipeline:
        # Global real-time pacer for the WHOLE reply: prebuffer the first
        # few frames, then release every frame on one monotonic clock.
        # Per-sentence pacing used to prebuffer-burst at each sentence, so
        # multi-sentence replies accumulated in the device jitter buffer
        # and overflowed (dropped words on long replies). A single clock
        # keeps the device buffer ~prebuffer-deep for the entire reply.
        _conv_cfg = system_config_store.get().conversation
        _do_pace = cfg.opus_pace if cfg.opus_pace is not None else _conv_cfg.conversation_opus_pace
        _prebuf = _conv_cfg.conversation_opus_prebuffer_frames
        _pace_t0 = None
        _pace_n = 0
        tts_error_reported = False
        # NB: nothing in this loop may log `sentence` (or the transcript
        # it answers). The stage timings below are deliberately
        # content-free -- a debugging pass once logged every synthesized
        # sentence at INFO, which put private conversation content into
        # the server log for every turn.
        async for index, sentence, (audio, packets, tts_error) in pipeline:
            _log_first_chunk()
            parts.append(sentence)
            if want_text:
                await session.emit("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
            if tts_error is not None:
                # Synth failed for this sentence: text already went out above.
                # Report the TTS failure once per turn (a fully-down engine
                # would otherwise emit one per sentence) and skip audio -- the
                # client falls back to showing text only.
                if not tts_error_reported:
                    tts_error_reported = True
                    await session.emit(
                        "tts_error", turn=turn, chunk_index=index,
                        engine=cfg.tts_engine, message=str(tts_error),
                    )
                continue
            if packets is not None:
                # Mark when the assistant first starts speaking this turn,
                # so feed_audio can ignore onset echo as barge-in.
                if session._speaking_since is None:
                    session._speaking_since = time.monotonic()
                # Push Opus binary frames bracketed by audio_start/audio_end (devices).
                await session.emit(
                    "audio_start", turn=turn, chunk_index=index,
                    text=sentence if want_text else None,
                    codec="opus", sample_rate=cfg.output_sample_rate, frames=len(packets),
                )
                # Release on the single global clock (see _pace_* above).
                # First _prebuf frames of the reply go out immediately to
                # fill the device jitter buffer; every frame after that is
                # paced to real time, so a fast synth can't flood the
                # device and a slow one just catches up (no per-sentence
                # burst accumulation).
                #
                # Frame duration is read HERE, not before the loop: a
                # session that negotiated no Opus (wav mode) has
                # session.opus_encoder is None for the whole turn, and
                # touching it eagerly crashed every such turn. Inside
                # this branch the encoder is guaranteed -- packets
                # only exist when it does -- same as speak().
                _frame_s = session.opus_encoder.frame / session.opus_encoder.sample_rate
                for pkt in packets:
                    if _do_pace:
                        if _pace_t0 is None:
                            _pace_t0 = time.monotonic()
                        if _pace_n >= _prebuf:
                            target = _pace_t0 + (_pace_n - _prebuf) * _frame_s
                            now = time.monotonic()
                            if target > now:
                                await asyncio.sleep(target - now)
                        _pace_n += 1
                    await session.emit_audio(pkt)
                await session.emit("audio_end", turn=turn, chunk_index=index)
            else:
                audio_bytes, media_type = audio
                if session._speaking_since is None:
                    session._speaking_since = time.monotonic()
                await session.emit(
                    "audio_start", turn=turn, chunk_index=index,
                    text=sentence if want_text else None,
                    codec="mp3" if media_type == "audio/mpeg" else "wav",
                )
                await session.emit_audio(audio_bytes)
                await session.emit("audio_end", turn=turn, chunk_index=index)
    return parts

