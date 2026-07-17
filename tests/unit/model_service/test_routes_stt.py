import pytest
from fastapi.testclient import TestClient

from app.core.errors import EngineNotFoundError
from app.schemas.stt import STTResult
from model_service.app.config import ServiceConfig
from model_service.app.main import create_app

_CFG = ServiceConfig(kind="stt", engine="whisper_local", api_token="t0ken")
_AUTH = {"Authorization": "Bearer t0ken"}


class _FakeSTT:
    name = "whisper_local"

    def __init__(self, exc: Exception | None = None):
        self.calls: list[tuple] = []
        self._exc = exc

    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        self.calls.append((audio_bytes, language, model))
        if self._exc:
            raise self._exc
        return STTResult(engine=self.name, text="xin chào", is_final=True, confidence=None)


def _client(provider):
    return TestClient(create_app(config=_CFG, provider=provider))


def test_transcribes_and_returns_openai_shape():
    client = _client(_FakeSTT())
    r = client.post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"RIFFDATA", "audio/wav")}
    )
    assert r.status_code == 200
    assert r.json() == {"text": "xin chào"}


def test_forwards_language_and_model_to_the_provider():
    # The gateway sends the registry entry's model_id here; it must reach the
    # provider, which takes a per-call model.
    provider = _FakeSTT()
    _client(provider).post(
        "/v1/audio/transcriptions",
        headers=_AUTH,
        files={"file": ("a.wav", b"RIFFDATA", "audio/wav")},
        data={"language": "vi", "model": "large-v3-turbo"},
    )
    assert provider.calls == [(b"RIFFDATA", "vi", "large-v3-turbo")]


def test_blank_language_and_model_become_none():
    provider = _FakeSTT()
    _client(provider).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert provider.calls == [(b"D", None, None)]


def test_requires_auth():
    r = _client(_FakeSTT()).post(
        "/v1/audio/transcriptions", files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert r.status_code == 401


def test_provider_error_becomes_502_in_the_openai_envelope():
    # Real STT providers never raise ProviderError -- they raise bare
    # RuntimeError (see apps/api_gateway/app/services/stt/providers/*.py).
    # This pins the path that actually occurs in production: routes_stt.py
    # must catch it and translate it into the envelope itself.
    r = _client(_FakeSTT(exc=RuntimeError("engine died"))).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert r.status_code == 502
    assert r.headers["content-type"] == "application/json"
    assert "engine died" in r.json()["error"]["message"]
    assert r.json()["error"]["type"] == "provider_error"


def test_non_wav_upload_surfaces_as_envelope_not_plain_text_500():
    # Simulates what a real provider does on unreadable/malformed audio (e.g.
    # vosk_provider.py raising RuntimeError on a bad WAV header): the raw
    # RuntimeError must not leak past routes_stt.py as a bare 500 text/plain.
    provider = _FakeSTT(exc=RuntimeError("Vosk requires a valid WAV PCM16 mono file."))
    r = _client(provider).post(
        "/v1/audio/transcriptions",
        headers=_AUTH,
        files={"file": ("not-audio.txt", b"this is not a wav file", "text/plain")},
    )
    assert r.status_code == 502
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert body["error"]["type"] == "provider_error"
    assert "WAV" in body["error"]["message"]


def test_404_carries_the_openai_envelope():
    # Starlette's router raises starlette.exceptions.HTTPException directly for
    # unmatched routes -- registering the handler on fastapi.HTTPException
    # alone would miss this and leak {"detail": "Not Found"}.
    r = _client(_FakeSTT()).get("/v1/no-such-route", headers=_AUTH)
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_engine_not_found_becomes_400():
    r = _client(_FakeSTT(exc=EngineNotFoundError("no such model"))).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_empty_upload_is_rejected():
    r = _client(_FakeSTT()).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"", "audio/wav")}
    )
    assert r.status_code == 400


def test_missing_file_field_is_rejected_in_the_openai_envelope():
    r = _client(_FakeSTT()).post(
        "/v1/audio/transcriptions", headers=_AUTH, data={"language": "vi"}
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


@pytest.mark.asyncio
async def test_validation_error_handler_survives_an_empty_errors_list():
    # Every real FastAPI/pydantic validation failure produces at least one
    # error item, but exc.errors()[0] being unguarded would IndexError
    # *inside the handler* for a hypothetical future validator that raises
    # RequestValidationError([]) -- turning a clean 422 into a bare 500.
    # Exercise the registered handler directly since there's no way to
    # trigger a genuinely empty errors() list through real request validation.
    from fastapi.exceptions import RequestValidationError

    app = _client(_FakeSTT()).app
    handler = app.exception_handlers[RequestValidationError]
    response = await handler(None, RequestValidationError([]))
    assert response.status_code == 422


def test_models_lists_the_running_engine():
    r = _client(_FakeSTT()).get("/v1/models", headers=_AUTH)
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["whisper_local"]


def test_health_needs_no_auth():
    r = _client(_FakeSTT()).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "kind": "stt", "engine": "whisper_local"}


def test_stt_container_does_not_expose_speech():
    # Kind-based mounting: this container has no TTS provider loaded at all.
    assert _client(_FakeSTT()).post("/v1/audio/speech", headers=_AUTH, json={"input": "hi"}).status_code == 404
