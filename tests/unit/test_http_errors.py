import httpx
import pytest

from app.services.http_errors import translate_httpx_error


def _status_error(status_code: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://example.test/v1/audio/transcriptions")
    response = httpx.Response(status_code, text=body, request=request)
    return httpx.HTTPStatusError(f"status {status_code}", request=request, response=response)


def test_status_error_names_the_provider_status_and_body():
    exc = _status_error(401, "invalid bearer token")
    err = translate_httpx_error("openai_stt", exc)
    assert isinstance(err, RuntimeError)
    assert str(err) == "openai_stt returned HTTP 401: invalid bearer token"


def test_status_error_truncates_the_body_to_200_chars():
    exc = _status_error(502, "x" * 500)
    err = translate_httpx_error("openai_tts", exc)
    assert str(err) == f"openai_tts returned HTTP 502: {'x' * 200}"


def test_generic_http_error_names_the_provider_and_the_exception():
    request = httpx.Request("POST", "http://example.test/v1/audio/speech")
    exc = httpx.ConnectTimeout("timed out", request=request)
    err = translate_httpx_error("openrouter", exc)
    assert isinstance(err, RuntimeError)
    assert str(err) == f"openrouter request failed: {exc}"


@pytest.mark.parametrize("name", ["openai_stt", "openai_tts", "remote_whisper", "openrouter"])
def test_works_for_every_provider_name(name):
    exc = _status_error(500, "boom")
    assert str(translate_httpx_error(name, exc)) == f"{name} returned HTTP 500: boom"
