"""The /v1/tts/stream background job must ALWAYS close its event-bus channel.

If any path skips the terminal event -- an exception before the synthesis
loop, a cancellation mid-synthesis (CancelledError isn't caught by `except
Exception`), or the untracked task being GC'd -- the channel's replay history
(up to 1000 events) leaks forever, and SSE subscribers never see end-of-stream.
"""

import asyncio

import pytest
from starlette.requests import Request

from app.api.routes import tts as tts_routes
from app.core.audio import pcm16_to_wav_bytes
from app.schemas.tts import TTSRequest, TTSResult
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service
from app.streaming.event_bus import event_bus


def _fake_request() -> Request:
    # Task 6 added a `request` param to synthesize() (for usage-metering
    # attribution) -- direct calls that bypass the ASGI app need a minimal
    # stand-in, same shape as tests/unit/test_actor.py's helper.
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "state": {}, "session": {}})


class _InstantStub(TTSProvider):
    name = "stub-tts-stream-ok"
    sample_rate = 24000

    async def synthesize(self, payload):
        return TTSResult(engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav", text=payload.text)

    async def render_audio(self, payload):
        # /v1/tts/synthesize now calls this bytes-returning seam directly
        # (see app.services.tts.base.TTSProvider.render_audio); /v1/tts/stream
        # still uses synthesize() above, unchanged.
        return pcm16_to_wav_bytes(b"\x00\x00" * 10, sample_rate=self.sample_rate), "audio/wav"


class _BlockingStub(TTSProvider):
    name = "stub-tts-stream-blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def synthesize(self, payload):
        self.started.set()
        await asyncio.Event().wait()  # blocks until the job task is cancelled


@pytest.fixture
def stubs():
    instant, blocking = _InstantStub(), _BlockingStub()
    tts_service.providers[instant.name] = instant
    tts_service.providers[blocking.name] = blocking
    yield instant, blocking
    tts_service.providers.pop(instant.name, None)
    tts_service.providers.pop(blocking.name, None)


async def _wait_for_job(job_id: str) -> None:
    for task in list(tts_routes._stream_jobs):
        if task.get_name() == f"tts-stream-{job_id}":
            await task
            return


async def test_stream_job_closes_channel_after_success(stubs):
    resp = await tts_routes.create_stream_job(
        TTSRequest(text="xin chào thế giới", engine="stub-tts-stream-ok"), _fake_request()
    )
    job_id = resp["data"]["job_id"]
    await _wait_for_job(job_id)

    assert f"job:{job_id}" in event_bus._closed


async def test_stream_job_closes_channel_when_it_crashes_before_the_loop(stubs, monkeypatch):
    def boom(text):
        raise RuntimeError("segmenter crashed")

    monkeypatch.setattr(tts_routes, "segment_text", boom)
    resp = await tts_routes.create_stream_job(
        TTSRequest(text="xin chào", engine="stub-tts-stream-ok"), _fake_request()
    )
    job_id = resp["data"]["job_id"]
    await _wait_for_job(job_id)

    # Channel must be closed even though the crash predates any publish --
    # a late subscriber gets an immediate end-of-stream, not an eternal hang.
    assert f"job:{job_id}" in event_bus._closed


async def test_stream_job_closes_channel_when_cancelled_mid_synthesis(stubs):
    _, blocking = stubs
    resp = await tts_routes.create_stream_job(
        TTSRequest(text="xin chào", engine="stub-tts-stream-blocking"), _fake_request()
    )
    job_id = resp["data"]["job_id"]
    await blocking.started.wait()

    task = next(t for t in tts_routes._stream_jobs if t.get_name() == f"tts-stream-{job_id}")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert f"job:{job_id}" in event_bus._closed


async def test_stream_job_task_reference_is_retained_while_running(stubs):
    _, blocking = stubs
    resp = await tts_routes.create_stream_job(
        TTSRequest(text="xin chào", engine="stub-tts-stream-blocking"), _fake_request()
    )
    job_id = resp["data"]["job_id"]
    await blocking.started.wait()

    task = next((t for t in tts_routes._stream_jobs if t.get_name() == f"tts-stream-{job_id}"), None)
    assert task is not None  # strong ref held -> the running job can't be GC'd

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task not in tts_routes._stream_jobs  # and it's released when done


async def test_synthesize_reports_wall_clock_process_seconds(stubs):
    # The response must carry how long synthesis actually took, distinct from
    # duration_seconds (the length of the produced audio). Task 7 moved this
    # from the JSON body to an X-TTS-Process-Seconds header (the endpoint now
    # returns the audio bytes directly, see routes/tts.py::synthesize).
    resp = await tts_routes.synthesize(
        TTSRequest(text="xin chào", engine="stub-tts-stream-ok"), _fake_request()
    )
    process_seconds = float(resp.headers["X-TTS-Process-Seconds"])
    assert process_seconds >= 0
