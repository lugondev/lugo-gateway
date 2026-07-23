from app.core.settings import Settings


def test_deploy_time_stt_settings_have_expected_defaults():
    s = Settings(_env_file=None)
    assert s.ollama_bin == ""
    assert s.warmup_on_startup is True
    assert s.warmup_startup_timeout_s == 180
    assert s.stt_model_dir == "models/stt"
    assert s.vosk_model_base_url == "https://alphacephei.com/vosk/models"
    assert s.stt_stream_sample_rate == 16000
    assert s.stt_glossary_path == ""
    assert s.pyannote_vad_model == "pyannote/segmentation-3.0"
    assert s.pyannote_auth_token == ""


def test_deploy_time_stt_settings_accept_explicit_overrides():
    s = Settings(
        _env_file=None,
        vosk_model_base_url="https://example.com/models",
        stt_stream_sample_rate=8000,
        pyannote_auth_token="hf_test_token",
    )
    assert s.vosk_model_base_url == "https://example.com/models"
    assert s.stt_stream_sample_rate == 8000
    assert s.pyannote_auth_token == "hf_test_token"
