import pytest
from fastapi.testclient import TestClient

from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest
from model_service.app.config import ServiceConfig
from model_service.app.main import create_app

_CFG = ServiceConfig(kind="tts", engine="vieneu", api_token="t0ken")
_AUTH = {"Authorization": "Bearer t0ken"}


class _FakeTTS:
    name = "vieneu"

    def __init__(self, exc: Exception | None = None):
        self.calls: list[TTSRequest] = []
        self._exc = exc

    async def render_wav(self, payload: TTSRequest) -> bytes:
        self.calls.append(payload)
        if self._exc:
            raise self._exc
        return b"RIFFWAVEDATA"


def _client(provider):
    return TestClient(create_app(config=_CFG, provider=provider))


def test_synthesizes_and_returns_wav_bytes():
    r = _client(_FakeTTS()).post("/v1/audio/speech", headers=_AUTH, json={"input": "xin chào"})
    assert r.status_code == 200
    assert r.content == b"RIFFWAVEDATA"
    assert r.headers["content-type"] == "audio/wav"


def test_maps_openai_fields_onto_the_tts_request():
    provider = _FakeTTS()
    _client(provider).post(
        "/v1/audio/speech",
        headers=_AUTH,
        json={"input": "xin chào", "voice": "vi-female-1", "speed": 1.25},
    )
    payload = provider.calls[0]
    assert (payload.text, payload.voice, payload.speed, payload.engine) == (
        "xin chào", "vi-female-1", 1.25, "vieneu",
    )


def test_requires_auth():
    assert _client(_FakeTTS()).post("/v1/audio/speech", json={"input": "hi"}).status_code == 401


def test_empty_input_is_rejected():
    r = _client(_FakeTTS()).post("/v1/audio/speech", headers=_AUTH, json={"input": ""})
    assert r.status_code == 422


def test_provider_error_becomes_502():
    r = _client(_FakeTTS(exc=ProviderError("vieneu synthesis failed: oom"))).post(
        "/v1/audio/speech", headers=_AUTH, json={"input": "xin chào"}
    )
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "provider_error"


def test_tts_container_does_not_expose_transcriptions():
    r = _client(_FakeTTS()).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert r.status_code == 404


def test_models_lists_the_running_engine():
    r = _client(_FakeTTS()).get("/v1/models", headers=_AUTH)
    assert [m["id"] for m in r.json()["data"]] == ["vieneu"]
