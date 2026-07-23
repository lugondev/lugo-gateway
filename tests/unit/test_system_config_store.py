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
    assert not hasattr(e, "warmup_on_startup")
    assert not hasattr(e, "warmup_startup_timeout_s")
    assert not hasattr(e, "ollama_bin")


def test_stt_local_config_has_expected_defaults(tmp_path):
    s = SystemConfigStore(str(tmp_path / "system_config.json"))
    c = s.get().stt_local
    assert not hasattr(c, "stt_model_dir")
    assert not hasattr(c, "vosk_model_base_url")
    assert not hasattr(c, "stt_stream_sample_rate")
    assert not hasattr(c, "stt_glossary_path")
    assert not hasattr(c, "stt_profile")  # preset layer removed
    assert c.stt_segment_long_enabled is False
    assert c.stt_segment_min_seconds == 30.0
    assert c.stt_segment_concurrency == 4


def test_system_config_has_no_stt_local_device_fields():
    from app.services.system_config import SystemConfig

    dumped = SystemConfig().model_dump()
    assert "whisper_local_device" not in dumped["stt_local"]
    assert "whisper_local_compute_type" not in dumped["stt_local"]
    assert "qwen3_asr_device" not in dumped["stt_local"]


def test_stt_local_has_no_per_engine_model_or_tuning_fields():
    """Every model is a Model Registry entry now -- no engine gets its own
    SystemConfig fields for default model / model path / decode tuning (all
    moved to the model_id="" sentinel rows' config)."""
    from app.services.system_config import SystemConfig

    dumped = SystemConfig().model_dump()
    for field in (
        "vosk_model_path",
        "whisper_local_model",
        "whisper_vad_filter",
        "whisper_beam_size",
        "whisper_condition_on_previous_text",
        "whisper_initial_prompt",
        "whisper_mlx_model_path",
        "qwen3_asr_model",
    ):
        assert field not in dumped["stt_local"], field


def test_system_config_has_no_omnivoice_or_remote_stt_groups():
    from app.services.system_config import SystemConfig

    dumped = SystemConfig().model_dump()
    assert "omnivoice" not in dumped
    assert "remote_stt" not in dumped


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
    assert c.llm_timeout_seconds == 60.0
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
