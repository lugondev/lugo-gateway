from app.services.system_config import SystemConfigStore


def test_default_when_empty(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    assert s.get().base_context == ""


def test_set_persists_across_instances(tmp_path):
    p = str(tmp_path / "system_config.json")
    SystemConfigStore(p).set_base_context("hello")
    assert SystemConfigStore(p).get().base_context == "hello"


def test_imports_legacy_and_keeps_file(tmp_path):
    from app.services.system_config import SystemConfig

    p = tmp_path / "system_config.json"
    p.write_text(SystemConfig(base_context="seeded").model_dump_json())
    s = SystemConfigStore(str(p))
    assert s.get().base_context == "seeded"
    assert p.exists()  # legacy file kept as backup, never deleted


def test_malformed_legacy_file_falls_back_to_defaults(tmp_path, caplog):
    import logging

    p = tmp_path / "system_config.json"
    p.write_text("{not valid json")
    with caplog.at_level(logging.WARNING):
        s = SystemConfigStore(str(p))
        assert s.get().base_context == ""
    assert p.exists()


def test_honors_settings_path_set_after_construction(tmp_path, monkeypatch):
    """Same singleton-timing hazard as the keyed stores: system_config_store
    is constructed once at import time, so _ensure() must re-read
    settings.system_config_path lazily rather than a value captured eagerly."""
    from app.core.settings import settings
    from app.services.system_config import SystemConfig, SystemConfigStore

    # constructed the same way the real singleton is (settings_attr, no explicit
    # path) and BEFORE the monkeypatch, like the real module-level singleton
    store = SystemConfigStore(settings_attr="system_config_path")

    seeded = tmp_path / "system_config.json"
    seeded.write_text(SystemConfig(base_context="from-settings-path").model_dump_json())
    monkeypatch.setattr(settings, "system_config_path", str(seeded))

    assert store.get().base_context == "from-settings-path"


def test_never_falls_back_to_real_default_path(tmp_path, monkeypatch):
    from app.core.settings import settings
    from app.services.system_config import SystemConfigStore

    store = SystemConfigStore(settings_attr="system_config_path")
    monkeypatch.setattr(settings, "system_config_path", str(tmp_path / "nonexistent.json"))
    assert store.get().base_context == ""



def test_engine_defaults_have_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    e = s.get().engines
    assert e.default_stt_engine == "vosk"
    assert e.default_tts_engine == "omnivoice"
    assert e.default_tts_engine_voice == ""
    assert e.extra_warmup_stt_engines == ""
    assert e.extra_warmup_tts_engines == ""
    assert e.warmup_on_startup is True
    assert e.warmup_startup_timeout_s == 180


def test_stt_local_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().stt_local
    assert c.stt_model_dir == "models/stt"
    assert c.vosk_model_path == "models/stt/vosk-model-small-en-us-0.15"
    assert c.vosk_model_base_url == "https://alphacephei.com/vosk/models"
    assert c.stt_stream_sample_rate == 16000
    assert c.whisper_local_model == "phowhisper-medium"
    assert c.whisper_local_device == "cpu"
    assert c.whisper_local_compute_type == "int8"
    assert c.whisper_vad_filter is True
    assert c.whisper_beam_size == 1
    assert c.whisper_condition_on_previous_text is False
    assert c.whisper_initial_prompt == ""
    assert c.stt_glossary_path == ""
    assert c.stt_profile == ""
    assert c.whisper_mlx_model_path == "models/stt/phowhisper-medium-mlx"
    assert c.qwen3_asr_model == "Qwen/Qwen3-ASR-0.6B"
    assert c.qwen3_asr_device == ""
    assert c.stt_enhance_timeout_seconds == 30.0
    assert "ASR post-editor" in c.stt_enhance_prompt
    assert c.stt_segment_long_enabled is False
    assert c.stt_segment_min_seconds == 30.0
    assert c.stt_segment_concurrency == 4


def test_omnivoice_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    o = s.get().omnivoice
    assert o.omnivoice_path == "/Users/lugon/code/OmniVoice"
    assert o.omnivoice_model_id == "k2-fsa/OmniVoice"
    assert o.omnivoice_device == ""
    assert o.omnivoice_dtype == "float16"
    assert o.omnivoice_python == ""
    assert o.omnivoice_timeout_seconds == 45.0
    assert o.omnivoice_use_server is True
    assert o.omnivoice_server_host == "127.0.0.1"
    assert o.omnivoice_server_port == 8762
    assert o.omnivoice_server_startup_seconds == 60.0
    assert o.omnivoice_default_instruct == "female, young adult"
    assert o.omnivoice_class_temperature == 0.0
    assert o.omnivoice_pin_voice is True
    assert "giọng đọc tham chiếu" in o.omnivoice_ref_text


def test_conversation_llm_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().conversation_llm
    assert c.conversation_llm_base_url == ""
    assert c.conversation_llm_api_key == ""
    assert c.conversation_llm_model == "gpt-3.5-turbo"
    assert c.conversation_llm_timeout_seconds == 60.0
    assert c.ollama_bin == ""


def test_remote_stt_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().remote_stt
    assert c.whisper_service_base_url == ""
    assert c.whisper_service_api_key == ""
    assert c.whisper_service_model == "whisper-1"
    assert c.eventlab_base_url == ""
    assert c.eventlab_api_key == ""
    assert c.eventlab_model == "whisper-1"
    assert c.remote_stt_timeout_seconds == 60.0


def test_conversation_tuning_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().conversation
    assert c.conversation_silence_ms == 700
    assert c.conversation_min_silence_ms == 450
    assert c.conversation_adaptive_full_ms == 3000
    assert c.conversation_min_speech_ms == 300
    assert c.conversation_rms_threshold == 0.015
    assert c.conversation_preroll_ms == 600
    assert c.conversation_max_utterance_ms == 30000
    assert c.conversation_goodbye_text == "Hẹn gặp lại nha!"
    assert c.conversation_stt_engine == "whisper"
    assert c.conversation_fast_stt_engine == ""
    assert c.conversation_fast_stt_max_ms == 1500
    assert c.conversation_streaming_stt is False
    assert c.conversation_streaming_chunk_ms == 1000
    assert c.conversation_tts_engine == "omnivoice"
    assert c.conversation_tts_lookahead == 3
    assert c.conversation_opus_pace is False
    assert c.conversation_opus_prebuffer_frames == 5
    assert c.conversation_language == "vi"
    assert "helpful, concise voice assistant" in c.conversation_system_prompt


def test_preprocessing_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().preprocessing
    assert c.stt_vad_enabled is False
    assert c.stt_vad_backend == "energy"
    assert c.stt_noise_reduce_enabled is False
    assert c.stt_noise_reduce_amount == 0.85
    assert c.pyannote_vad_model == "pyannote/segmentation-3.0"
    assert c.pyannote_auth_token == ""


def test_set_replaces_full_config_and_persists(tmp_path):
    from app.services.system_config import SystemConfig

    p = str(tmp_path / "system_config.json")
    s1 = SystemConfigStore(p)
    current = s1.get()
    updated = current.model_copy(
        update={"engines": current.engines.model_copy(update={"default_stt_engine": "qwen3_asr"})}
    )
    result = s1.set(updated)
    assert result.engines.default_stt_engine == "qwen3_asr"

    s2 = SystemConfigStore(p)
    assert s2.get().engines.default_stt_engine == "qwen3_asr"


def test_warmup_stt_engines_combines_conversation_default_and_extras(tmp_path, monkeypatch):
    from app.services import system_config as sc_mod

    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={"engines": fresh.get().engines.model_copy(update={"extra_warmup_stt_engines": "qwen3_asr, whisper_mlx"})}
        )
    )
    monkeypatch.setattr(sc_mod, "system_config_store", fresh)
    result = sc_mod.warmup_stt_engines()
    assert result == [fresh.get().conversation.conversation_stt_engine, "qwen3_asr", "whisper_mlx"]
