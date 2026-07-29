"""Unit tests for app.core.upload_size_limit.UploadSizeLimitMiddleware.

These drive the middleware directly at the ASGI level (fake scope/receive/
send, a stub downstream app) instead of through the real FastAPI app, so
they can precisely assert what fix-round-1's review flagged: the cap must
trip BEFORE the request body is fully received/spooled, not merely before
the route handler starts running. tests/unit/tts/test_tts_dos_hardening.py adds
one end-to-end check (a real oversized upload against the real app) on top
of these.
"""

import pytest

from app.core.upload_size_limit import UploadSizeLimitMiddleware


def _scope(
    path: str = "/v1/tts/reference-audio",
    content_length: int | None = None,
    root_path: str = "",
) -> dict:
    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {"type": "http", "path": path, "root_path": root_path, "headers": headers}


class _RecordingSend:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )


@pytest.mark.asyncio
async def test_content_length_over_cap_is_rejected_without_ever_calling_receive():
    """The fast path: a client honest about an oversized Content-Length is
    rejected before the middleware reads a single byte off the wire, so the
    downstream app (routing/multipart parsing) never even starts."""
    receive_calls = 0

    async def receive() -> dict:
        nonlocal receive_calls
        receive_calls += 1
        raise AssertionError("receive() must not be called for an over-cap Content-Length")

    app_calls = 0

    async def downstream_app(scope, receive, send):
        nonlocal app_calls
        app_calls += 1

    middleware = UploadSizeLimitMiddleware(
        downstream_app, paths={"/v1/tts/reference-audio"}, max_bytes=1024
    )
    send = _RecordingSend()
    await middleware(_scope(content_length=2048), receive, send)

    assert send.status == 413
    assert receive_calls == 0
    assert app_calls == 0  # downstream (routing/multipart parsing) never ran


@pytest.mark.asyncio
async def test_streamed_body_over_cap_is_rejected_before_it_is_fully_drained():
    """The slow-path check: no (or a lying) Content-Length, so the cap has to
    be enforced by counting bytes as they stream in. Simulates a client that
    sends far more than the cap in chunks -- the middleware must reject as
    soon as the running total crosses the cap, WITHOUT the downstream app
    (standing in for Starlette's MultiPartParser) ever seeing all the
    chunks. That's the exact gap fix-round-1 flagged: a handler-level
    counter that only runs after full-body multipart parsing is too late.
    """
    max_bytes = 3 * 1024 * 1024  # 3MB cap
    chunk = b"x" * (1024 * 1024)  # 1MB chunks
    total_chunks_available = 20  # 20MB available -- way more than the cap
    chunks_sent = 0

    async def receive() -> dict:
        nonlocal chunks_sent
        chunks_sent += 1
        more = chunks_sent < total_chunks_available
        return {"type": "http.request", "body": chunk, "more_body": more}

    chunks_received_by_app = 0

    async def downstream_app(scope, receive, send):
        # Stands in for Starlette's MultiPartParser: keeps pulling from
        # receive() until told there's no more body -- exactly what would
        # spool an oversized upload to disk if nothing intervened.
        nonlocal chunks_received_by_app
        while True:
            message = await receive()
            chunks_received_by_app += 1
            if not message.get("more_body"):
                break

    middleware = UploadSizeLimitMiddleware(
        downstream_app, paths={"/v1/tts/reference-audio"}, max_bytes=max_bytes
    )
    send = _RecordingSend()
    # No content-length header -- forces the streamed byte-counting path.
    await middleware(_scope(content_length=None), receive, send)

    assert send.status == 413
    # The cap (3MB) trips on the 4th 1MB chunk -- proves the downstream app
    # (and therefore any multipart parser/spooling behind it) stopped
    # receiving far short of the 20 chunks a naive full-drain would need.
    assert chunks_received_by_app < total_chunks_available
    assert chunks_received_by_app <= 4


@pytest.mark.asyncio
async def test_request_under_cap_passes_through_unmodified():
    max_bytes = 1024 * 1024
    body = b"y" * 1024  # tiny, well under the cap

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    seen = {}

    async def downstream_app(scope, receive, send):
        message = await receive()
        seen["body"] = message["body"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = UploadSizeLimitMiddleware(
        downstream_app, paths={"/v1/tts/reference-audio"}, max_bytes=max_bytes
    )
    send = _RecordingSend()
    await middleware(_scope(content_length=len(body)), receive, send)

    assert seen["body"] == body
    assert send.status == 200
    assert send.body == b"ok"


@pytest.mark.asyncio
async def test_matches_and_caps_when_root_path_prefix_is_present():
    """Regression for the round-2 finding: scope["path"] includes the
    root_path prefix (e.g. under `--root-path /gw`), but the router (and
    auth_guard.py) dispatch on get_route_path(scope), which strips it. If
    this middleware matched on scope["path"] directly, a deployment behind
    a root_path would never match self.paths and the cap would silently
    stop applying -- reopening H3. Content-Length is set over the cap so a
    correct match rejects at the fast pre-check, before receive() runs."""
    receive_calls = 0

    async def receive() -> dict:
        nonlocal receive_calls
        receive_calls += 1
        raise AssertionError("receive() must not be called for an over-cap Content-Length")

    app_calls = 0

    async def downstream_app(scope, receive, send):
        nonlocal app_calls
        app_calls += 1

    middleware = UploadSizeLimitMiddleware(
        downstream_app, paths={"/v1/tts/reference-audio"}, max_bytes=1024
    )
    send = _RecordingSend()
    scope = _scope(
        path="/gw/v1/tts/reference-audio",
        content_length=2048,
        root_path="/gw",
    )
    await middleware(scope, receive, send)

    assert send.status == 413
    assert receive_calls == 0
    assert app_calls == 0


@pytest.mark.asyncio
async def test_paths_outside_the_configured_set_are_not_touched():
    """A route this middleware wasn't scoped to (e.g. /v1/stt/transcribe)
    must pass straight through with no size enforcement -- this task only
    caps the reference-audio upload, not every upload endpoint."""

    async def receive() -> dict:
        return {"type": "http.request", "body": b"z" * (10 * 1024 * 1024), "more_body": False}

    async def downstream_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = UploadSizeLimitMiddleware(
        downstream_app, paths={"/v1/tts/reference-audio"}, max_bytes=1024
    )
    send = _RecordingSend()
    await middleware(_scope(path="/v1/stt/transcribe", content_length=10 * 1024 * 1024), receive, send)

    assert send.status == 200
