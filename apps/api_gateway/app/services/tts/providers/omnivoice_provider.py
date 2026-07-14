import asyncio
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx

from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.system_config import system_config_store
from app.services.tts.base import RenderingTTSProvider

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 24000  # OmniVoice audio tokenizer rate.

# Runtime-selected model repo id; falls back to settings. Reset on restart.
_active_model: str | None = None

# Process-wide pinned voice reference {"path", "text"} cloned for every chunk.
_voice_ref: dict[str, str] = {}
# Guards the check-then-build in _ensure_voice_ref: concurrent cold-start calls
# (e.g. two sessions' first turn landing at the same moment) would otherwise each
# synthesize the reference independently — wasted work, not incorrect output,
# since both would build the same thing from the same global settings.
_voice_ref_lock = asyncio.Lock()

# Tracks the currently-running sidecar Popen handle so a settings change can kill
# it before spawning a replacement. None until the first _spawn_sidecar() call.
_sidecar_process: subprocess.Popen | None = None
# Guards the kill-then-spawn critical section in _spawn_sidecar: warm() runs on a
# background thread-pool thread (asyncio.to_thread) for every new conversation
# session, so two sessions starting close together -- or a warm()-triggered
# respawn racing an admin's PUT /v1/system/config -> reset_voice_ref_and_respawn()
# on the event-loop thread -- can call _spawn_sidecar() concurrently. Without a
# lock, each thread's check-then-kill-then-Popen-then-assign sequence can
# interleave: one thread's freshly-spawned process gets silently overwritten in
# the global by the other thread's spawn, leaking the first process (never
# killed) and likely colliding on the same host:port. A plain threading.Lock (not
# asyncio.Lock, since _spawn_sidecar is a sync method called from both the event
# loop and a thread-pool thread) makes kill+spawn atomic -- same pattern as
# whisper_provider.py's _MODEL_LOCK.
_sidecar_lock = threading.Lock()


def get_active_omnivoice_model() -> str:
    return _active_model or system_config_store.get().omnivoice.omnivoice_model_id


def set_active_omnivoice_model(model_id: str) -> None:
    global _active_model
    _active_model = model_id


def _kill_sidecar_if_running() -> None:
    """Kill the tracked sidecar process (if any) and wait for it to exit.

    Only called from inside ``_spawn_sidecar`` while ``_sidecar_lock`` is held --
    see that method for why the kill and the subsequent Popen assignment must be
    one atomic critical section.
    """
    global _sidecar_process
    if _sidecar_process is not None and _sidecar_process.poll() is None:
        _sidecar_process.kill()
        try:
            _sidecar_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def reset_voice_ref_and_respawn() -> None:
    """Clear the pinned-voice cache and kill+respawn the sidecar so an admin edit
    to model_id/dtype/device/host/port/instruct/temperature takes effect without a
    process restart. The sidecar has no reload endpoint (see omnivoice_sidecar.py) —
    a new process with the new CLI args is the only way to pick up the change.
    _spawn_sidecar() itself owns the full kill-old-then-spawn-new sequence
    (under _sidecar_lock), so it's called once here rather than duplicating the
    kill step."""
    _voice_ref.clear()
    provider = OmniVoiceProvider()
    provider._spawn_sidecar()


