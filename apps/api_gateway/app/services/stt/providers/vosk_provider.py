import asyncio
import io
import json
import os
import threading
import wave
from concurrent.futures import ThreadPoolExecutor

from app.schemas.stt import STTResult
from app.services.model_registry.resolve import resolve_stt_engine_config
from app.services.stt.base import STTProvider, STTStream

_MODEL_CACHE: dict[str, object] = {}
# Decode now runs on worker threads (to_thread / the stream executor), so the
# cold-cache check-then-insert below can race: without the lock two concurrent
# first calls would BOTH run the multi-second, multi-hundred-MB Model() load
# (transient double RAM -- OOM risk on the RPi target). Same double-checked
# pattern as whisper_provider._MODEL_LOCK.
_MODEL_LOCK = threading.Lock()

# Per-frame stream decodes (16-50 dispatches/s, single-digit ms each, latency-
# sensitive) get their own single thread instead of the shared to_thread pool,
# where they would queue behind 100-300ms PBKDF2 hashes and whole-utterance
# decodes whenever the pool is busy. One thread is enough: per-stream calls
# are serialized by the caller's await, and Kaldi decode of a 20-60ms frame
# is far faster than real time even with several concurrent sessions.
_STREAM_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vosk-stream")

# Runtime-selected active Vosk model path; falls back to the Model Registry
# engine-config sentinel row when unset. Reset on restart (not persisted).
_active_path: str | None = None


def get_active_vosk_path() -> str:
    return _active_path or resolve_stt_engine_config("vosk")["model_path"]


def set_active_vosk_path(path: str) -> None:
    global _active_path
    _active_path = path


def _load_vosk_model():
    try:
        from vosk import Model
    except ImportError as exc:
        raise RuntimeError("vosk is not installed. Run scripts/setup_local_stt.sh") from exc

    model_path = get_active_vosk_path()
    if not os.path.isdir(model_path):
        raise RuntimeError(
            f"Vosk model not found at {model_path}. Run scripts/download_vosk_model.sh"
        )

    if model_path not in _MODEL_CACHE:
        with _MODEL_LOCK:
            if model_path not in _MODEL_CACHE:  # lost the race to another thread
                _MODEL_CACHE[model_path] = Model(model_path)
    return _MODEL_CACHE[model_path]


class VoskStream(STTStream):
    """Native incremental decoding: emits partials, then finals per utterance.

    The recognizer (and with it the multi-second model load on a cold cache)
    is built lazily on the first accept(), which runs on the stream executor:
    __init__ is called synchronously on the event loop by the WS handler
    (routes/stt.py open_stream), and vosk has no warm(), so an eager load
    here would freeze every live session on the first stream after boot."""

    def __init__(self, engine_name: str, sample_rate: int) -> None:
        self._engine_name = engine_name
        self._sample_rate = sample_rate
        self._recognizer = None

    def _ensure_recognizer(self):
        if self._recognizer is None:
            from vosk import KaldiRecognizer

            self._recognizer = KaldiRecognizer(_load_vosk_model(), self._sample_rate)
        return self._recognizer

    async def accept(self, pcm: bytes) -> list[STTResult]:
        # Kaldi decode is CPU work -- off the loop, or every other WS session
        # stalls for the duration of each chunk's decode. Calls are serialized
        # by the caller's `await`, so the recognizer is never used from two
        # threads at once.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_STREAM_EXECUTOR, self._accept_sync, pcm)

    def _accept_sync(self, pcm: bytes) -> list[STTResult]:
        recognizer = self._ensure_recognizer()
        if recognizer.AcceptWaveform(pcm):
            text = json.loads(recognizer.Result()).get("text", "").strip()
            if text:
                return [STTResult(engine=self._engine_name, text=text, is_final=True)]
            return []
        partial = json.loads(recognizer.PartialResult()).get("partial", "").strip()
        if partial:
            return [STTResult(engine=self._engine_name, text=partial, is_final=False)]
        return []

    async def finalize(self) -> STTResult | None:
        if self._recognizer is None:
            return None  # no audio ever accepted -- nothing to finalize
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_STREAM_EXECUTOR, self._finalize_sync)

    def _finalize_sync(self) -> STTResult | None:
        text = json.loads(self._recognizer.FinalResult()).get("text", "").strip()
        if text:
            return STTResult(engine=self._engine_name, text=text, is_final=True)
        return None


class VoskProvider(STTProvider):
    name = "vosk"

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        # Whole-utterance Kaldi decode (plus a multi-second model load on the
        # first call) is CPU-bound -- run it off the loop like the other local
        # providers (whisper/whisper_mlx/qwen3) already do.
        return await asyncio.to_thread(self._transcribe_sync, audio_bytes)

    def _transcribe_sync(self, audio_bytes: bytes) -> STTResult:
        try:
            from vosk import KaldiRecognizer
        except ImportError as exc:
            raise RuntimeError("vosk is not installed. Run scripts/setup_local_stt.sh") from exc

        model = _load_vosk_model()

        try:
            wav_file = wave.open(io.BytesIO(audio_bytes), "rb")
        except wave.Error as exc:
            raise RuntimeError("Vosk requires a valid WAV PCM16 mono file.") from exc

        with wav_file:
            if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                raise RuntimeError("Vosk requires WAV PCM16 mono audio.")

            recognizer = KaldiRecognizer(model, wav_file.getframerate())
            while True:
                chunk = wav_file.readframes(4000)
                if not chunk:
                    break
                recognizer.AcceptWaveform(chunk)

            text = json.loads(recognizer.FinalResult()).get("text", "").strip()

        return STTResult(engine=self.name, text=text, is_final=True, confidence=None)

    def open_stream(
        self, sample_rate: int, language: str | None = None, model: str | None = None
    ) -> STTStream:
        return VoskStream(self.name, sample_rate)
