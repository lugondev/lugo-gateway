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
        "app.services.stt.model_catalog.apply_stt_model",
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


def test_transcribe_passes_model_to_provider(monkeypatch):
    seen = {}

    async def fake_tb(self, audio_bytes, language=None, model=None):
        seen["model"] = model
        from app.schemas.stt import STTResult
        return STTResult(engine="qwencloud", text="ok", is_final=True)

    from app.services.stt.providers.qwencloud_provider import QwenCloudSttProvider
    monkeypatch.setattr(QwenCloudSttProvider, "transcribe_bytes", fake_tb)

    import io
    import wave

    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 320)
    w.close()

    resp = client.post(
        "/v1/stt/transcribe",
        data={"engine": "qwencloud", "model": "fun-asr"},
        files={"audio": ("a.wav", buf.getvalue(), "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    assert seen["model"] == "fun-asr"
