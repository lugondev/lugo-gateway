import pytest
from pydantic import ValidationError

from app.schemas.stt import STTRequest


def test_accepts_openrouter_engines():
    assert STTRequest(engine="qwen3_asr_or").engine == "qwen3_asr_or"
    assert STTRequest(engine="whisper_or").engine == "whisper_or"


def test_rejects_unknown_engine():
    with pytest.raises(ValidationError):
        STTRequest(engine="not_a_real_engine")
