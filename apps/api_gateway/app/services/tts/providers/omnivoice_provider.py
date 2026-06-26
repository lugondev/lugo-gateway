import asyncio
import logging
import os
import tempfile

from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.tts.base import MockFallbackTTSProvider

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 24000  # OmniVoice audio tokenizer rate.

# Runtime-selected model repo id; falls back to settings. Reset on restart.
_active_model: str | None = None


def get_active_omnivoice_model() -> str:
    return _active_model or settings.omnivoice_model_id


def set_active_omnivoice_model(model_id: str) -> None:
    global _active_model
    _active_model = model_id


class OmniVoiceProvider(MockFallbackTTSProvider):
    name = "omnivoice"
    sample_rate = _SAMPLE_RATE

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

    async def _render_wav(self, payload: TTSRequest) -> bytes:
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
