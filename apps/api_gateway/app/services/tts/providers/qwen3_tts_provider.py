"""Qwen3-TTS (0.6B/1.7B) engine — voice clone (Base) + preset speakers (CustomVoice).

Package: qwen-tts (`pip install -U qwen-tts`). Not on this project's core
dependency list — optional, like voxcpm/kokoro-vietnamese; gated by
``available()``. Officially supports 10 languages (not Vietnamese), but
``language="Auto"`` has been verified to produce acceptable Vietnamese
output.
"""

import os


def _pick_device_dtype_attn():
    """Auto-detect device/dtype/attn-impl; ``QWEN3_TTS_DEVICE`` overrides."""
    import torch

    device = os.environ.get("QWEN3_TTS_DEVICE") or (
        "cuda:0"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    if device.startswith("cuda"):
        return device, torch.bfloat16, "flash_attention_2"
    if device == "mps":
        return device, torch.float16, None
    return device, torch.float32, None
