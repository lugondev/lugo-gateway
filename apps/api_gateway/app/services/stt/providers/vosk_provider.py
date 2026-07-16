import io
import json
import os
import wave

from app.schemas.stt import STTResult
from app.services.model_registry.resolve import resolve_stt_engine_config
from app.services.stt.base import STTProvider, STTStream

_MODEL_CACHE: dict[str, object] = {}

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
        _MODEL_CACHE[model_path] = Model(model_path)
    return _MODEL_CACHE[model_path]


class VoskStream(STTStream):
    """Native incremental decoding: emits partials, then finals per utterance."""

    def __init__(self, engine_name: str, sample_rate: int) -> None:
        from vosk import KaldiRecognizer

        self._engine_name = engine_name
        self._recognizer = KaldiRecognizer(_load_vosk_model(), sample_rate)

    async def accept(self, pcm: bytes) -> list[STTResult]:
        if self._recognizer.AcceptWaveform(pcm):
            text = json.loads(self._recognizer.Result()).get("text", "").strip()
            if text:
                return [STTResult(engine=self._engine_name, text=text, is_final=True)]
            return []
        partial = json.loads(self._recognizer.PartialResult()).get("partial", "").strip()
        if partial:
            return [STTResult(engine=self._engine_name, text=partial, is_final=False)]
        return []

    async def finalize(self) -> STTResult | None:
        text = json.loads(self._recognizer.FinalResult()).get("text", "").strip()
        if text:
            return STTResult(engine=self._engine_name, text=text, is_final=True)
        return None


class VoskProvider(STTProvider):
    name = "vosk"

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
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

    def open_stream(self, sample_rate: int, language: str | None = None) -> STTStream:
        return VoskStream(self.name, sample_rate)
