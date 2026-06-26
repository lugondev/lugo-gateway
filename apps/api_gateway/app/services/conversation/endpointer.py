"""VAD-based turn endpointing for voice conversation.

Ingests raw PCM16 mono frames and detects end-of-turn from trailing silence,
so the server knows when the user has finished speaking (turn-taking). Energy
(RMS) based — cheap, streaming-friendly, no heavy model per frame.
"""

import numpy as np

from app.core.audio import pcm16_to_float_array


class VadEndpointer:
    def __init__(
        self,
        sample_rate: int,
        silence_ms: int = 700,
        min_speech_ms: int = 300,
        rms_threshold: float = 0.015,
        max_utterance_ms: int = 30000,
        noise_factor: float = 2.5,
    ) -> None:
        self.sample_rate = sample_rate
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.rms_threshold = rms_threshold  # absolute floor for "speech"
        self.max_utterance_ms = max_utterance_ms
        self.noise_factor = noise_factor
        self.reset()

    def reset(self) -> None:
        self._collected = bytearray()
        self._speaking = False
        self._silence_acc_ms = 0.0
        self._speech_ms = 0.0
        self._noise = self.rms_threshold  # adaptive ambient-noise floor

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
            if self._silence_acc_ms >= self.silence_ms and self._speech_ms >= self.min_speech_ms:
                return self._emit_endpoint()
        return None

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
