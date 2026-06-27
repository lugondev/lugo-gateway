import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

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

    # ---------------------------------------------------------------- synth
    async def _synth(
        self, text: str, *, instruct=None, ref_audio=None, ref_text=None, speed=None
    ) -> bytes:
        if settings.omnivoice_use_server:
            return await self._server_synth(text, instruct, ref_audio, ref_text, speed)
        return await self._cli_synth(text, instruct, ref_audio, ref_text, speed)

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        instruct, ref_audio, ref_text = payload.instruct, payload.ref_audio_path, payload.ref_text
        if settings.omnivoice_pin_voice and not ref_audio and not instruct:
            # Clone a fixed reference so every chunk shares exactly one voice.
            ref = await self._ensure_voice_ref()
            ref_audio, ref_text = ref["path"], ref["text"]
        elif not ref_audio and not instruct:
            instruct = settings.omnivoice_default_instruct
        return await self._synth(
            payload.text, instruct=instruct, ref_audio=ref_audio, ref_text=ref_text, speed=payload.speed
        )

    async def _ensure_voice_ref(self) -> dict[str, str]:
        """Generate a fixed reference voice once; reused (cloned) for every chunk."""
        if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
            return _voice_ref
        ref_dir = Path(settings.artifacts_dir).resolve()
        ref_dir.mkdir(parents=True, exist_ok=True)
        ref_path = str(ref_dir / "_omnivoice_voice_ref.wav")
        wav = await self._synth(settings.omnivoice_ref_text, instruct=settings.omnivoice_default_instruct)
        Path(ref_path).write_bytes(wav)
        _voice_ref.update({"path": ref_path, "text": settings.omnivoice_ref_text})
        return _voice_ref

    # ---------------------------------------------------------------- server mode
    def _server_base(self) -> str:
        return f"http://{settings.omnivoice_server_host}:{settings.omnivoice_server_port}"

    async def _server_up(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                return (await client.get(f"{self._server_base()}/health")).status_code == 200
        except httpx.HTTPError:
            return False

    def _spawn_sidecar(self) -> None:
        # sidecar lives in services/tts/ (one level up from providers/)
        sidecar = Path(__file__).resolve().parent.parent / "omnivoice_sidecar.py"
        cmd = [
            settings.omnivoice_python_path, str(sidecar),
            "--host", settings.omnivoice_server_host,
            "--port", str(settings.omnivoice_server_port),
            "--model", get_active_omnivoice_model(),
            "--dtype", settings.omnivoice_dtype,
        ]
        if settings.omnivoice_device:
            cmd += ["--device", settings.omnivoice_device]
        logger.info("Starting OmniVoice sidecar server on port %s", settings.omnivoice_server_port)
        # Clean env: our app sets PYTHONPATH/VIRTUAL_ENV, which would leak into the
        # OmniVoice venv interpreter and can break its imports.
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
        log_fh = open(  # noqa: SIM115 - kept open for the child's lifetime
            Path(settings.artifacts_dir).resolve() / "_omnivoice_sidecar.log", "ab"
        )
        subprocess.Popen(  # noqa: S603 - local model server
            cmd, cwd=settings.omnivoice_path, env=env,
            stdout=log_fh, stderr=log_fh, start_new_session=True,
        )

    def warm(self) -> None:
        """Best-effort: start the sidecar early (fire-and-forget) so it loads while
        the user speaks, instead of paying the cold start on the first reply."""
        if not settings.omnivoice_use_server:
            return
        try:
            if httpx.get(f"{self._server_base()}/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        self._spawn_sidecar()

    async def _ensure_server(self) -> None:
        if await self._server_up():
            return
        self._spawn_sidecar()
        deadline = settings.omnivoice_server_startup_seconds
        waited = 0.0
        while waited < deadline:
            await asyncio.sleep(1.0)
            waited += 1.0
            if await self._server_up():
                return
        raise RuntimeError("OmniVoice server did not become ready in time")

    async def _server_synth(self, text, instruct, ref_audio, ref_text, speed) -> bytes:
        await self._ensure_server()
        body = {
            "text": text,
            "language": None,
            "instruct": instruct,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "speed": speed,
            "class_temperature": settings.omnivoice_class_temperature,
        }
        async with httpx.AsyncClient(timeout=settings.omnivoice_timeout_seconds) as client:
            resp = await client.post(f"{self._server_base()}/synth", json=body)
            if resp.status_code != 200:
                raise RuntimeError(f"OmniVoice server error {resp.status_code}: {resp.text[:200]}")
            return resp.content

    # ---------------------------------------------------------------- CLI mode (fallback)
    def _build_cmd(self, text, instruct, ref_audio, ref_text, speed, output_path) -> list[str]:
        cmd = [
            settings.omnivoice_python_path, "-m", "omnivoice.cli.infer",
            "--model", get_active_omnivoice_model(), "--text", text, "--output", output_path,
            "--class_temperature", str(settings.omnivoice_class_temperature),
        ]
        if settings.omnivoice_device:
            cmd += ["--device", settings.omnivoice_device]
        if ref_audio:
            cmd += ["--ref_audio", ref_audio]
        if ref_text:
            cmd += ["--ref_text", ref_text]
        if instruct:
            cmd += ["--instruct", instruct]
        if speed:
            cmd += ["--speed", str(speed)]
        return cmd

    async def _cli_synth(self, text, instruct, ref_audio, ref_text, speed) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            output_path = tmp.name
        try:
            cmd = self._build_cmd(text, instruct, ref_audio, ref_text, speed, output_path)
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=settings.omnivoice_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=settings.omnivoice_timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                proc.kill()
                raise RuntimeError("OmniVoice inference timed out") from exc
            if proc.returncode != 0:
                raise RuntimeError(f"OmniVoice CLI failed ({proc.returncode}): {stderr.decode()[-400:]}")
            return Path(output_path).read_bytes()
        finally:
            if os.path.isfile(output_path):
                os.unlink(output_path)
