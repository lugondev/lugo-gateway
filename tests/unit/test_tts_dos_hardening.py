"""Regression tests for H3 (event-loop DoS via synchronous ref-audio read +
unbounded upload) and M5 (unbounded TTSRequest.text) -- see
docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md and
.superpowers/sdd/2026-07-29-authz-round2/task-6-brief.md.
"""

import asyncio
import inspect

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.tts import TTSRequest
from app.services.model_registry.store import ModelRegistryStore
from app.services.tts.profile_store import TtsProfileStore


# ------------------------------------------------------------- H3: off-loop read


def test_render_wav_source_offloads_ref_audio_read_to_a_thread():
    """Structural check: http_tts_provider._render_wav must route the
    reference-audio read + base64 encode through asyncio.to_thread rather than
    calling Path.read_bytes()/base64.b64encode() directly on the event loop.

    A structural assertion (rather than a heartbeat-tick timing test) is
    enough here: the fix is a mechanical "wrap the blocking call" change, and
    the source shape is what actually prevents the freeze -- a timing test
    would be flakier and test the same thing indirectly. The other five
    providers (e.g. vieneu_provider._render_wav) are already covered by their
    own tests using the same to_thread idiom, so this pins http_tts_provider
    to match them.
    """
    from app.services.tts.providers import http_tts_provider

    source = inspect.getsource(http_tts_provider.HttpTtsProvider._render_wav)
    assert "asyncio.to_thread" in source
    assert "Path(payload.ref_audio_path).read_bytes()" not in source
    assert "base64.b64encode" not in source  # moved into _read_ref_audio_base64

    # And the helper it delegates to really does the blocking work, so the
    # to_thread call above isn't offloading a no-op.
    helper_source = inspect.getsource(http_tts_provider._read_ref_audio_base64)
    assert "read_bytes()" in helper_source
    assert "base64.b64encode" in helper_source


@pytest.mark.asyncio
async def test_blocking_ref_audio_read_does_not_stall_a_concurrent_coroutine(
    tmp_path, monkeypatch
):
    """Behavioral companion to the structural test above: with the read
    offloaded, a concurrent coroutine keeps making progress (heartbeat ticks)
    while a slow "disk read" is in flight -- the same signal the audit's H3
    finding used to prove the freeze (0 ticks) before this fix."""
    import httpx

    from app.services.tts.providers.http_tts_provider import HttpTtsProvider

    entry = {
        "id": "t1", "kind": "tts", "engine": "http_tts", "model_id": "vieneu",
        "label": "local box", "enabled": True, "stage": "stable",
        "api_key": "t0ken", "base_url": "http://tts-service:8100/v1", "config": {},
    }

    slow_path = tmp_path / "slow_ref.wav"
    real_bytes = b"RIFF....WAVEfmt "
    slow_path.write_bytes(real_bytes)

    # Make the "disk read" artificially slow so a stalled event loop is
    # observable within a normal test timeout.
    from app.services.tts.providers import http_tts_provider as mod

    real_helper = mod._read_ref_audio_base64

    def slow_helper(path: str) -> str:
        import time

        time.sleep(0.3)  # blocking sleep -- simulates a slow synchronous read
        return real_helper(path)

    monkeypatch.setattr(mod, "_read_ref_audio_base64", slow_helper)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"RIFFxxxxWAVEdata")

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )

    from app.services.artifacts import ArtifactStore

    fresh_store = ArtifactStore(str(tmp_path))
    monkeypatch.setattr("app.schemas.tts.artifact_store", fresh_store)

    provider = HttpTtsProvider(entry=entry)
    payload = TTSRequest(
        text="hi", engine="http_tts", ref_audio_path=str(slow_path), ref_text="ref"
    )

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.02)
            ticks += 1

    await asyncio.gather(provider.render_wav(payload), heartbeat())

    # If the read blocked the loop, the heartbeat coroutine would get almost
    # no chance to run during the 0.3s sleep -- with to_thread, it should tick
    # close to its full budget.
    assert ticks >= 10


# ------------------------------------------------------------- H3: upload cap


@pytest.fixture(autouse=True)
def _catalog_engines():
    store = ModelRegistryStore()
    asyncio.run(store.create("tts", "vieneu", "vieneu", "VieNeu"))


@pytest.fixture(autouse=True)
def _clean_profile_store(tmp_path, monkeypatch):
    fresh = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr("app.api.routes.tts_profiles.tts_profile_store", fresh)


@pytest.fixture
def client():
    return TestClient(app)


def test_reference_audio_upload_within_limit_succeeds(client, tmp_path, monkeypatch):
    from app.services.artifacts import ArtifactStore

    fresh_store = ArtifactStore(str(tmp_path))
    monkeypatch.setattr("app.api.routes.tts.artifact_store", fresh_store)

    small = b"RIFF....WAVEfmt " * 100  # tiny, well under the cap
    resp = client.post(
        "/v1/tts/reference-audio",
        files={"audio": ("ref.wav", small, "audio/wav")},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_reference_audio_upload_over_limit_is_rejected(client, tmp_path, monkeypatch):
    """Handler-level backstop: with the ASGI-layer cap raised out of the way
    (a tiny body never trips the real 10MB middleware cap below), the
    in-handler chunked-read counter is still the thing that catches an
    oversized body -- defense in depth, see
    test_upload_size_limit_middleware.py for the ASGI-layer enforcement this
    now primarily relies on."""
    from app.api.routes import tts as tts_route
    from app.services.artifacts import ArtifactStore

    fresh_store = ArtifactStore(str(tmp_path))
    monkeypatch.setattr("app.api.routes.tts.artifact_store", fresh_store)
    # Shrink the cap for the test instead of uploading a real 10MB+ body.
    monkeypatch.setattr(tts_route, "_MAX_REFERENCE_AUDIO_BYTES", 1024)

    oversized = b"x" * (1024 * 4)
    resp = client.post(
        "/v1/tts/reference-audio",
        files={"audio": ("ref.wav", oversized, "audio/wav")},
    )
    assert resp.status_code == 413


def test_reference_audio_upload_over_real_cap_is_rejected_end_to_end(client):
    """End-to-end regression for the fix-round-1 finding: FastAPI's
    `UploadFile = File(...)` makes routing call `await request.form()`
    (driving Starlette's MultiPartParser, which has no size limit on file
    parts) BEFORE `upload_reference_audio` ever runs -- so a handler-only
    chunked-read counter alone lets an oversized body be fully received and
    spooled to disk first. This hits the REAL registered app (main.py's
    UploadSizeLimitMiddleware, at the actual 10MB production cap -- not
    monkeypatched) with a body over that cap and expects 413 from the
    middleware layer, not merely from the handler's backstop counter."""
    from app.core.upload_limits import REFERENCE_AUDIO_MAX_BYTES

    oversized = b"x" * (REFERENCE_AUDIO_MAX_BYTES + (1024 * 1024))  # +1MB over
    resp = client.post(
        "/v1/tts/reference-audio",
        files={"audio": ("ref.wav", oversized, "audio/wav")},
    )
    assert resp.status_code == 413


# ------------------------------------------------------------- M5: text cap


def test_tts_request_text_at_cap_is_accepted():
    text = "a" * 10_000
    request = TTSRequest(text=text, engine="http_tts")
    assert request.text == text


def test_tts_request_normal_text_is_accepted():
    request = TTSRequest(text="xin chào thế giới", engine="http_tts")
    assert request.text == "xin chào thế giới"


def test_tts_request_text_over_cap_is_rejected():
    with pytest.raises(ValidationError):
        TTSRequest(text="a" * 10_001, engine="http_tts")
