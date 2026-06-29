"""SenseVoice (FunAudioLLM) STT via the funasr framework.

SenseVoice-Small is a fast, non-autoregressive multilingual ASR model
(Mandarin / Cantonese / English / Japanese / Korean) with emotion + audio-event
tags. It is NOT a Vietnamese model — use whisper/PhoWhisper for Vietnamese; this
engine is for the languages above (or fast multilingual fallback).

Available only when `funasr` is installed (the optional `sensevoice` extra, which
pulls PyTorch). The SenseVoiceSmall weights download from the hub on first use, so
the engine auto-hides when funasr is absent (e.g. the slim deploy image).
"""

import asyncio
import os
import tempfile

from app.core.deps import module_available
from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider

_MODEL_CACHE: dict[str, object] = {}

# Languages SenseVoice actually supports; anything else (incl. Vietnamese) → auto.
_SUPPORTED = {"zh", "yue", "en", "ja", "ko", "auto", "nospeech"}


class SenseVoiceProvider(STTProvider):
    name = "sensevoice"

    def available(self) -> bool:
        return module_available("funasr")

    def detail(self) -> str:
        model = settings.sensevoice_model.split("/")[-1]
        return f"{model} · multilingual zh/yue/en/ja/ko · downloads on first use"

    def _load(self):
        model_id = settings.sensevoice_model
        if model_id not in _MODEL_CACHE:
            from funasr import AutoModel

            _MODEL_CACHE[model_id] = AutoModel(
                model=model_id,
                trust_remote_code=True,
                vad_model="fsmn-vad",
                vad_kwargs={"max_single_segment_time": 30000},
                device=settings.sensevoice_device or "cpu",
                disable_update=True,
            )
        return _MODEL_CACHE[model_id]

    def _transcribe(self, wav_path: str, language: str | None) -> str:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        model = self._load()
        lang = (language or "auto").lower()
        if lang not in _SUPPORTED:  # e.g. "vi" — SenseVoice can't, fall back to auto
            lang = "auto"
        res = model.generate(
            input=wav_path,
            cache={},
            language=lang,
            use_itn=True,
            batch_size_s=60,
            merge_vad=True,
            merge_length_s=15,
        )
        if not res:
            return ""
        return rich_transcription_postprocess(res[0]["text"]).strip()

    def warm(self) -> None:
        try:
            self._load()
        except Exception:  # noqa: BLE001 - best-effort warm
            pass

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None) -> STTResult:
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            text = await asyncio.to_thread(self._transcribe, tmp, language)
            return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)
