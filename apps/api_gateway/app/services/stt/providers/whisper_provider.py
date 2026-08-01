import asyncio
import threading

from app.core.audio import wav_tempfile
from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.model_registry.resolve import (
    resolve_stt_engine_config,
    resolve_stt_local_device,
)
from app.services.stt.base import STTProvider
from app.services.stt.glossary import resolve_initial_prompt

_MODEL_CACHE: dict[str, object] = {}
# Guards the check-then-set on _MODEL_CACHE: the background warm() task and the
# first real turn's transcribe can race on different threads (asyncio.to_thread
# uses a pool), each building the same model concurrently and doubling first-turn
# latency. A lock makes the build single-flight.
_MODEL_LOCK = threading.Lock()

# Runtime-selected whisper model size; falls back to the Model Registry
# engine-config sentinel row when unset. Reset on restart (not persisted).
_active_model: str | None = None


def resolve_whisper_model(model: str) -> str:
    """Map a model id to something faster-whisper accepts.

    Every WHISPER_SIZES entry ("medium", "large-v3", ...) passes through unchanged
    -- faster-whisper resolves it to Systran/faster-whisper-{model} on the hub
    itself. Kept as a seam (rather than calling faster_whisper.WhisperModel with
    `model` directly) so tests can monkeypatch it and a future non-Systran model
    source only needs to change this one function.
    """
    return model


def get_active_whisper_model() -> str:
    return _active_model or resolve_stt_engine_config("whisper_local")["default_model"]


def set_active_whisper_model(model: str) -> None:
    global _active_model
    _active_model = model


class WhisperProvider(STTProvider):
    name = "whisper_local"

    def _cache_key(self, model: str) -> str:
        device_cfg = resolve_stt_local_device("whisper_local")
        # Normalize with the same "cpu" fallback _load_model applies below --
        # otherwise an unset device ("") and an explicitly-configured "cpu"
        # (functionally identical) would cache under two different keys.
        return ":".join([model, device_cfg["device"] or "cpu", device_cfg["compute_type"]])

    def _load_model(self, model: str | None = None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run scripts/setup_local_stt.sh"
            ) from exc

        model_name = model or get_active_whisper_model()
        key = self._cache_key(model_name)
        if key not in _MODEL_CACHE:
            with _MODEL_LOCK:
                if key not in _MODEL_CACHE:
                    device_cfg = resolve_stt_local_device("whisper_local")
                    _MODEL_CACHE[key] = WhisperModel(
                        resolve_whisper_model(model_name),
                        device=device_cfg["device"] or "cpu",
                        compute_type=device_cfg["compute_type"],
                    )
        return _MODEL_CACHE[key]

    def warm(self) -> None:
        """Load the model into memory so the first conversation turn isn't slow."""
        self._load_model()

    def _do_transcribe(self, audio_bytes: bytes, language: str | None, model: str | None) -> str:
        whisper_model = self._load_model(model)
        with wav_tempfile(audio_bytes) as temp_file_path:
            engine_cfg = resolve_stt_engine_config("whisper_local")
            segments, _ = whisper_model.transcribe(
                temp_file_path,
                language=language,
                vad_filter=engine_cfg["vad_filter"],
                beam_size=engine_cfg["beam_size"],
                condition_on_previous_text=engine_cfg["condition_on_previous_text"],
                initial_prompt=resolve_initial_prompt(
                    engine_cfg["initial_prompt"],
                    settings.stt_glossary_path,
                ),
            )
            return " ".join(s.text.strip() for s in segments if s.text.strip())

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        text = await asyncio.to_thread(self._do_transcribe, audio_bytes, language, model)
        return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
