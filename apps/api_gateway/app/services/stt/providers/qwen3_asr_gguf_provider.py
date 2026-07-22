"""Qwen3-ASR via GGUF — runs on CPU (and Apple Metal) through a native C++ binary.

Unlike qwen3_asr_provider.py (Python packages mlx-qwen3-asr / qwen-asr, GPU-only),
this backend shells out to the `qwen3-asr-cli` binary from
https://github.com/predict-woo/qwen3-asr.cpp — a pure-C++17/GGML build with no
Python binding. That is exactly why it exists: it gives Qwen3-ASR a CPU path for
the CPU-only Linux deploy, where both Python backends auto-hide.

It is the reference implementation for a non-Python STT engine in this codebase:
same STTProvider template as every other engine, but transcribe runs a subprocess
instead of importing a module. It is registered both in-process (the gateway calls
the binary directly) and, unchanged, as an apps/model_service engine
(SERVICE_ENGINE=qwen3_asr_gguf) behind the OpenAI-compatible surface — see
docs/model_service_template.md.

Binary + weights are built/fetched by apps/model_service/scripts/build_qwen3_asr_cpp.sh.
The engine auto-hides when the binary or the GGUF model file is absent.
"""

import asyncio
import concurrent.futures
import logging
import os
import shutil
import subprocess
import tempfile
import wave

from app.core.audio import pcm16_to_wav_bytes, wav_bytes_to_pcm16
from app.schemas.stt import STTResult
from app.services.model_registry.resolve import resolve_stt_engine_config
from app.services.stt.base import STTProvider

logger = logging.getLogger(__name__)

# qwen3-asr-cli REQUIRES 16 kHz mono PCM16 WAV. It reads the header but does NOT
# resample: feeding 24k/48k (browser/device capture) produces empty or garbage
# output that drifts into Chinese. Normalize every clip to this before the binary
# ever sees it.
_TARGET_SR = 16000

# Default install location the build script targets, checked last after explicit
# config and a PATH lookup (mirrors _ollama_bin's candidate list in llm_models.py).
_DEFAULT_BUILD_BIN = os.path.join(
    os.path.dirname(__file__),
    # providers -> stt -> services -> app -> api_gateway -> apps -> repo root (6 up)
    "..", "..", "..", "..", "..", "..",
    "apps", "model_service", "vendor", "qwen3-asr.cpp", "build", "qwen3-asr-cli",
)

# Map our short language codes to qwen3-asr.cpp's -l/--language values; unknown ->
# auto-detect (omit the flag, which triggers the binary's built-in detection). The
# CLI accepts short codes like en/zh/ja here (verified against qwen3-asr-cli --help).
_LANG = {
    "vi": "vi", "en": "en", "zh": "zh",
    "ja": "ja", "ko": "ko", "yue": "yue",
}

# A single dedicated worker thread for all transcribes. The conversation warms STT
# in the background while the user speaks turn 1, so without pinning, turn-1
# transcribe and the warm could spawn two binary processes at once and thrash a
# CPU-bound machine. One thread makes subprocess launches serialize; it also mirrors
# the MLX providers' executor shape so the engine registry is uniform.
_INFER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="qwen3-asr-gguf"
)


def resolve_qwen3_asr_gguf_binary() -> str | None:
    """Locate the qwen3-asr-cli binary, or None if not installed.

    Precedence (highest first): the engine-config `binary_path` (registry
    sentinel row > env STT_QWEN3_ASR_GGUF_BINARY_PATH > default ""), then a PATH
    lookup, then the build script's default output dir. Mirrors _ollama_bin()."""
    config = resolve_stt_engine_config("qwen3_asr_gguf")
    candidates = [
        (config.get("binary_path") or "").strip(),
        shutil.which("qwen3-asr-cli"),
        os.path.normpath(_DEFAULT_BUILD_BIN),
    ]
    for c in candidates:
        if c and (shutil.which(c) or os.path.isfile(c)):
            return c
    return None


def resolve_qwen3_asr_gguf_model() -> str:
    """Absolute path to the GGUF weights file (engine-config default_model)."""
    return resolve_stt_engine_config("qwen3_asr_gguf")["default_model"]


def _extract_text(stdout: str) -> str:
    """qwen3-asr-cli prints the transcript to stdout; strip trailing whitespace
    and drop any empty lines it emits around the text."""
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    return "\n".join(lines).strip()


