import os
import tempfile

from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider

_MODEL_CACHE: dict[str, object] = {}

# Runtime-selected whisper model size; falls back to settings when unset.
# Reset on restart (not persisted).
_active_model: str | None = None


def get_active_whisper_model() -> str:
    return _active_model or settings.whisper_local_model


def set_active_whisper_model(model: str) -> None:
    global _active_model
    _active_model = model


class WhisperProvider(STTProvider):
    name = "whisper_local"

    def _cache_key(self, model: str) -> str:
        return ":".join(
            [model, settings.whisper_local_device, settings.whisper_local_compute_type]
        )

    def _load_model(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run scripts/setup_local_stt.sh"
            ) from exc

        model_name = get_active_whisper_model()
        key = self._cache_key(model_name)
        if key not in _MODEL_CACHE:
            _MODEL_CACHE[key] = WhisperModel(
                model_name,
                device=settings.whisper_local_device,
                compute_type=settings.whisper_local_compute_type,
            )
        return _MODEL_CACHE[key]

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        model = self._load_model()
        temp_file_path = ""

        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_file.write(audio_bytes)
                temp_file_path = temp_file.name

            segments, _ = model.transcribe(
                temp_file_path,
                language=language,
                vad_filter=settings.stt_vad_enabled,
            )

            text_parts = [segment.text.strip() for segment in segments if segment.text.strip()]
            text = " ".join(text_parts)
            return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
        finally:
            if temp_file_path and os.path.isfile(temp_file_path):
                os.unlink(temp_file_path)
