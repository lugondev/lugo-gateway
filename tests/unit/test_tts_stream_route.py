"""The /v1/tts/stream background job must ALWAYS close its event-bus channel.

If any path skips the terminal event -- an exception before the synthesis
loop, a cancellation mid-synthesis (CancelledError isn't caught by `except
Exception`), or the untracked task being GC'd -- the channel's replay history
(up to 1000 events) leaks forever, and SSE subscribers never see end-of-stream.
"""

import asyncio

import pytest

from app.api.routes import tts as tts_routes
from app.schemas.tts import TTSRequest, TTSResult
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service
from app.streaming.event_bus import event_bus


class _InstantStub(TTSProvider):
    name = "stub-tts-stream-ok"

    async def synthesize(self, payload):
        return TTSResult(engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav", text=payload.text)


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
        TTSRequest(text="xin chào thế giới", engine="stub-tts-stream-ok")
    )
    job_id = resp["data"]["job_id"]
    await _wait_for_job(job_id)

    assert f"job:{job_id}" in event_bus._closed


async def test_stream_job_closes_channel_when_it_crashes_before_the_loop(stubs, monkeypatch):
    def boom(text):
        raise RuntimeError("segmenter crashed")

    monkeypatch.setattr(tts_routes, "segment_text", boom)
    resp = await tts_routes.create_stream_job(
        TTSRequest(text="xin chào", engine="stub-tts-stream-ok")
    )
    job_id = resp["data"]["job_id"]
    await _wait_for_job(job_id)

    # Channel must be closed even though the crash predates any publish --
    # a late subscriber gets an immediate end-of-stream, not an eternal hang.
    assert f"job:{job_id}" in event_bus._closed


async def test_stream_job_closes_channel_when_cancelled_mid_synthesis(stubs):
    _, blocking = stubs
    resp = await tts_routes.create_stream_job(
        TTSRequest(text="xin chào", engine="stub-tts-stream-blocking")
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
        TTSRequest(text="xin chào", engine="stub-tts-stream-blocking")
    )
    job_id = resp["data"]["job_id"]
    await blocking.started.wait()

    task = next((t for t in tts_routes._stream_jobs if t.get_name() == f"tts-stream-{job_id}"), None)
    assert task is not None  # strong ref held -> the running job can't be GC'd

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task not in tts_routes._stream_jobs  # and it's released when done