class Qwen3AsrGgufProvider(STTProvider):
    name = "qwen3_asr_gguf"

    def available(self) -> bool:
        return resolve_qwen3_asr_gguf_binary() is not None and os.path.isfile(
            resolve_qwen3_asr_gguf_model()
        )

    def detail(self) -> str:
        model = os.path.basename(resolve_qwen3_asr_gguf_model()) or "GGUF"
        return f"{model} · CPU/Metal (qwen3-asr.cpp) · multilingual incl. Vietnamese"

    def _transcribe(self, wav_path: str, language: str | None, model: str | None = None) -> str:
        binary = resolve_qwen3_asr_gguf_binary()
        if binary is None:
            raise RuntimeError(
                "Qwen3-ASR GGUF needs the qwen3-asr-cli binary. Build it with "
                "apps/model_service/scripts/build_qwen3_asr_cpp.sh or set "
                "STT_QWEN3_ASR_GGUF_BINARY_PATH."
            )
        config = resolve_stt_engine_config("qwen3_asr_gguf")
        # `model` is an optional per-call override. Callers may pass a real GGUF
        # path, but the OpenAI surface forwards the *engine/model id* (e.g.
        # "qwen3_asr_gguf") in this field -- only honour it as a path when it
        # actually points at a file, else fall back to the configured default.
        override = (model or "").strip()
        model_path = override if (override and os.path.isfile(override)) else config["default_model"]
        if not os.path.isfile(model_path):
            raise RuntimeError(f"Qwen3-ASR GGUF model file not found: {model_path!r}")

        # --no-timing keeps stdout to just the transcript (timing/status already go
        # to stderr, but be explicit so _extract_text never picks up a timing line).
        argv = [
            binary, "-m", model_path, "-f", wav_path,
            "-t", str(int(config.get("n_threads", 8))), "--no-timing",
        ]
        lang = _LANG.get((language or "").lower())
        if lang:
            argv += ["--language", lang]

        timeout = float(config.get("timeout_seconds", 120.0))
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=True
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"qwen3-asr-cli timed out after {timeout}s") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise RuntimeError(f"qwen3-asr-cli failed (exit {exc.returncode}): {detail}") from exc
        return _extract_text(proc.stdout)

    def warm(self) -> None:
        # No persistent model handle to build (each transcribe is a fresh
        # process), so warm is a cheap availability probe on the dedicated
        # thread — keeps the STTProvider warm() contract uniform.
        try:
            _INFER_EXECUTOR.submit(self.available).result()
        except Exception:  # noqa: BLE001 - best-effort warm
            pass

    async def transcribe_bytes(
        self, audio_bytes: bytes, language: str | None = None, model: str | None = None
    ) -> STTResult:
        # The binary needs 16 kHz mono PCM16 and won't resample -- downmix +
        # resample here (no-op if already 16k mono). Any WAV the gateway accepts
        # (24k/48k, stereo) becomes what qwen3-asr-cli expects.
        try:
            pcm = wav_bytes_to_pcm16(audio_bytes, _TARGET_SR)
            audio_bytes = pcm16_to_wav_bytes(pcm, _TARGET_SR)
        except wave.Error as exc:
            # Not a WAV container at all (mp3/m4a/ogg upload, etc). qwen3-asr-cli
            # has no format sniffing of its own -- it reads whatever bytes land at
            # a WAV header's offsets and happily misreports garbage as e.g. "Audio
            # must be 16kHz, got <nonsense> Hz", which sent people chasing the
            # wrong bug. Raise our own clear error instead of letting the binary
            # see it at all.
            raise RuntimeError(
                "qwen3_asr_gguf needs WAV audio (16-bit PCM) -- got a non-WAV "
                "upload (mp3/m4a/ogg are not decoded by this engine). Convert to "
                "WAV first or use the browser mic recorder, which already emits WAV."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - a genuine WAV that failed to resample; let the binary report it
            # Logged (not just swallowed): this once masked a missing `scipy`
            # dependency in the model_service image -- the resample silently
            # no-opped and the binary rejected the un-resampled audio with its
            # own (much less useful) "must be 16kHz" error.
            logger.warning("qwen3_asr_gguf: resample to %dHz failed, sending audio as-is: %s", _TARGET_SR, exc)
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(_INFER_EXECUTOR, self._transcribe, tmp, language, model)
            return STTResult(engine=self.name, text=text, is_final=True, confidence=None)
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)
