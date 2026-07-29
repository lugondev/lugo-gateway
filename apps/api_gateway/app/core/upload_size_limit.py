"""ASGI middleware enforcing a hard body-size cap on specific routes, running
BEFORE Starlette's request routing / multipart form parsing ever touches the
request.

Why a route-level chunked read (see routes/tts.py's `upload_reference_audio`)
is not enough on its own: FastAPI's `UploadFile = File(...)` parameter makes
the routing layer call `await request.form()` -- which drives Starlette's
`MultiPartParser` -- BEFORE the route function's own body ever runs. This
version's `MultiPartParser` has no size limit on file parts (`max_part_size`
only bounds ordinary/non-file form fields), so an oversized upload is fully
received off the wire and spooled to a `SpooledTemporaryFile` (1MB in RAM,
then unbounded spillover to local disk) before a handler-level byte counter
ever gets a chance to run. That is still the resource-exhaustion H3
describes -- unbounded receive + disk I/O + wall-clock time -- regardless of
what the handler does afterward. This middleware closes that gap by capping
the request body at the ASGI layer, upstream of routing/form-parsing
entirely.

Registration (main.py): must sit somewhere inside CORSMiddleware (so a 413
from here still carries CORS headers a browser client can read) and outside
the request handler / router. Where it sits relative to SessionMiddleware /
AuthGuardMiddleware doesn't matter for correctness -- neither of those reads
or buffers the request body -- but it must run for every candidate request
regardless of auth outcome, so keep it wrapping (added after) them too.
"""

from __future__ import annotations

import logging

from starlette._utils import get_route_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class _BodyTooLarge(Exception):
    """Raised from inside the wrapped receive() once the running byte count
    crosses the cap. Caught by __call__ below -- never escapes this module."""


class UploadSizeLimitMiddleware:
    """Caps the request body for a fixed set of paths.

    Two independent checks, because either alone is bypassable:
      - A `Content-Length` pre-check: rejects immediately, before reading
        any body at all, when the client is honest about an oversized
        upload.
      - A wrapped `receive()` that counts bytes actually read off the wire
        and aborts once the running total crosses the cap, regardless of
        what `Content-Length` claimed (or if it was omitted/wrong) -- this
        is what closes the "lying/absent Content-Length" gap the H3 finding
        calls out explicitly; the header check alone cannot.
    """

    def __init__(self, app: ASGIApp, paths: set[str], max_bytes: int) -> None:
        self.app = app
        self.paths = paths
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Match on the ROUTER's dispatch path (root_path stripped), the same
        # call auth_guard.py and Starlette's own routing use -- not
        # scope["path"], which still carries the root_path prefix. Under
        # `--root-path /gw` those two diverge, and matching scope["path"]
        # would silently fall through to the unwrapped receive(), reopening
        # H3 (see module docstring). This is the twin fix to auth_guard.py's
        # Fix #1 (H1/M3), applied to the second place in this app that has
        # to classify a request by path before routing runs.
        if scope["type"] != "http" or get_route_path(scope) not in self.paths:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers") or ()}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except ValueError:
                declared = None
            if declared is not None and declared > self.max_bytes:
                await _send_413(send, self.max_bytes)
                return

        max_bytes = self.max_bytes
        total = 0

        async def guarded_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body") or b"")
                if total > max_bytes:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, guarded_receive, send)
        except _BodyTooLarge:
            # Safe to still emit a fresh response here: this middleware is
            # only ever registered for upload routes whose body is fully
            # consumed by form-parsing before any response starts -- the
            # cap trips during that parse, strictly before the route
            # handler (and therefore any http.response.start) runs.
            await _send_413(send, max_bytes)


async def _send_413(send: Send, max_bytes: int) -> None:
    body = (
        '{"success": false, "error": "upload exceeds the '
        f'{max_bytes // (1024 * 1024)}MB limit"}}'
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
