"""POST /v1/tts/stream and its SSE job channel are gone -- synthesized audio is
never persisted, so there is nothing for a URL-emitting job to hand back. See
docs/superpowers/specs/2026-07-29-drop-audio-artifacts-design.md."""
from fastapi.testclient import TestClient

from app.main import app


def test_stream_job_endpoints_are_gone():
    client = TestClient(app)
    assert client.post("/v1/tts/stream", json={"text": "xin chao"}).status_code == 404
    assert client.get("/v1/events/jobs/anything").status_code == 404
