"""Opus decoding for the audio transport.

Embedded clients (ESP32 / Raspberry Pi) and browsers (WebCodecs) can stream Opus
instead of raw PCM16 — ~10x less bandwidth over the network, and the native format
for the xiaozhi-style firmware (Opus, 16 kHz mono, 20-60 ms frames). The server
decodes each Opus packet back to PCM16 for the endpointer / STT.

Requires libopus (system lib) + opuslib (pip). On Linux: apt-get install libopus0.
On macOS/Homebrew the lib lives in /opt/homebrew/lib — the run env must expose it
via DYLD_FALLBACK_LIBRARY_PATH (handled in the Makefile). If unavailable, Opus is
disabled and clients must send PCM16.
"""

import logging

logger = logging.getLogger(__name__)

_decoder_cls = None  # None = untried, False = unavailable, else opuslib.Decoder


def _load():
    global _decoder_cls
    if _decoder_cls is not None:
        return _decoder_cls or None
    try:
        import opuslib

        _decoder_cls = opuslib.Decoder
    except Exception as exc:  # noqa: BLE001 - any load failure disables Opus
        logger.warning(
            "Opus transport disabled (opuslib/libopus unavailable: %s). "
            "Clients must send PCM16.",
            exc,
        )
        _decoder_cls = False
    return _decoder_cls or None


def opus_available() -> bool:
    return _load() is not None


class OpusFrameEncoder:
    """Encode PCM16 mono to a list of Opus packets (for pushing to devices)."""

    def __init__(self, sample_rate: int = 24000, channels: int = 1, frame_ms: int = 60) -> None:
        cls = _load_encoder()
        if not cls:
            raise RuntimeError("opuslib/libopus not available")
        import opuslib

        self._enc = cls(sample_rate, channels, opuslib.APPLICATION_VOIP)
        self.sample_rate = sample_rate
        self.frame = sample_rate * frame_ms // 1000  # samples per frame

    def encode_pcm16(self, pcm16: bytes) -> list[bytes]:
        import numpy as np

        arr = np.frombuffer(pcm16, dtype="<i2")
        packets: list[bytes] = []
        for i in range(0, len(arr), self.frame):
            chunk = arr[i : i + self.frame]
            if len(chunk) < self.frame:  # pad the last frame to a full Opus frame
                chunk = np.concatenate([chunk, np.zeros(self.frame - len(chunk), dtype="<i2")])
            packets.append(self._enc.encode(chunk.tobytes(), self.frame))
        return packets


def _load_encoder():
    if _load() is None:
        return None
    import opuslib

    return opuslib.Encoder


class OpusFrameDecoder:
    """Stateful per-connection Opus decoder (mono). Feed it one packet at a time."""

    def __init__(self, sample_rate: int = 16000, channels: int = 1) -> None:
        cls = _load()
        if not cls:
            raise RuntimeError("opuslib/libopus not available")
        self._dec = cls(sample_rate, channels)
        # Output buffer sized for the largest Opus frame (120 ms); decode() returns
        # only the actual samples, so over-sizing is safe.
        self._frame_size = sample_rate * 120 // 1000

    def decode(self, packet: bytes) -> bytes:
        """Decode one Opus packet to PCM16 little-endian bytes."""
        return self._dec.decode(packet, self._frame_size)
