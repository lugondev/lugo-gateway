import pytest

from app.services.model_registry import resolve
from app.services.model_registry.resolve import EnvVarError
from app.services.model_registry.store import model_registry_store


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch):
    # Container conditions: nothing has awaited the store, so the cache is cold
    # and find_sync returns None for every lookup.
    monkeypatch.setattr(model_registry_store, "_by_id", None, raising=False)


def test_env_overrides_default_when_no_registry_row(monkeypatch):
    monkeypatch.setenv("STT_WHISPER_LOCAL_DEFAULT_MODEL", "large-v3")
    assert resolve.resolve_stt_engine_config("whisper_local")["default_model"] == "large-v3"


def test_env_is_coerced_to_the_default_s_type(monkeypatch):
    monkeypatch.setenv("STT_WHISPER_LOCAL_VAD_FILTER", "false")
    monkeypatch.setenv("STT_WHISPER_LOCAL_BEAM_SIZE", "5")
    cfg = resolve.resolve_stt_engine_config("whisper_local")
    assert cfg["vad_filter"] is False
    assert cfg["beam_size"] == 5


def test_default_survives_when_env_absent():
    assert resolve.resolve_stt_engine_config("whisper_local")["beam_size"] == 1


def test_env_overrides_device_and_compute_type(monkeypatch):
    monkeypatch.setenv("STT_WHISPER_LOCAL_DEVICE", "cuda")
    monkeypatch.setenv("STT_WHISPER_LOCAL_COMPUTE_TYPE", "float16")
    assert resolve.resolve_stt_local_device("whisper_local") == {
        "device": "cuda",
        "compute_type": "float16",
    }


def test_device_resolver_returns_only_its_two_keys(monkeypatch):
    monkeypatch.setenv("STT_WHISPER_LOCAL_DEVICE", "cuda")
    assert set(resolve.resolve_stt_local_device("whisper_local")) == {"device", "compute_type"}


def test_bad_int_env_raises_a_clear_error_naming_the_var_and_value(monkeypatch):
    monkeypatch.setenv("STT_WHISPER_LOCAL_BEAM_SIZE", "not-a-number")
    with pytest.raises(EnvVarError, match="STT_WHISPER_LOCAL_BEAM_SIZE"):
        resolve.resolve_stt_engine_config("whisper_local")


def test_bad_float_env_raises_a_clear_error_naming_the_var_and_value():
    # None of today's STT_ENGINE_CONFIG_DEFAULTS are float-typed, so there's
    # no env var name that exercises this branch end-to-end through
    # resolve_stt_engine_config -- test _coerce's float branch directly.
    with pytest.raises(EnvVarError, match="STT_SOME_FLOAT_VAR.*not a valid float"):
        resolve._coerce("not-a-number", 1.5, "STT_SOME_FLOAT_VAR")


def test_unrecognized_bool_env_string_raises_instead_of_silently_becoming_false(monkeypatch):
    # `raw.lower() in (...)` used to make any unrecognized string False --
    # dangerous for a container whose only config surface is env.
    monkeypatch.setenv("STT_WHISPER_LOCAL_VAD_FILTER", "banana")
    with pytest.raises(EnvVarError, match="STT_WHISPER_LOCAL_VAD_FILTER"):
        resolve.resolve_stt_engine_config("whisper_local")


def test_omnivoice_env_overrides_defaults(monkeypatch):
    # A single-engine omnivoice container has no registry DB and no second venv:
    # without these overrides omnivoice_python_path stays the dev-machine path
    # (/Users/lugon/code/OmniVoice/.venv/bin/python) and the engine reports
    # unavailable inside the image.
    monkeypatch.setenv("OMNIVOICE_PATH", "/app")
    monkeypatch.setenv("OMNIVOICE_PYTHON", "/usr/local/bin/python")
    monkeypatch.setenv("OMNIVOICE_DEVICE", "cpu")
    monkeypatch.setenv("OMNIVOICE_DTYPE", "float32")
    monkeypatch.setenv("OMNIVOICE_NUM_STEP", "8")
    monkeypatch.setenv("OMNIVOICE_USE_SERVER", "false")
    monkeypatch.setenv("OMNIVOICE_TIMEOUT_SECONDS", "120.5")
    cfg = resolve.resolve_omnivoice_config()
    assert cfg.omnivoice_path == "/app"
    assert cfg.omnivoice_python_path == "/usr/local/bin/python"
    assert cfg.omnivoice_device == "cpu"
    assert cfg.omnivoice_dtype == "float32"
    assert cfg.omnivoice_num_step == 8  # coerced to int
    assert cfg.omnivoice_use_server is False  # coerced to bool
    assert cfg.omnivoice_timeout_seconds == 120.5  # coerced to float
    assert cfg.omnivoice_server_port == 8762  # untouched default


def test_omnivoice_bad_env_raises_naming_the_var(monkeypatch):
    monkeypatch.setenv("OMNIVOICE_NUM_STEP", "many")
    with pytest.raises(EnvVarError, match="OMNIVOICE_NUM_STEP"):
        resolve.resolve_omnivoice_config()


def test_omnivoice_registry_row_beats_env(monkeypatch):
    monkeypatch.setattr(
        model_registry_store,
        "_by_id",
        {
            "x": {
                "id": "x", "kind": "tts", "engine": "omnivoice", "model_id": "",
                "enabled": True, "stage": "stable", "label": "", "api_key": "",
                "base_url": "", "config": {"omnivoice_device": "from-registry"},
            }
        },
        raising=False,
    )
    monkeypatch.setenv("OMNIVOICE_DEVICE", "from-env")
    assert resolve.resolve_omnivoice_config().omnivoice_device == "from-registry"


def test_registry_row_beats_env(monkeypatch):
    # Gateway conditions: a warm cache with a sentinel row must win, so existing
    # deployments are unaffected by env vars that happen to be set.
    monkeypatch.setattr(
        model_registry_store,
        "_by_id",
        {
            "x": {
                "id": "x", "kind": "stt", "engine": "whisper_local", "model_id": "",
                "enabled": True, "stage": "stable", "label": "", "api_key": "",
                "base_url": "", "config": {"default_model": "from-registry"},
            }
        },
        raising=False,
    )
    monkeypatch.setenv("STT_WHISPER_LOCAL_DEFAULT_MODEL", "from-env")
    assert resolve.resolve_stt_engine_config("whisper_local")["default_model"] == "from-registry"
