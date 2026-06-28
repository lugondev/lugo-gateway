"""Audio-native STT via Qwen3-Omni (mlx-vlm, Apple GPU).

Unlike Whisper (audio -> text transcription only), Qwen-Omni is a multimodal LLM
that ingests audio directly. Here we use it as an ASR stage: audio -> Vietnamese
text (the speech-output limitation of Qwen-Omni doesn't matter — VieNeu handles
Vietnamese TTS). Understands accent/noise/context better than Whisper, at the cost
of a much heavier model (30B MoE) and higher latency.

Available only when mlx-vlm is installed (Apple Silicon) and the selected model is
cached; otherwise the engine hides and callers fall back to whisper/whisper_mlx.
"""

import asyncio
import os
import re
import tempfile

from app.core.settings import settings
from app.schemas.stt import STTResult
from app.services.stt.base import STTProvider

# Cache loaded (model, processor, config) per model id — loads are very expensive.
_MODEL_CACHE: dict[str, tuple] = {}
_active_model: str | None = None


def get_active_qwen_omni_model() -> str:
    return _active_model or settings.qwen_omni_model


def set_active_qwen_omni_model(model: str) -> None:
    global _active_model
    _active_model = model


# Strip Qwen chat/reasoning control tokens that occasionally leak into the output.
_SPECIAL_RE = re.compile(r"<\|[^|]*\|>|</?think>")


def _clean(text: str) -> str:
    text = _SPECIAL_RE.sub("", text)
    return text.strip()


def _mlx_vlm_version() -> str:
    try:
        from importlib.metadata import version

        return version("mlx-vlm")
    except Exception:  # noqa: BLE001
        return "unknown"


_patched = False


def _patch_mlx_vlm_qwen_audio() -> None:
    """Work around an mlx-vlm 0.6.3 bug for Qwen3-Omni audio.

    process_inputs() forwards the audio *file paths* straight to the HF Qwen3-Omni
    processor, whose audio handler expects decoded waveform arrays — so it throws
    "could not convert string to float: '<path>.wav'". We decode the paths to float32
    arrays (at the processor's sampling rate) before they reach the processor.
    """
    global _patched
    if _patched:
        return
    import mlx_vlm.utils as u

    orig = u.process_inputs_with_fallback

    def patched(processor, *args, **kw):
        cls = processor.__class__.__name__.lower()
        audio = kw.get("audio")
        if "qwen3" in cls and "omni" in cls and audio:
            sr = getattr(getattr(processor, "feature_extractor", None), "sampling_rate", 16000)
            kw["audio"] = [
                u.load_audio(a, sr=sr) if isinstance(a, str) else a for a in audio
            ]
        return orig(processor, *args, **kw)

    u.process_inputs_with_fallback = patched
    _patched = True


def _is_cached(model_id: str) -> bool:
    from app.core.hf_cache import hub_dir

    hub = hub_dir()
    if not hub.is_dir():
        return False
    d = hub / f"models--{model_id.replace('/', '--')}"
    if not d.is_dir():
        return False
    blobs = d / "blobs"
    # A partial download leaves *.incomplete blobs — not usable yet.
    if blobs.is_dir() and any(blobs.glob("*.incomplete")):
        return False
    return True


class QwenOmniProvider(STTProvider):
    name = "qwen_omni"

    def available(self) -> bool:
        try:
            import mlx_vlm  # noqa: F401
        except ImportError:
            return False
        return _is_cached(get_active_qwen_omni_model())

    def detail(self) -> str:
        return f"{get_active_qwen_omni_model().split('/')[-1]} · Apple GPU (MLX)"

    def _load(self):
        from mlx_vlm import load

        _patch_mlx_vlm_qwen_audio()
        model_id = get_active_qwen_omni_model()
        if model_id not in _MODEL_CACHE:
            model, processor = load(model_id)
            config = getattr(model, "config", None)
            if config is None:  # fall back to reading the snapshot's config.json
                from mlx_vlm.utils import get_model_path, load_config

                config = load_config(get_model_path(model_id))
            _MODEL_CACHE[model_id] = (model, processor, config)
        return _MODEL_CACHE[model_id]

    def _generate(self, wav_path: str, prompt_text: str, max_tokens: int) -> str:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        model, processor, config = self._load()
        prompt = apply_chat_template(processor, config, prompt_text, num_audios=1)
        try:
            out = generate(
                model,
                processor,
                prompt,
                audio=[wav_path],
                max_tokens=max_tokens,
                temperature=0.0,  # greedy: avoids degenerate special-token outputs
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001 - surface a clean error to the caller
            raise RuntimeError(
                f"Qwen3-Omni inference failed (mlx-vlm {_mlx_vlm_version()}): {exc}"
            ) from exc
        return _clean(str(getattr(out, "text", out)))

    def _transcribe(self, wav_path: str) -> str:
        return self._generate(wav_path, settings.qwen_omni_prompt, settings.qwen_omni_max_tokens)

    def _reply_text(self, prompt_text: str) -> str:
        """Text-only chat generation with Qwen-Omni (no audio) — used as the LLM."""
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        model, processor, config = self._load()
        prompt = apply_chat_template(processor, config, prompt_text, num_audios=0)
        try:
            out = generate(model, processor, prompt, max_tokens=256, temperature=0.0, verbose=False)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Qwen3-Omni reply failed: {exc}") from exc
        return _clean(str(getattr(out, "text", out)))

    def _converse(self, wav_path: str, history: list[dict], system_prompt: str) -> tuple[str, str]:
        """Audio-native conversation, two reliable Qwen-Omni passes (no separate gemma):
        1) transcribe the audio, 2) generate the reply from the transcript + history.

        (A single audio->reply pass with a forced format is unreliable on real mic
        audio — it often degenerates — so we split it into the two robust prompts.)
        """
        transcript = self._transcribe(wav_path)
        if not transcript:
            return "", ""
        convo = system_prompt.strip() + "\n\n"
        for m in history[-6:]:
            who = "Người dùng" if m.get("role") == "user" else "Trợ lý"
            convo += f"{who}: {m.get('content', '')}\n"
        convo += f"Người dùng: {transcript}\nHãy trả lời ngắn gọn, tự nhiên bằng tiếng Việt."
        reply = self._reply_text(convo)
        return transcript, reply

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

    async def converse(self, audio_bytes: bytes, history: list[dict], system_prompt: str) -> tuple[str, str]:
        """Return (user_transcript, assistant_reply) from the audio in one model pass."""
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            return await asyncio.to_thread(self._converse, tmp, history, system_prompt)
        finally:
            if tmp and os.path.isfile(tmp):
                os.unlink(tmp)
