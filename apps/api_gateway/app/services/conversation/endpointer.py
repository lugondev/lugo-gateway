"""VAD-based turn endpointing for voice conversation.

Ingests raw PCM16 mono frames and detects end-of-turn from trailing silence,
so the server knows when the user has finished speaking (turn-taking). Energy
(RMS) based — cheap, streaming-friendly, no heavy model per frame.
"""

from collections import deque

import numpy as np

from app.core.audio import pcm16_to_float_array


def barge_in_suppressed(
    speaking_since: float | None, now: float, grace_ms: float
) -> bool:
    """True if a detected speech onset should be ignored as likely echo.

    ``speaking_since`` is the monotonic time the assistant started emitting audio
    for the current turn (None when it isn't speaking). Within ``grace_ms`` of
    that moment, a speech_start is almost certainly the assistant's own audio
    echoed into the mic, not the user barging in -- so it is suppressed. After
    the grace window (or when the assistant isn't speaking) barge-in is allowed.
    """
    if speaking_since is None or grace_ms <= 0:
        return False
    return (now - speaking_since) * 1000.0 < grace_ms


class VadEndpointer:
    def __init__(
        self,
        sample_rate: int,
        silence_ms: int = 700,
        min_speech_ms: int = 300,
        rms_threshold: float = 0.015,
        max_utterance_ms: int = 30000,
        noise_factor: float = 2.5,
        preroll_ms: int = 300,
        min_silence_ms: int | None = None,
        adaptive_full_ms: int = 3000,
    ) -> None:
        self.sample_rate = sample_rate
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.rms_threshold = rms_threshold  # absolute floor for "speech"
        self.max_utterance_ms = max_utterance_ms
        self.noise_factor = noise_factor
        self.preroll_ms = preroll_ms  # lead-in kept before speech is detected
        # Adaptive endpointing: required trailing silence shrinks from silence_ms
        # toward min_silence_ms as the utterance length approaches adaptive_full_ms.
        # min_silence_ms=None (or >= silence_ms) disables adaptation (fixed window).
        self.min_silence_ms = silence_ms if min_silence_ms is None else min_silence_ms
        self.adaptive_full_ms = adaptive_full_ms
        self.reset()

    def reset(self) -> None:
        self._collected = bytearray()
        self._speaking = False
        self._silence_acc_ms = 0.0
        self._speech_ms = 0.0
        self._noise = self.rms_threshold  # adaptive ambient-noise floor
        self._preroll: deque[tuple[bytes, float]] = deque()  # recent pre-speech frames
        self._preroll_ms = 0.0

    @property
    def speaking(self) -> bool:
        return self._speaking

    def accept(self, pcm: bytes) -> dict | None:
        """Feed a PCM frame. Returns an event dict or None.

        Speech is detected relative to an adaptive noise floor (so constant mic
        hiss / room noise above a fixed threshold doesn't read as endless speech).

        Events: {"event": "speech_start"} when speech begins, or
        {"event": "endpoint", "audio": <pcm bytes>, "speech_ms": float} when a
        complete utterance is detected.
        """
        samples = pcm16_to_float_array(pcm)
        if samples.size == 0:
            return None

        frame_ms = 1000.0 * samples.size / self.sample_rate
        rms = float(np.sqrt(np.mean(samples**2)))
        speech_level = max(self.rms_threshold, self._noise * self.noise_factor)
        is_speech = rms > speech_level
        if not is_speech:
            # Learn the ambient floor from quiet frames (slow exponential update).
            self._noise = 0.95 * self._noise + 0.05 * rms

        if is_speech:
            started = not self._speaking
            if started:
                # Prepend the buffered lead-in so the word onset isn't clipped.
                for frame, _ in self._preroll:
                    self._collected.extend(frame)
                self._preroll.clear()
                self._preroll_ms = 0.0
            self._speaking = True
            self._collected.extend(pcm)
            self._speech_ms += frame_ms
            self._silence_acc_ms = 0.0
            if self._speech_ms >= self.max_utterance_ms:
                return self._emit_endpoint()
            return {"event": "speech_start"} if started else None

        # silence frame
        if self._speaking:
            self._collected.extend(pcm)  # keep a little trailing silence for the decoder
            self._silence_acc_ms += frame_ms
            if (
                self._silence_acc_ms >= self._effective_silence_ms()
                and self._speech_ms >= self.min_speech_ms
            ):
                return self._emit_endpoint()
        else:
            # Idle: keep a rolling pre-roll buffer of the most recent audio.
            self._preroll.append((pcm, frame_ms))
            self._preroll_ms += frame_ms
            while self._preroll_ms > self.preroll_ms and len(self._preroll) > 1:
                _, dropped = self._preroll.popleft()
                self._preroll_ms -= dropped
        return None

    def _effective_silence_ms(self) -> float:
        """Trailing silence required to end the current turn.

        Interpolates linearly from ``silence_ms`` (short utterance) down to
        ``min_silence_ms`` as ``speech_ms`` grows to ``adaptive_full_ms``.
        """
        if self.min_silence_ms >= self.silence_ms or self.adaptive_full_ms <= 0:
            return self.silence_ms
        ratio = min(1.0, self._speech_ms / self.adaptive_full_ms)
        return self.silence_ms - (self.silence_ms - self.min_silence_ms) * ratio

    def flush(self) -> bytes | None:
        """End-of-stream: return any buffered utterance that meets the minimum."""
        audio = bytes(self._collected) if self._speech_ms >= self.min_speech_ms else None
        self.reset()
        return audio

    def _emit_endpoint(self) -> dict:
        audio = bytes(self._collected)
        speech_ms = self._speech_ms
        self.reset()
        return {"event": "endpoint", "audio": audio, "speech_ms": speech_ms}