class OmniVoiceProvider(RenderingTTSProvider):
    name = "omnivoice"
    sample_rate = _SAMPLE_RATE

    def available(self) -> bool:
        return os.path.isfile(system_config_store.get().omnivoice.omnivoice_python_path)

    def detail(self) -> str:
        return get_active_omnivoice_model()

    # ---------------------------------------------------------------- synth
    async def _synth(
        self, text: str, *, instruct=None, ref_audio=None, ref_text=None, speed=None
    ) -> bytes:
        if system_config_store.get().omnivoice.omnivoice_use_server:
            return await self._server_synth(text, instruct, ref_audio, ref_text, speed)
        return await self._cli_synth(text, instruct, ref_audio, ref_text, speed)

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        logger.info("DEBUG_HANG _render_wav: text_len=%d", len(payload.text or ""))
        omnivoice = system_config_store.get().omnivoice
        instruct, ref_audio, ref_text = payload.instruct, payload.ref_audio_path, payload.ref_text
        if omnivoice.omnivoice_pin_voice and not ref_audio and not instruct:
            # Clone a fixed reference so every chunk shares exactly one voice.
            ref = await self._ensure_voice_ref()
            ref_audio, ref_text = ref["path"], ref["text"]
        elif not ref_audio and not instruct:
            instruct = omnivoice.omnivoice_default_instruct
        return await self._synth(
            payload.text, instruct=instruct, ref_audio=ref_audio, ref_text=ref_text, speed=payload.speed
        )

    async def _ensure_voice_ref(self) -> dict[str, str]:
        """Generate a fixed reference voice once; reused (cloned) for every chunk."""
        if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
            return _voice_ref
        async with _voice_ref_lock:
            if _voice_ref.get("path") and os.path.isfile(_voice_ref["path"]):
                return _voice_ref
            omnivoice = system_config_store.get().omnivoice
            ref_dir = Path(settings.artifacts_dir).resolve()
            ref_dir.mkdir(parents=True, exist_ok=True)
            ref_path = str(ref_dir / "_omnivoice_voice_ref.wav")
            wav = await self._synth(
                omnivoice.omnivoice_ref_text, instruct=omnivoice.omnivoice_default_instruct
            )
            Path(ref_path).write_bytes(wav)
            _voice_ref.update({"path": ref_path, "text": omnivoice.omnivoice_ref_text})
            return _voice_ref

    # ---------------------------------------------------------------- server mode
    def _server_base(self) -> str:
        omnivoice = system_config_store.get().omnivoice
        return f"http://{omnivoice.omnivoice_server_host}:{omnivoice.omnivoice_server_port}"

    async def _server_up(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                return (await client.get(f"{self._server_base()}/health")).status_code == 200
        except httpx.HTTPError:
            return False

    def _spawn_sidecar(self) -> None:
        global _sidecar_process
        # Kill-old-then-spawn-new must be one atomic critical section -- see
        # _sidecar_lock's definition for the race this guards against.
        with _sidecar_lock:
            _kill_sidecar_if_running()
            omnivoice = system_config_store.get().omnivoice
            # sidecar lives in services/tts/ (one level up from providers/)
            sidecar = Path(__file__).resolve().parent.parent / "omnivoice_sidecar.py"
            cmd = [
                omnivoice.omnivoice_python_path, str(sidecar),
                "--host", omnivoice.omnivoice_server_host,
                "--port", str(omnivoice.omnivoice_server_port),
                "--model", get_active_omnivoice_model(),
                "--dtype", omnivoice.omnivoice_dtype,
            ]
            if omnivoice.omnivoice_device:
                cmd += ["--device", omnivoice.omnivoice_device]
            logger.info("Starting OmniVoice sidecar server on port %s", omnivoice.omnivoice_server_port)
            # Clean env: our app sets PYTHONPATH/VIRTUAL_ENV, which would leak into the
            # OmniVoice venv interpreter and can break its imports.
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
            log_fh = open(  # noqa: SIM115 - kept open for the child's lifetime
                Path(settings.artifacts_dir).resolve() / "_omnivoice_sidecar.log", "ab"
            )
            _sidecar_process = subprocess.Popen(  # noqa: S603 - local model server
                cmd, cwd=omnivoice.omnivoice_path, env=env,
                stdout=log_fh, stderr=log_fh, start_new_session=True,
            )

    def warm(self) -> None:
        """Best-effort: start the sidecar early (fire-and-forget) so it loads while
        the user speaks, instead of paying the cold start on the first reply."""
        if not system_config_store.get().omnivoice.omnivoice_use_server:
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
        deadline = system_config_store.get().omnivoice.omnivoice_server_startup_seconds
        waited = 0.0
        while waited < deadline:
            await asyncio.sleep(1.0)
            waited += 1.0
            if await self._server_up():
                return
        raise RuntimeError("OmniVoice server did not become ready in time")

    async def _server_synth(self, text, instruct, ref_audio, ref_text, speed) -> bytes:
        await self._ensure_server()
        omnivoice = system_config_store.get().omnivoice
        body = {
            "text": text,
            "language": None,
            "instruct": instruct,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "speed": speed,
            "class_temperature": omnivoice.omnivoice_class_temperature,
        }
        logger.info("DEBUG_HANG _server_synth: posting /synth, text_len=%d", len(text))
        async with httpx.AsyncClient(timeout=omnivoice.omnivoice_timeout_seconds) as client:
            try:
                resp = await client.post(f"{self._server_base()}/synth", json=body)
            except asyncio.CancelledError:
                # e.g. barge-in aborted this turn mid-synth. The connection closes
                # when this `async with` block exits, but nothing guarantees the
                # sidecar notices and aborts its in-progress inference -- logged
                # so a stall traced back here isn't mistaken for a silent hang.
                logger.warning("OmniVoice synth cancelled mid-request (turn aborted), text_len=%d", len(text))
                raise
            logger.info("DEBUG_HANG _server_synth: got response, status=%s", resp.status_code)
            if resp.status_code != 200:
                raise RuntimeError(f"OmniVoice server error {resp.status_code}: {resp.text[:200]}")
            return resp.content

    # ---------------------------------------------------------------- CLI mode (fallback)
    def _build_cmd(self, text, instruct, ref_audio, ref_text, speed, output_path) -> list[str]:
        omnivoice = system_config_store.get().omnivoice
        cmd = [
            omnivoice.omnivoice_python_path, "-m", "omnivoice.cli.infer",
            "--model", get_active_omnivoice_model(), "--text", text, "--output", output_path,
            "--class_temperature", str(omnivoice.omnivoice_class_temperature),
        ]
        if omnivoice.omnivoice_device:
            cmd += ["--device", omnivoice.omnivoice_device]
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
        omnivoice = system_config_store.get().omnivoice
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            output_path = tmp.name
        try:
            cmd = self._build_cmd(text, instruct, ref_audio, ref_text, speed, output_path)
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=omnivoice.omnivoice_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=omnivoice.omnivoice_timeout_seconds
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
