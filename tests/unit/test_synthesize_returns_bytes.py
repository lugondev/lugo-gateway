"""POST /v1/tts/synthesize returns the audio itself, not a URL to a temp file.

The artifact indirection existed only because JSON can't carry binary; nothing
persists artifact URLs (0 rows in `messages` reference /artifacts/), so the
file was pure churn and an auth-sensitive surface.
"""

import io
import wave

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.tts import TTSRequest
from app.services.tts.base import RenderingTTSProvider
from app.services.tts.service import tts_service


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\x00\x00" * 100)
    return buf.getvalue()


class _StubTTS(RenderingTTSProvider):
    name = "stub-bytes-tts"
    sample_rate = 8000

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        return _tiny_wav()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(tts_service.providers, _StubTTS.name, _StubTTS())
    return TestClient(app)


def test_response_body_is_the_wav_itself(client):
    resp = client.post("/v1/tts/synthesize", json={"text": "xin chao", "engine": _StubTTS.name})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content[:4] == b"RIFF" and resp.content[8:12] == b"WAVE"


def test_metadata_travels_in_headers(client):
    resp = client.post("/v1/tts/synthesize", json={"text": "xin chao", "engine": _StubTTS.name})
    assert resp.headers["x-tts-engine"] == _StubTTS.name
    assert int(resp.headers["x-tts-sample-rate"]) == 8000
    assert float(resp.headers["x-tts-duration-seconds"]) > 0


def test_no_artifact_file_is_written(client, monkeypatch):
    """The whole point: this path must stop creating temp files."""
    calls = {"n": 0}
    from app.services import artifacts as artifacts_mod

    def spy(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("synthesize must not write an artifact")

    monkeypatch.setattr(artifacts_mod.artifact_store, "save_wav", spy, raising=False)
    monkeypatch.setattr(artifacts_mod.artifact_store, "save_mp3", spy, raising=False)

    resp = client.post("/v1/tts/synthesize", json={"text": "xin chao", "engine": _StubTTS.name})
    assert resp.status_code == 200
    assert calls["n"] == 0


def test_metadata_headers_are_cors_exposed():
    """A cross-origin client (lugo-web-client) reads these headers; without
    expose_headers the browser hides them and the client sees null."""
    from app.core.settings import settings  # noqa: F401
    from app.main import app as the_app

    cors = next(
        m for m in the_app.user_middleware if "CORSMiddleware" in str(m.cls)
    )
    exposed = {h.lower() for h in (cors.kwargs.get("expose_headers") or [])}
    for header in ("x-tts-engine", "x-tts-sample-rate", "x-tts-duration-seconds"):
        assert header in exposed, f"{header} not exposed to cross-origin clients"
