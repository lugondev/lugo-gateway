import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.model_registry.config_schema import config_schema_for


def _by_key(fields):
    return {f["key"]: f for f in fields}


def test_whisper_local_rich_schema_with_correct_types():
    fields = config_schema_for("stt", "whisper_local")
    by = _by_key(fields)
    assert by["default_model"] == {"key": "default_model", "type": "str", "default": "large-v3-turbo"}
    # bool must be reported as bool, not int (bool is an int subclass)
    assert by["vad_filter"]["type"] == "bool"
    assert by["beam_size"]["type"] == "int"
    assert by["condition_on_previous_text"]["type"] == "bool"


def test_remote_engines_expose_only_timeout_seconds():
    # config_schema_for keys off engine, not kind, for remote engines -- the
    # kind arg is irrelevant here, so pass a fixed one.
    for engine in ("http_stt", "http_tts", "whisper_service", "eventlab"):
        fields = config_schema_for("stt", engine)
        assert set(_by_key(fields)) == {"timeout_seconds"}
        assert _by_key(fields)["timeout_seconds"]["type"] == "float"


def test_omnivoice_exposes_its_config_fields():
    fields = config_schema_for("tts", "omnivoice")
    by = _by_key(fields)
    assert "omnivoice_model_id" in by and by["omnivoice_model_id"]["type"] == "str"
    assert by["omnivoice_use_server"]["type"] == "bool"
    assert by["omnivoice_server_port"]["type"] == "int"
    assert by["omnivoice_timeout_seconds"]["type"] == "float"


def test_unknown_and_llm_engines_have_no_fields():
    assert config_schema_for("llm", "openrouter") == []
    assert config_schema_for("tts", "edge_tts") == []
    assert config_schema_for("stt", "made_up") == []


@pytest.fixture
def client():
    return TestClient(app)


def test_endpoint_returns_fields(client):
    r = client.get("/v1/model_registry/config_schema", params={"kind": "stt", "engine": "whisper_local"})
    assert r.status_code == 200
    keys = {f["key"] for f in r.json()["fields"]}
    assert "beam_size" in keys and "vad_filter" in keys


def test_endpoint_empty_for_llm(client):
    r = client.get("/v1/model_registry/config_schema", params={"kind": "llm", "engine": "openrouter"})
    assert r.status_code == 200
    assert r.json() == {"fields": []}


def test_qwencloud_schema_has_enum_choices_and_defaults():
    fields = config_schema_for("stt", "qwencloud")
    by = _by_key(fields)
    assert set(by) == {"realtime_model", "language", "turn_detection",
                       "semantic_punctuation", "timeout_seconds"}
    assert by["realtime_model"]["default"] == "qwen3-asr-flash-realtime"
    assert by["realtime_model"]["choices"] == ["qwen3-asr-flash-realtime", "fun-asr-realtime"]
    assert by["turn_detection"]["choices"] == ["server_vad", "manual"]
    assert by["turn_detection"]["default"] == "server_vad"
    assert by["semantic_punctuation"]["type"] == "bool"
    assert by["timeout_seconds"]["type"] == "float"
    # non-enum fields carry no `choices` key
    assert "choices" not in by["language"]
