from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_list_stt_models_known_engine_supports_variants():
    resp = client.get("/v1/stt/models", params={"engine": "qwen3_asr"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["engine"] == "qwen3_asr"
    assert data["supports_variants"] is True
    ids = {m["id"] for m in data["models"]}
    assert ids == {"0.6b", "1.7b"}
    assert all(m["valid"] is True for m in data["models"])
    assert all({"id", "label", "cached", "active", "valid"} <= set(m) for m in data["models"])


def test_list_stt_models_engine_without_registry():
    resp = client.get("/v1/stt/models", params={"engine": "vosk"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {"engine": "vosk", "supports_variants": False, "models": []}


def test_list_stt_models_requires_engine_param():
    resp = client.get("/v1/stt/models")
    assert resp.status_code == 422  # FastAPI required-query-param validation


def test_warm_stt_engine_with_explicit_model(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.stt.model_registry.apply_stt_model",
        lambda e, m: calls.append((e, m)),
    )

    resp = client.post("/v1/stt/warm", params={"engine": "qwen3_asr", "model": "1.7b"})
    assert resp.status_code == 200
    assert resp.json()["data"]["model"] == "1.7b"
    assert calls == [("qwen3_asr", "1.7b")]


def test_warm_stt_engine_rejects_invalid_model():
    resp = client.post("/v1/stt/warm", params={"engine": "qwen3_asr", "model": "not-real"})
    assert resp.status_code == 400


def test_warm_stt_engine_no_model_unchanged():
    resp = client.post("/v1/stt/warm", params={"engine": "vosk"})
    assert resp.status_code == 200
    assert resp.json()["data"]["model"] is None
