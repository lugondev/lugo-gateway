"""Adversarial regression for H5: livehost WS + 3 HTTP control routes IDOR.

Before this fix:
- The 3 HTTP control routes (connect/disconnect/status) did only
  `livehost_registry.get(session_id)` with no owner check -- any logged-in
  user could drive/stop/inspect another user's live TikTok session by id.
- The WS `/v1/livehost/stream?session_id=` route took the caller-supplied
  id straight into `livehost_registry.register()`, which unconditionally
  OVERWRITES any existing entry for that key -- so a second user could
  hijack (overwrite the ingestor/scheduler of) another user's already-live
  session, and `unregister()` on close would then orphan it.

Fix: `LivehostSession` now carries the registering caller's `user_id`; the 3
HTTP routes 404 unless the caller owns the session (admins unscoped, mirrors
sessions.py's `_scope_user_id`); the WS route calls `ws_session_owner_denied`
before `register()`, same as conversation.py/lugo.py.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.livehost.ingestor import TikTokLiveIngestor
from app.services.livehost.registry import LivehostSession, livehost_registry
from app.services.livehost.scheduler import EventScheduler


def _dummy_client_factory(unique_id: str):
    raise AssertionError("ingestor.start() should never be invoked by these tests")


def _register_session(session_id: str, user_id: str | None) -> LivehostSession:
    session = LivehostSession(
        scheduler=EventScheduler(),
        ingestor=TikTokLiveIngestor(client_factory=_dummy_client_factory, queue=asyncio.Queue()),
        user_id=user_id,
    )
    livehost_registry.register(session_id, session)
    return session


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    # In-memory module-level singleton -- don't leak sessions across tests.
    livehost_registry._sessions.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def client_a():
    return TestClient(app)


@pytest.fixture
def client_b():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """Same rationale as test_conversation_authz.py's fixture of the same
    name: the autouse `_hermetic` fixture blanks the admin password, which
    makes auth a no-op and every caller resolve as an unscoped admin. The
    ownership checks under test need auth actually enabled."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient, role: str = "user") -> str:
    username = f"{role}-{uuid.uuid4().hex[:10]}"
    password = "s3cret-password"
    signup = client.post("/api/auth/signup", json={"username": username, "password": password})
    assert signup.status_code == 200, signup.text
    if role == "admin":
        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    from app.services.auth.users import user_store as _user_store

    return asyncio.run(_user_store.get_by_username(username)).id


# ---------------------------------------------------------------------------
# HTTP control routes: connect / disconnect / status.
# ---------------------------------------------------------------------------


def test_status_404_for_non_owner(client, _with_password):
    alice_id = _as_user(client, "user")  # noqa: F841 -- documents whose session this is
    sid = "alice-status-" + uuid.uuid4().hex[:8]
    _register_session(sid, alice_id)

    _as_user(client, "user")  # now logged in as bob (same client, fresh session)
    resp = client.get(f"/v1/livehost/{sid}/status")
    assert resp.status_code == 404


def test_connect_404_for_non_owner(client, _with_password):
    alice_id = _as_user(client, "user")
    sid = "alice-connect-" + uuid.uuid4().hex[:8]
    _register_session(sid, alice_id)

    _as_user(client, "user")  # bob
    resp = client.post(f"/v1/livehost/{sid}/connect", json={"unique_id": "victim_stream"})
    assert resp.status_code == 404


def test_disconnect_404_for_non_owner(client, _with_password):
    alice_id = _as_user(client, "user")
    sid = "alice-disconnect-" + uuid.uuid4().hex[:8]
    _register_session(sid, alice_id)

    _as_user(client, "user")  # bob
    resp = client.post(f"/v1/livehost/{sid}/disconnect")
    assert resp.status_code == 404


