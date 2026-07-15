import asyncio
import os
import tempfile
import threading
from pathlib import Path

from app.schemas.stt import STTResult
from app.services.model_registry.resolve import resolve_stt_local_device
from app.services.stt.base import STTProvider
from app.services.stt.glossary import resolve_initial_prompt
from app.services.system_config import system_config_store

_MODEL_CACHE: dict[str, object] = {}
# Guards the check-then-set on _MODEL_CACHE: the background warm() task and the
# first real turn's transcribe can race on different threads (asyncio.to_thread
# uses a pool), each building the same model concurrently and doubling first-turn
# latency. A lock makes the build single-flight.
_MODEL_LOCK = threading.Lock()

# Runtime-selected whisper model size; falls back to system_config_store when unset.
# Reset on restart (not persisted).
_active_model: str | None = None

# PhoWhisper (VinAI) Vietnamese fine-tune, pre-converted to CTranslate2 so it loads
# directly in faster-whisper. One repo holds every size in its own subfolder.
PHOWHISPER_REPO = "quocphu/PhoWhisper-ct2-FasterWhisper"
PHOWHISPER_SUBFOLDERS = {
    "phowhisper-tiny": "PhoWhisper-tiny-ct2-fasterWhisper",
    "phowhisper-base": "PhoWhisper-base-ct2-fasterWhisper",
    "phowhisper-small": "PhoWhisper-small-ct2-fasterWhisper",
    "phowhisper-medium": "PhoWhisper-medium-ct2-fasterWhisper",
    "phowhisper-large": "PhoWhisper-large-ct2-fasterWhisper",
}


def is_phowhisper(model: str) -> bool:
    return model in PHOWHISPER_SUBFOLDERS


def resolve_whisper_model(model: str) -> str:
    """Map a model id to something faster-whisper accepts.

    Standard sizes ("medium", "large-v3") pass through. PhoWhisper ids download the
    matching subfolder from the hub (cached after the first call) and return its path.
    """
    sub = PHOWHISPER_SUBFOLDERS.get(model)
    if not sub:
        return model
    from huggingface_hub import snapshot_download

    root = snapshot_download(PHOWHISPER_REPO, allow_patterns=[f"{sub}/*"])
    return str(Path(root) / sub)


def get_active_whisper_model() -> str:
    return _active_model or system_config_store.get().stt_local.whisper_local_model


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
        temp_file_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                temp_file_path = f.name
            stt_local = system_config_store.get().stt_local
            segments, _ = whisper_model.transcribe(
                temp_file_path,
                language=language,
                vad_filter=stt_local.whisper_vad_filter,
                beam_size=stt_local.whisper_beam_size,
                condition_on_previous_text=stt_local.whisper_condition_on_previous_text,
                initial_prompt=resolve_initial_prompt(
                    stt_local.whisper_initial_prompt, stt_local.stt_glossary_path
                ),
            )
            return " ".join(s.text.strip() for s in segments if s.text.strip())
        finally:
            if temp_file_path and os.path.isfile(temp_file_path):
                os.unlink(temp_file_path)

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        text = await asyncio.to_thread(self._do_transcribe, audio_bytes, language, model)
        return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
