import asyncio
import logging
import os
import tempfile
from pathlib import Path

from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.tts.base import MockFallbackTTSProvider

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 24000  # OmniVoice audio tokenizer rate.

# Runtime-selected model repo id; falls back to settings. Reset on restart.
_active_model: str | None = None

# Process-wide pinned voice reference {"path", "text"} cloned for every chunk.
_voice_ref: dict[str, str] = {}


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

        # Voice consistency: use the caller's instruct, else a fixed default so every
        # chunk shares one voice instead of the model picking a random voice per call.
        instruct = payload.instruct or (
            settings.omnivoice_default_instruct if not payload.ref_audio_path else ""
        )
        if instruct:
            cmd += ["--instruct", instruct]
        # Greedy token sampling -> deterministic voice realization across calls.
        cmd += ["--class_temperature", str(settings.omnivoice_class_temperature)]

        if payload.speed:
            cmd += ["--speed", str(payload.speed)]
        return cmd

    async def _exec(self, cmd: list[str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
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

    async def _ensure_voice_ref(self) -> dict[str, str]:
        """Generate a fixed reference voice once; reused (cloned) for every chunk."""
        if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
            return _voice_ref
        # Absolute path: the CLI runs with cwd=OMNIVOICE_PATH, so relatives break.
        ref_dir = Path(settings.artifacts_dir).resolve()
        ref_dir.mkdir(parents=True, exist_ok=True)
        ref_path = str(ref_dir / "_omnivoice_voice_ref.wav")
        ref_req = TTSRequest(
            text=settings.omnivoice_ref_text,
            engine=self.name,
            instruct=settings.omnivoice_default_instruct,
        )
        await self._exec(self._build_cmd(ref_req, ref_path))
        _voice_ref.update({"path": ref_path, "text": settings.omnivoice_ref_text})
        return _voice_ref

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        """Run OmniVoice inference in its own venv via the CLI; return WAV bytes."""
        effective = payload
        # Pin one voice: clone a fixed reference unless the caller specified a voice.
        if settings.omnivoice_pin_voice and not payload.ref_audio_path and not payload.instruct:
            ref = await self._ensure_voice_ref()
            effective = payload.model_copy(
                update={"ref_audio_path": ref["path"], "ref_text": ref["text"]}
            )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            output_path = tmp.name
        try:
            await self._exec(self._build_cmd(effective, output_path))
            with open(output_path, "rb") as fh:
                return fh.read()
        finally:
            if os.path.isfile(output_path):
                os.unlink(output_path)
