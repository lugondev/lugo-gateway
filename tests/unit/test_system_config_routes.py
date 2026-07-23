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


def test_get_config_includes_nested_groups_with_defaults(client):
    data = client.get("/v1/system/config").json()["data"]
    assert data["engines"]["default_stt_engine"] == "vosk"
    assert data["engines"]["stt_segment_min_seconds"] == 30.0
    assert data["conversation"]["conversation_silence_ms"] == 700
    assert data["preprocessing"]["stt_vad_backend"] == "energy"


def test_put_updates_a_nested_field_and_preserves_others(client):
    full = client.get("/v1/system/config").json()["data"]
    full["engines"]["default_stt_engine"] = "qwen3_asr"
    resp = client.put("/v1/system/config", json=full)
    data = resp.json()["data"]
    assert data["engines"]["default_stt_engine"] == "qwen3_asr"
    assert data["conversation"]["conversation_silence_ms"] == 700  # unrelated group untouched


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
        json={"engines": {"default_stt_engine": 123}},
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


## NOTE: the qwen3_asr_device/remote_stt/omnivoice reinit-trigger tests that used to
## live here were removed in Task 7 along with the SystemConfig fields/groups they
## exercised. The equivalent reinit side-effects (clear_model_cache/
## reinit_remote_providers/reset_voice_ref_and_respawn) now live on
## PATCH /v1/model_registry/{id} -- see the ported tests in test_model_registry_routes.py:
## test_patch_qwen3_asr_config_clears_the_model_cache,
## test_patch_unrelated_qwen3_asr_field_does_not_clear_the_model_cache,
## test_patch_whisper_service_entry_rebuilds_the_provider,
## test_patch_entry_can_update_config (omnivoice config PATCH),
## test_patch_omnivoice_entry_respawns_the_sidecar.
