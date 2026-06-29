"""SenseVoice (FunAudioLLM) STT via sherpa-onnx — the lightweight, torch-free path.

SenseVoice-Small is a fast non-autoregressive multilingual ASR model
(Mandarin / Cantonese / English / Japanese / Korean). This runs the quantized int8
ONNX export through sherpa-onnx (onnxruntime, CPU) — no PyTorch, ~250 MB model — so
it is light enough for the slim deploy image (unlike the funasr/torch path).

NOT a Vietnamese model — use whisper/PhoWhisper for Vietnamese. The engine auto-hides
when `sherpa-onnx` (the optional `sensevoice` extra) is absent. The ONNX model
(model.int8.onnx + tokens.txt) downloads from the hub on first use.
"""

import asyncio
import os
import tempfile

import numpy as np

from app.core.audio import wav_file_to_pcm16
from app.core.deps import module_available
from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider

_SAMPLE_RATE = 16000
_recognizer = None


class SenseVoiceProvider(STTProvider):
    name = "sensevoice"

    def available(self) -> bool:
        return module_available("sherpa_onnx")

    def detail(self) -> str:
        return "SenseVoice-Small · sherpa-onnx int8 · zh/yue/en/ja/ko · downloads on first use"

    def _model_files(self) -> tuple[str, str]:
        """Return (model.int8.onnx, tokens.txt) paths, downloading them once."""
        from huggingface_hub import hf_hub_download

        repo = settings.sensevoice_onnx_repo
        model = hf_hub_download(repo, "model.int8.onnx")
        tokens = hf_hub_download(repo, "tokens.txt")
        return model, tokens

    def _load(self):
        global _recognizer
        if _recognizer is None:
            import sherpa_onnx

            model, tokens = self._model_files()
            _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model,
                tokens=tokens,
                num_threads=settings.sensevoice_num_threads,
                use_itn=settings.sensevoice_use_itn,
                language="auto",
                provider="cpu",
            )
        return _recognizer

    def _transcribe(self, wav_path: str) -> str:
        rec = self._load()
        pcm = wav_file_to_pcm16(wav_path, _SAMPLE_RATE)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        stream = rec.create_stream()
        stream.accept_waveform(_SAMPLE_RATE, samples)
        rec.decode_stream(stream)
        return (stream.result.text or "").strip()

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
            text = await asyncio.to_thread(self._transcribe, tmp)
            return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)
