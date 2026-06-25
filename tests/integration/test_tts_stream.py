import json

import httpx
import pytest

from app.main import app


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _collect_sse(client, url, max_events=50):
    events = []
    async with client.stream("GET", url) as response:
        assert response.status_code == 200
        current_type = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                current_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                events.append((current_type, data))
                if current_type == "done" or len(events) >= max_events:
                    break
    return events


async def test_tts_stream_emits_chunks_and_real_audio(client):
    async with client:
        resp = await client.post(
            "/v1/tts/stream",
            json={"text": "Hello world. This is a second sentence.", "engine": "omnivoice"},
        )
        assert resp.status_code == 200
        job_id = resp.json()["data"]["job_id"]

        events = await _collect_sse(client, f"/v1/events/jobs/{job_id}")
        types = [t for t, _ in events]

        assert types[0] == "queued"  # replay guarantees we see the first event
        assert "audio_chunk" in types
        assert types[-1] == "done"

        chunks = [d["payload"] for t, d in events if t == "audio_chunk"]
        assert len(chunks) == 2  # two sentences -> two chunks
        for chunk in chunks:
            assert chunk["audio_url"].startswith("/artifacts/")

        # The generated audio artifact is actually fetchable and is a WAV.
        audio = await client.get(chunks[0]["audio_url"])
        assert audio.status_code == 200
        assert audio.content[:4] == b"RIFF"


async def test_unknown_tts_engine_returns_400(client):
    async with client:
        resp = await client.post(
            "/v1/tts/synthesize", json={"text": "hi", "engine": "nope"}
        )
        assert resp.status_code == 400
        assert resp.json()["success"] is False


async def test_synthesize_returns_audio_url(client):
    async with client:
        resp = await client.post(
            "/v1/tts/synthesize", json={"text": "hello there", "engine": "omnivoice"}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["audio_url"].startswith("/artifacts/")
        assert data["sample_rate"] == 24000
        assert data["duration_seconds"] > 0
