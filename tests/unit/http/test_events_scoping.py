"""A logged-in user must not be able to subscribe to another user's live
transcript stream. Mirrors the ownership rule sessions.py already enforces.

NOTE on "allowed" (200) assertions: httpx's built-in ASGITransport runs the
whole ASGI app call to completion before it will hand back a Response object
(see httpx._transports.asgi.ASGITransport.handle_async_request -- it awaits
`self.app(...)` fully, then asserts response_complete). That's fine for an SSE
generator that eventually terminates (tests/integration/test_tts_stream.py's
job channel closes on its "done" event), but a channel nobody ever publishes
to or closes blocks forever -- `client.stream(...)` never returns, sync or
async, and the run only ends via pytest-timeout's 120s kill. So "allowed"
cases here call the route coroutine directly (same pattern already used by
tests/unit/tts/test_tts_stream_route.py) instead of going through a client and
opening the stream body.
"""

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.routes import events as events_routes
from app.api.routes import tts as tts_routes
from app.main import app


def _fake_request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}, "session": {}})


@pytest.fixture
def client():
    return TestClient(app)


def test_session_channel_404s_for_non_owner(client, monkeypatch):
    """Owner is 'alice'; caller is 'bob' -> 404, not a live stream."""
    async def fake_get(session_id):
        return {"id": session_id, "user_id": "alice"}

    monkeypatch.setattr("app.api.routes.events.session_store.get", fake_get, raising=False)
    monkeypatch.setattr("app.api.routes.events._scope_user_id", lambda request: "bob", raising=False)

    resp = client.get("/v1/events/sessions/s-alice")
    assert resp.status_code == 404


async def test_session_channel_allows_owner(monkeypatch):
    async def fake_get(session_id):
        return {"id": session_id, "user_id": "alice"}

    monkeypatch.setattr(events_routes.session_store, "get", fake_get, raising=False)
    monkeypatch.setattr(events_routes, "_scope_user_id", lambda request: "alice", raising=False)

    resp = await events_routes.stream_session_events("s-alice", _fake_request())
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200


def test_session_channel_404s_for_unknown_session(client, monkeypatch):
    async def fake_get(session_id):
        return None

    monkeypatch.setattr("app.api.routes.events.session_store.get", fake_get, raising=False)
    monkeypatch.setattr("app.api.routes.events._scope_user_id", lambda request: "bob", raising=False)

    assert client.get("/v1/events/sessions/ghost").status_code == 404


async def test_session_channel_unfiltered_for_admin_scope(monkeypatch):
    """scope is None for admins (and dev-mode/auth-disabled) -- unfiltered,
    unchanged from today's behavior, same contract as sessions.py."""
    async def fake_get(session_id):
        return {"id": session_id, "user_id": "alice"}

    monkeypatch.setattr(events_routes.session_store, "get", fake_get, raising=False)
    monkeypatch.setattr(events_routes, "_scope_user_id", lambda request: None, raising=False)

    resp = await events_routes.stream_session_events("s-alice", _fake_request())
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200


def test_job_channel_404s_for_non_owner(client, monkeypatch):
    """Job ownership is recorded at job-creation time in tts.py; a caller who
    didn't create the job must not be able to subscribe to its result stream."""
    tts_routes._record_job_owner("job-alice", "alice")
    monkeypatch.setattr("app.api.routes.events._scope_user_id", lambda request: "bob", raising=False)

    resp = client.get("/v1/events/jobs/job-alice")
    assert resp.status_code == 404


async def test_job_channel_allows_owner(monkeypatch):
    tts_routes._record_job_owner("job-alice-2", "alice")
    monkeypatch.setattr(events_routes, "_scope_user_id", lambda request: "alice", raising=False)

    resp = await events_routes.stream_job_events("job-alice-2", _fake_request())
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200


def test_job_channel_404s_for_unrecorded_job(client, monkeypatch):
    """A job_id with no recorded owner (evicted, or never created via
    create_stream_job) must fail closed for a non-admin caller rather than
    being readable by anyone who guesses/observes a job_id."""
    monkeypatch.setattr("app.api.routes.events._scope_user_id", lambda request: "bob", raising=False)

    resp = client.get("/v1/events/jobs/never-created")
    assert resp.status_code == 404


async def test_job_channel_unfiltered_for_admin_scope(monkeypatch):
    tts_routes._record_job_owner("job-alice-3", "alice")
    monkeypatch.setattr(events_routes, "_scope_user_id", lambda request: None, raising=False)

    resp = await events_routes.stream_job_events("job-alice-3", _fake_request())
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200
