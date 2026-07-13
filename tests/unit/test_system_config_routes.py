import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.system_config import SystemConfigStore


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.api.routes.system.system_config_store", fresh)
    return fresh


@pytest.fixture
def client():
    return TestClient(app)


def test_get_config_defaults_empty(client):
    resp = client.get("/v1/system/config")
    assert resp.status_code == 200
    assert resp.json()["data"]["base_context"] == ""


def test_set_config_base_context(client):
    resp = client.put("/v1/system/config", json={"base_context": "Platform: TeguVoice."})
    assert resp.status_code == 200
    assert resp.json()["data"]["base_context"] == "Platform: TeguVoice."
    assert client.get("/v1/system/config").json()["data"]["base_context"] == "Platform: TeguVoice."


def test_set_config_clears_base_context(client):
    client.put("/v1/system/config", json={"base_context": "something"})
    resp = client.put("/v1/system/config", json={"base_context": ""})
    assert resp.json()["data"]["base_context"] == ""


def test_get_config_defaults_openrouter_api_key_empty(client):
    resp = client.get("/v1/system/config")
    assert resp.json()["data"]["openrouter_api_key"] == ""


def test_set_openrouter_api_key_is_masked_in_response(client):
    resp = client.put("/v1/system/config", json={"openrouter_api_key": "sk-or-real-secret"})
    assert resp.json()["data"]["openrouter_api_key"] == "***"
    assert client.get("/v1/system/config").json()["data"]["openrouter_api_key"] == "***"


def test_blank_openrouter_api_key_preserves_existing(client):
    client.put("/v1/system/config", json={"openrouter_api_key": "sk-or-real-secret"})
    resp = client.put("/v1/system/config", json={"base_context": "x", "openrouter_api_key": ""})
    # Still masked (not empty) => the previously stored key was preserved, not wiped.
    assert resp.json()["data"]["openrouter_api_key"] == "***"
    assert resp.json()["data"]["base_context"] == "x"


def test_set_base_context_does_not_clear_openrouter_api_key(client):
    client.put("/v1/system/config", json={"openrouter_api_key": "sk-or-real-secret"})
    client.put("/v1/system/config", json={"base_context": "hello"})
    assert client.get("/v1/system/config").json()["data"]["openrouter_api_key"] == "***"


def test_get_config_includes_nested_groups_with_defaults(client):
    data = client.get("/v1/system/config").json()["data"]
    assert data["engines"]["default_stt_engine"] == "vosk"
    assert data["stt_local"]["whisper_local_model"] == "phowhisper-medium"
    assert data["omnivoice"]["omnivoice_model_id"] == "k2-fsa/OmniVoice"
    assert data["conversation_llm"]["conversation_llm_model"] == "gpt-3.5-turbo"
    assert data["remote_stt"]["whisper_service_model"] == "whisper-1"
    assert data["conversation"]["conversation_silence_ms"] == 700
    assert data["preprocessing"]["stt_vad_backend"] == "energy"


def test_put_updates_a_nested_field_and_preserves_others(client):
    full = client.get("/v1/system/config").json()["data"]
    full["engines"]["default_stt_engine"] = "qwen3_asr"
    resp = client.put("/v1/system/config", json=full)
    data = resp.json()["data"]
    assert data["engines"]["default_stt_engine"] == "qwen3_asr"
    assert data["stt_local"]["whisper_local_model"] == "phowhisper-medium"  # unrelated group untouched


@pytest.mark.parametrize(
    "group,field",
    [
        (None, "openrouter_api_key"),
        ("conversation_llm", "conversation_llm_api_key"),
        ("remote_stt", "whisper_service_api_key"),
        ("remote_stt", "eventlab_api_key"),
        ("preprocessing", "pyannote_auth_token"),
    ],
)
def test_secret_field_is_masked_and_blank_put_preserves_it(client, group, field):
    full = client.get("/v1/system/config").json()["data"]
    target = full if group is None else full[group]
    target[field] = "super-secret-value"
    masked = client.put("/v1/system/config", json=full).json()["data"]
    masked_target = masked if group is None else masked[group]
    assert masked_target[field] == "***"

    # Re-submit the whole form with the mask placeholder still in place (as the UI would).
    resubmit = client.get("/v1/system/config").json()["data"]
    still_masked = client.put("/v1/system/config", json=resubmit).json()["data"]
    still_masked_target = still_masked if group is None else still_masked[group]
    assert still_masked_target[field] == "***"  # still configured, not wiped


def test_partial_put_does_not_reset_unrelated_group_to_defaults(client):
    """Regression test: a PUT body that only contains base_context (exactly what the
    pre-existing saveBaseContext() JS sends) must not silently reset every other
    group/field back to its Pydantic default. Before the fix, PUT'ing a bare
    SystemConfig-shaped payload filled in ALL omitted fields with fresh defaults,
    wiping any customization made via the new grouped settings panel."""
    full = client.get("/v1/system/config").json()["data"]
    full["engines"]["default_stt_engine"] = "qwen3_asr"
    client.put("/v1/system/config", json=full)

    # Mirrors exactly what saveBaseContext() sends: only base_context, nothing else.
    resp = client.put("/v1/system/config", json={"base_context": "something else"})
    assert resp.json()["data"]["base_context"] == "something else"
    assert resp.json()["data"]["engines"]["default_stt_engine"] == "qwen3_asr"

    # Also confirmed via a fresh GET, not just the PUT response.
    data = client.get("/v1/system/config").json()["data"]
    assert data["engines"]["default_stt_engine"] == "qwen3_asr"


def test_malformed_field_type_returns_422_not_500(client):
    """Regression test: switching the route to manual request.json() +
    SystemConfig.model_validate() (to enable the deep-merge fix above) must not
    lose the structured 422 FastAPI gave for free when the param was a typed
    `payload: SystemConfig` -- a wrong-typed field should be a 422 JSON error,
    not a bare 500 text/plain response."""
    resp = client.put(
        "/v1/system/config",
        json={"engines": {"warmup_startup_timeout_s": "not-a-number"}},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"]


def test_non_dict_json_body_returns_422_not_500(client):
    """A JSON body that parses but isn't an object (e.g. a bare array) must also
    be a 422, not a 500 -- request.json() succeeding doesn't guarantee a dict."""
    resp = client.put(
        "/v1/system/config",
        content="[1, 2, 3]",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"]


def test_changing_qwen3_asr_device_clears_the_model_cache(client, monkeypatch):
    from app.services.stt.providers import qwen3_asr_provider as mod

    mod._MODEL_CACHE["cuda:some-model"] = object()
    full = client.get("/v1/system/config").json()["data"]
    full["stt_local"]["qwen3_asr_device"] = "cuda:1"
    client.put("/v1/system/config", json=full)
    assert mod._MODEL_CACHE == {}


def test_unrelated_field_change_does_not_clear_qwen3_asr_cache(client):
    from app.services.stt.providers import qwen3_asr_provider as mod

    sentinel = object()
    mod._MODEL_CACHE["cuda:some-model"] = sentinel
    full = client.get("/v1/system/config").json()["data"]
    full["base_context"] = "unrelated change"
    client.put("/v1/system/config", json=full)
    assert mod._MODEL_CACHE.get("cuda:some-model") is sentinel
