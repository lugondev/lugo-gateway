import asyncio

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.streaming.event_bus import event_bus

router = APIRouter(prefix="/v1/events", tags=["events"])


async def _sse_generator(channel: str):
    queue = event_bus.subscribe(channel)
    try:
        while True:
            event = await queue.get()
            if event is None:
                # Terminal sentinel: channel closed, stop the stream cleanly.
                break
            yield f"event: {event.event_type}\n"
            yield f"data: {event.model_dump_json()}\n\n"
    except asyncio.CancelledError:
        raise
    finally:
        event_bus.unsubscribe(channel, queue)


@router.get("/jobs/{job_id}")
async def stream_job_events(job_id: str) -> StreamingResponse:
    return StreamingResponse(_sse_generator(f"job:{job_id}"), media_type="text/event-stream")


@router.get("/sessions/{session_id}")
async def stream_session_events(session_id: str) -> StreamingResponse:
    return StreamingResponse(_sse_generator(f"session:{session_id}"), media_type="text/event-stream")