def test_owner_can_still_use_own_control_routes(client, _with_password):
    alice_id = _as_user(client, "user")
    sid = "alice-owns-" + uuid.uuid4().hex[:8]
    _register_session(sid, alice_id)

    resp = client.get(f"/v1/livehost/{sid}/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["state"] == "idle"


def test_admin_can_use_any_users_control_routes(client, _with_password):
    victim_id = "victim-user-id"
    sid = "victim-admin-access-" + uuid.uuid4().hex[:8]
    _register_session(sid, victim_id)

    _as_user(client, "admin")
    resp = client.get(f"/v1/livehost/{sid}/status")
    assert resp.status_code == 200, resp.text


def test_unknown_session_id_still_404s(client, _with_password):
    _as_user(client, "user")
    resp = client.get(f"/v1/livehost/{uuid.uuid4().hex}/status")
    assert resp.status_code == 404


def test_ownerless_session_not_reachable_by_an_unrelated_logged_in_user(client, _with_password):
    """A session registered with no owner (unauthenticated/dev-mode/fleet-
    token WS caller, user_id=None) is NOT an open door for every OTHER
    logged-in user -- mirrors sessions.py's list_sessions, where a non-admin
    scope is an exact-match filter on their own id, never None. Only an
    unscoped caller (admin, or auth fully disabled) can reach it -- see
    test_admin_can_use_any_users_control_routes and
    test_ownerless_session_reachable_when_auth_disabled below."""
    sid = "ownerless-" + uuid.uuid4().hex[:8]
    _register_session(sid, None)

    _as_user(client, "user")
    resp = client.get(f"/v1/livehost/{sid}/status")
    assert resp.status_code == 404


def test_ownerless_session_reachable_when_auth_disabled(client):
    """No `_with_password` here -- the autouse `_hermetic` fixture leaves
    auth disabled, so current_role() defaults to "admin" (dev-mode, see
    app.core.actor.current_role) and _scope_user_id is unscoped, same as
    today's unauthenticated behavior for every other route in this file."""
    sid = "ownerless-dev-" + uuid.uuid4().hex[:8]
    _register_session(sid, None)

    resp = client.get(f"/v1/livehost/{sid}/status")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# WS route: session_id ownership + no registry overwrite.
# ---------------------------------------------------------------------------


def test_ws_cannot_hijack_another_users_live_session(client_a, client_b, _with_password):
    """The full end-to-end IDOR: alice's WS is live (registered in
    livehost_registry) when bob connects with the same ?session_id=. Bob
    must be denied (error + close) BEFORE register() runs, and alice's
    registry entry must be byte-for-byte the same object afterward -- not
    overwritten, not orphaned."""
    alice_id = _as_user(client_a, "user")
    bob_id = _as_user(client_b, "user")
    assert alice_id != bob_id
    sid = "hijack-target-" + uuid.uuid4().hex[:8]

    with client_a.websocket_connect(f"/v1/livehost/stream?session_id={sid}&sample_rate=16000") as ws_a:
        started = ws_a.receive_json()
        assert started["event"] == "session_started"
        assert started["session_id"] == sid

        original_session = livehost_registry.get(sid)
        assert original_session is not None
        assert original_session.user_id == alice_id

        with client_b.websocket_connect(f"/v1/livehost/stream?session_id={sid}&sample_rate=16000") as ws_b:
            msg = ws_b.receive_json()
            assert msg["event"] == "error"
            assert sid in msg["message"]

        # Alice's registry entry is untouched -- same object, same owner --
        # register() from bob's attempt never ran.
        assert livehost_registry.get(sid) is original_session
        assert livehost_registry.get(sid).user_id == alice_id

        # And bob's HTTP control routes against the same id are still denied
        # (the WS attempt didn't leave a side-door open).
        resp = client_b.get(f"/v1/livehost/{sid}/status")
        assert resp.status_code == 404

        # Alice herself can still drive her own live session.
        resp = client_a.get(f"/v1/livehost/{sid}/status")
        assert resp.status_code == 200, resp.text


def test_ws_admin_can_still_connect_with_any_session_id(client_a, client_b, _with_password):
    """Admin bypass on the WS ownership check must survive (mirrors
    conversation.py's/lugo.py's identical admin-bypass tests)."""
    alice_id = _as_user(client_a, "user")
    sid = "admin-resume-" + uuid.uuid4().hex[:8]

    with client_a.websocket_connect(f"/v1/livehost/stream?session_id={sid}&sample_rate=16000") as ws_a:
        started = ws_a.receive_json()
        assert started["event"] == "session_started"
        assert livehost_registry.get(sid).user_id == alice_id

        _as_user(client_b, "admin")
        with client_b.websocket_connect(f"/v1/livehost/stream?session_id={sid}&sample_rate=16000") as ws_b:
            msg = ws_b.receive_json()
            # Admin's own connect under the same id is allowed through (not
            # denied) -- it re-registers under the admin's own identity,
            # same overwrite-on-purpose semantics conversation.py's WS
            # resume has for an admin.
            assert msg["event"] == "session_started"
