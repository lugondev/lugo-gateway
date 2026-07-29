import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.routes.sessions import _scope_user_id
from app.services.history.store import session_store
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
async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    # Job ownership is recorded by tts.py's create_stream_job at job creation
    # time (there is no persistent job store, so this is the only record of
    # who may listen). An unrecorded job_id (evicted from the bounded owner
    # map, or simply never created) is treated as not-owned for a non-admin --
    # fail closed rather than leaking a channel nobody can prove ownership of.
    from app.api.routes.tts import get_job_owner

    scope = _scope_user_id(request)
    if scope is not None and get_job_owner(job_id) != scope:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return StreamingResponse(_sse_generator(f"job:{job_id}"), media_type="text/event-stream")


@router.get("/sessions/{session_id}")
async def stream_session_events(session_id: str, request: Request) -> StreamingResponse:
    # Same ownership rule sessions.py's get_session enforces: 404, not 403, so
    # a non-owner can't distinguish "not yours" from "doesn't exist".
    sess = await session_store.get(session_id)
    scope = _scope_user_id(request)
    if not sess or (scope is not None and sess.get("user_id") != scope):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return StreamingResponse(_sse_generator(f"session:{session_id}"), media_type="text/event-stream")
