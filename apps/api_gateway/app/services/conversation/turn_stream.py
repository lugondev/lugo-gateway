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


class ReplyPacer:
    """The release clock for one reply's Opus frames.

    Holds the invariant this module's docstring promises: the stream never runs
    more than a prebuffer ahead of real playback, for the whole reply.

    Why that has to be enforced rather than assumed. The clock is anchored once,
    at the reply's first frame, and each later frame is due at
    ``t0 + (n - prebuffer) * frame_s``. When synthesis keeps up, frames arrive
    after their due time by a hair and the clock is self-correcting. When it does
    NOT keep up the clock falls into arrears -- and arrears are settled by
    emitting every overdue frame with no sleep at all, because each one's due
    time is already in the past.

    That is not a corner case here: every Vietnamese TTS engine we ship runs
    slower than real time (vieneu measures RTF 1.11-1.34), so the clock loses
    ``synth - audio`` seconds on every single sentence and the debt compounds
    across the reply. A 4-second sentence synthesized in 5.4s left the clock
    ~53 frames behind, and all 67 of that sentence's frames then went out as one
    burst. The ESP32's downlink queue is ``DL_QUEUE_DEPTH`` = 32 frames and
    ``dl_push`` is ``xQueueSend(..., 0)``, which drops silently when full
    (main.c's ``s_dl_drops``) -- so half the sentence never reached the speaker,
    and the device's Opus decoder resumed from stale state, which is audible as
    a warbly, drawn-out first word. Only the reply's FIRST sentence was safe,
    because nothing was in arrears yet.

    So: cap the arrears. Overdue frames still go out immediately -- the device is
    starved at that point and wants them -- but only a prebuffer's worth, after
    which the clock is re-anchored to now. Playback latency is unchanged (the
    frames the device can actually hold arrive just as fast); what goes away is
    the part of the burst it was always going to drop.
    """

    def __init__(self, prebuffer: int, frame_s: float) -> None:
        self._prebuffer = prebuffer
        self._frame_s = frame_s
        self._t0: float | None = None
        self._n = 0

    def delay_before_next(self, now: float) -> float:
        """Seconds to wait before releasing the next frame (<= 0 means now).

        Takes ``now`` instead of reading the clock so the pacing arithmetic can
        be tested for what it does, not how long it takes.
        """
        if self._t0 is None:
            self._t0 = now
        n, self._n = self._n, self._n + 1
        if n < self._prebuffer:
            return 0.0
        target = self._t0 + (n - self._prebuffer) * self._frame_s
        # Arrears beyond a prebuffer mean synthesis has fallen behind playback.
        # Re-anchor rather than pay the whole debt out at once (see class docs).
        max_arrears = self._prebuffer * self._frame_s
        if now - target > max_arrears:
            self._t0 += (now - target) - max_arrears
            target = self._t0 + (n - self._prebuffer) * self._frame_s
        return target - now


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
        # a synthesis failure has to be a value here and not an exception. The
        # pacing loop below is this module's own -- the livehost plugin's own
        # traffic inherits it too now, reaching this module over
        # /v1/conversation/stream rather than pacing its own copy the way
        # api/routes/livehost.py used to before it left this repo.
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
        _pacer = None   # built on the first packet, once the frame size is known
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
                # Release on the single global clock (see ReplyPacer).
                # First _prebuf frames of the reply go out immediately to
                # fill the device jitter buffer; every frame after that is
                # paced to real time, so a fast synth can't flood the
                # device. A slow synth catches up too, but only by a
                # prebuffer at a time -- an unbounded catch-up burst is
                # what used to overrun the device queue (ReplyPacer's
                # docstring has the numbers).
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
                        if _pacer is None:
                            _pacer = ReplyPacer(prebuffer=_prebuf, frame_s=_frame_s)
                        delay = _pacer.delay_before_next(time.monotonic())
                        if delay > 0:
                            await asyncio.sleep(delay)
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

