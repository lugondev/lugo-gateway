import asyncio
import logging
import os
import tempfile

from app.core.audio import silent_wav_bytes, wav_duration_seconds
from app.core.settings import settings
from app.schemas.tts import TTSRequest, TTSResult
from app.services.artifacts import artifact_store
from app.services.tts.base import TTSProvider

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 24000  # OmniVoice audio tokenizer rate.

# Runtime-selected model repo id; falls back to settings. Reset on restart.
_active_model: str | None = None


def get_active_omnivoice_model() -> str:
    return _active_model or settings.omnivoice_model_id


def set_active_omnivoice_model(model_id: str) -> None:
    global _active_model
    _active_model = model_id


class OmniVoiceProvider(TTSProvider):
    name = "omnivoice"

    def available(self) -> bool:
        return os.path.isfile(settings.omnivoice_python_path)

    def detail(self) -> str:
        return get_active_omnivoice_model()

    def _build_cmd(self, payload: TTSRequest, output_path: str) -> list[str]:
        cmd = [
            settings.omnivoice_python_path,
            "-m",
            "omnivoice.cli.infer",
            "--model",
            get_active_omnivoice_model(),
            "--text",
            payload.text,
            "--output",
            output_path,
        ]
        if settings.omnivoice_device:
            cmd += ["--device", settings.omnivoice_device]
        if payload.language:
            cmd += ["--language", payload.language]
        if payload.ref_audio_path:
            cmd += ["--ref_audio", payload.ref_audio_path]
        if payload.ref_text:
            cmd += ["--ref_text", payload.ref_text]
        if payload.instruct:
            cmd += ["--instruct", payload.instruct]
        if payload.speed:
            cmd += ["--speed", str(payload.speed)]
        return cmd

    async def _generate_wav(self, payload: TTSRequest) -> bytes:
        """Run OmniVoice inference in its own venv via the CLI; return WAV bytes."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            output_path = tmp.name
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_cmd(payload, output_path),
                cwd=settings.omnivoice_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=settings.omnivoice_timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                proc.kill()
                raise RuntimeError("OmniVoice inference timed out") from exc

            if proc.returncode != 0:
                raise RuntimeError(
                    f"OmniVoice CLI failed ({proc.returncode}): {stderr.decode()[-400:]}"
                )
            with open(output_path, "rb") as fh:
                return fh.read()
        finally:
            if os.path.isfile(output_path):
                os.unlink(output_path)

    def _mock_wav(self, payload: TTSRequest) -> bytes:
        word_count = max(1, len(payload.text.split()))
        return silent_wav_bytes(word_count / 2.5, sample_rate=_SAMPLE_RATE)

    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        mock = settings.enable_mock_engines
        if not mock:
            try:
                wav = await self._generate_wav(payload)
            except Exception as exc:  # noqa: BLE001 - degrade gracefully, log cause
                logger.warning("OmniVoice unavailable, using mock audio: %s", exc)
                mock = True
                wav = self._mock_wav(payload)
        else:
            wav = self._mock_wav(payload)

        _, audio_url = artifact_store.save_wav(wav)
        return TTSResult(
            engine=self.name,
            sample_rate=_SAMPLE_RATE,
            audio_url=audio_url,
            duration_seconds=round(wav_duration_seconds(wav), 3),
            text=payload.text,
            mock=mock,
        )
