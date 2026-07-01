from app.services.stt.routing import select_stt_engine


def test_short_utterance_routes_to_fast_engine():
    engine = select_stt_engine(
        speech_ms=800, default_engine="whisper", fast_engine="qwen3_asr", fast_max_ms=1500
    )
    assert engine == "qwen3_asr"


def test_long_utterance_routes_to_default_engine():
    engine = select_stt_engine(
        speech_ms=5000, default_engine="whisper", fast_engine="qwen3_asr", fast_max_ms=1500
    )
    assert engine == "whisper"


def test_boundary_is_inclusive_for_fast_engine():
    engine = select_stt_engine(
        speech_ms=1500, default_engine="whisper", fast_engine="qwen3_asr", fast_max_ms=1500
    )
    assert engine == "qwen3_asr"


def test_no_fast_engine_configured_uses_default():
    assert select_stt_engine(speech_ms=100, default_engine="whisper") == "whisper"
    assert (
        select_stt_engine(speech_ms=100, default_engine="whisper", fast_engine="", fast_max_ms=1500)
        == "whisper"
    )


def test_zero_threshold_disables_fast_path():
    engine = select_stt_engine(
        speech_ms=100, default_engine="whisper", fast_engine="qwen3_asr", fast_max_ms=0
    )
    assert engine == "whisper"


def test_unavailable_fast_engine_falls_back_to_default():
    engine = select_stt_engine(
        speech_ms=500,
        default_engine="whisper",
        fast_engine="qwen3_asr",
        fast_max_ms=1500,
        available={"whisper", "vosk"},
    )
    assert engine == "whisper"


def test_same_fast_and_default_engine_is_noop():
    engine = select_stt_engine(
        speech_ms=500, default_engine="whisper", fast_engine="whisper", fast_max_ms=1500
    )
    assert engine == "whisper"
