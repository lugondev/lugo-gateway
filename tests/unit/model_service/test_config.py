import pytest

from model_service.app.config import ConfigError, load_config

_VALID = {"SERVICE_KIND": "stt", "SERVICE_ENGINE": "whisper_local", "SERVICE_API_TOKEN": "t0ken"}


def test_loads_a_valid_env():
    cfg = load_config(_VALID)
    assert (cfg.kind, cfg.engine, cfg.api_token, cfg.port) == ("stt", "whisper_local", "t0ken", 8100)


def test_kind_is_normalized():
    assert load_config({**_VALID, "SERVICE_KIND": " STT "}).kind == "stt"


@pytest.mark.parametrize("kind", ["", "llm", "nonsense"])
def test_rejects_bad_kind(kind):
    with pytest.raises(ConfigError, match="SERVICE_KIND"):
        load_config({**_VALID, "SERVICE_KIND": kind})


def test_rejects_missing_engine():
    with pytest.raises(ConfigError, match="SERVICE_ENGINE"):
        load_config({**_VALID, "SERVICE_ENGINE": "  "})


def test_rejects_missing_token():
    # The token is mandatory: an unauthenticated STT container that gets its
    # port published hands out free GPU to anyone who finds it.
    with pytest.raises(ConfigError, match="SERVICE_API_TOKEN"):
        load_config({**_VALID, "SERVICE_API_TOKEN": ""})


def test_port_is_overridable():
    assert load_config({**_VALID, "SERVICE_PORT": "9000"}).port == 9000
