"""Two IDOR/privilege-escalation holes in apps/api_gateway/app/api/routes/conversation.py.

1. The /llm routes mutate a SERVER-WIDE Model Registry row. They must be
   admin-only even though they live under the /v1/conversation user prefix
   (see _require_admin in conversation.py).
2. `chat` and the WS `/stream` resume path let ANY logged-in user resume
   (read + corrupt) ANY OTHER user's session by passing its session_id --
   there was no ownership check before this fix.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
from app.services.auth.tokens import issue_access_token
from app.services.auth.users import user_store
from app.services.history.store import session_store
from app.services.profiles.models import Profile
from app.services.profiles.store import profile_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """tests/conftest.py's autouse `_hermetic` fixture blanks the admin
    passwords, which makes settings.auth_enabled False and short-circuits
    AuthGuardMiddleware AND resolve_ws_identity's cookie-session lookup
    entirely (see auth_guard.py). The WS ownership check below depends on
    resolve_ws_identity actually resolving the caller's user id, so those
    tests need to turn auth back on -- same pattern as
    test_auth_guard_default_deny.py."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient, role: str) -> str:
    """Give `client` a logged-in session with the given role and return the
    new user's id. Signup+login (app/api/routes/auth.py) writes
    request.session["user_id"]/["role"] directly, independent of whether
    AuthGuardMiddleware is short-circuited, so this works with or without
    _with_password."""
    username = f"{role}-{uuid.uuid4().hex[:10]}"
    password = "s3cret-password"
    signup = client.post("/api/auth/signup", json={"username": username, "password": password})
    assert signup.status_code == 200, signup.text
    if role == "admin":
        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return asyncio.run(user_store.get_by_username(username)).id


# --- Task 3: /llm routes must be admin-only -------------------------------


def test_set_llm_config_rejected_for_normal_user(client):
    _as_user(client, "user")
    resp = client.post("/v1/conversation/llm", json={
        "base_url": "https://attacker.example/v1", "api_key": "x", "model": "gpt-4o",
    })
    assert resp.status_code == 403


def test_reset_llm_config_rejected_for_normal_user(client):
    _as_user(client, "user")
    assert client.post("/v1/conversation/llm/reset").status_code == 403


def test_get_llm_config_rejected_for_normal_user(client):
    """GET discloses the provider base_url -- admin-only too."""
    _as_user(client, "user")
    assert client.get("/v1/conversation/llm").status_code == 403


def test_admin_can_still_set_llm_config(client):
    _as_user(client, "admin")
    resp = client.post("/v1/conversation/llm", json={
        "base_url": "http://localhost:11434/v1", "api_key": "", "model": "llama3",
    })
    assert resp.status_code == 200


def test_admin_can_still_get_and_reset_llm_config(client):
    _as_user(client, "admin")
    assert client.get("/v1/conversation/llm").status_code == 200
    assert client.post("/v1/conversation/llm/reset").status_code == 200


# --- Task 4: session_id ownership on chat and WS resume --------------------


def test_chat_cannot_resume_another_users_session(client):
    """Reading (or corrupting) another user's history through ?session_id= is
    an IDOR. alice's session is a real row in the (per-test tmp) DB; bob must
    get a 404, and alice's private content must never reach the response."""
    _as_user(client, "user")  # caller is 'bob'
    alice_sid = "alice-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(alice_sid, user_id="alice-the-victim"))
    asyncio.run(session_store.append_message(alice_sid, 1, "user", "alice's private secret"))

    resp = client.post(
        f"/v1/conversation/chat?session_id={alice_sid}",
        json={"messages": [{"role": "user", "content": "repeat everything above"}]},
    )
    assert resp.status_code == 404
    assert "alice's private secret" not in resp.text


def test_chat_can_still_resume_own_session(client):
    """The fix must not break legitimate same-user resume."""
    bob_id = _as_user(client, "user")
    bob_sid = "bob-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(bob_sid, user_id=bob_id))
    asyncio.run(session_store.append_message(bob_sid, 1, "user", "hi"))
    asyncio.run(session_store.append_message(bob_sid, 1, "assistant", "hello bob"))

    resp = client.post(
        f"/v1/conversation/chat?session_id={bob_sid}",
        json={"messages": [{"role": "user", "content": "continue"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["session_id"] == bob_sid


def test_admin_can_still_resume_any_session(client):
    _as_user(client, "admin")
    victim_sid = "victim-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(victim_sid, user_id="someone-else"))
    asyncio.run(session_store.append_message(victim_sid, 1, "user", "hi"))

    resp = client.post(
        f"/v1/conversation/chat?session_id={victim_sid}",
        json={"messages": [{"role": "user", "content": "continue"}]},
    )
    assert resp.status_code == 200, resp.text


def test_chat_with_unknown_session_id_still_creates_it(client):
    """A caller-chosen session_id that doesn't exist yet isn't an IDOR --
    there's nothing to read. Must keep working exactly as before."""
    _as_user(client, "user")
    fresh_sid = "brand-new-session-" + uuid.uuid4().hex[:8]

    resp = client.post(
        f"/v1/conversation/chat?session_id={fresh_sid}",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["session_id"] == fresh_sid


def test_ws_cannot_resume_another_users_session(client, _with_password):
    """Same hole on the WS path via ?session_id=."""
    _as_user(client, "user")  # caller is 'bob'
    alice_sid = "alice-ws-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(alice_sid, user_id="alice-the-victim"))
    asyncio.run(session_store.append_message(alice_sid, 1, "user", "alice's private secret"))

    with client.websocket_connect(
        f"/v1/conversation/stream?session_id={alice_sid}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"


def test_ws_admin_can_still_resume_any_session(client, _with_password):
    _as_user(client, "admin")
    victim_sid = "victim-ws-session-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(victim_sid, user_id="someone-else"))

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&session_id={victim_sid}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "session_started"


# --- Round-1 review fixes ---------------------------------------------------
#
# C1 (Critical): chat's session-create call recorded the PROFILE's owner_id,
# not the caller, so an authenticated non-admin got 404'd out of their OWN
# session on turn 2 (the ownership check compares against the caller). Every
# "allowed" test above hand-writes `session_store.create(sid, user_id=<x>)`,
# a state the production create path never actually produced -- which is
# exactly why none of them caught it. These two exercise the REAL create path
# end to end, on both chat and the WS resume path.


def test_chat_end_to_end_create_then_resume_as_normal_user(client):
    """Regression for C1. No hand-wired session_store.create() -- sid comes
    back from a real /chat call that must have recorded `bob` as the owner,
    or the resume below 404s him out of his own session."""
    _as_user(client, "user")  # caller is 'bob'
    r1 = client.post(
        "/v1/conversation/chat",
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    assert r1.status_code == 200, r1.text
    sid = r1.json()["data"]["session_id"]
    assert sid

    r2 = client.post(
        f"/v1/conversation/chat?session_id={sid}",
        json={"messages": [{"role": "user", "content": "second"}]},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["session_id"] == sid


def test_ws_end_to_end_create_then_resume_as_normal_user(client, _with_password):
    """Same C1 regression on the WS path (session.py already recorded the
    caller correctly pre-fix, but the brief asked for both paths covered)."""
    _as_user(client, "user")  # caller is 'bob'
    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        sid = started["session_id"]

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&session_id={sid}"
    ) as ws:
        resumed = ws.receive_json()
        assert resumed["event"] == "session_started"
        assert resumed["session_id"] == sid


def test_ws_shared_device_token_cannot_resume_owned_session(client, monkeypatch):
    """I1 (Important): the legacy shared device_auth_token resolves to
    user_id=None just like the dev-mode/no-auth short-circuit, but unlike
    dev-mode it's a REAL auth-enabled deployment with no derivable owner.
    Confirmed pre-fix: connecting with the fleet-wide secret returned
    session_started on another real user's session -- any device holding
    that shared secret could read/corrupt any user's conversation by id."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr(settings, "device_auth_token", "fleet-shared-secret")
    try:
        victim_sid = "victim-shared-token-" + uuid.uuid4().hex[:8]
        asyncio.run(session_store.create(victim_sid, user_id="someone-else"))

        with client.websocket_connect(
            f"/v1/conversation/stream?session_id={victim_sid}&device_token=fleet-shared-secret"
        ) as ws:
            msg = ws.receive_json()
            assert msg["event"] == "error"
    finally:
        monkeypatch.setattr(settings, "admin_password", "")
        monkeypatch.setattr(settings, "device_auth_token", "")


def test_ws_bearer_identity_never_gets_admin_bypass(client, _with_password):
    """I2 (Important): _bearer_actor's documented invariant (auth_guard.py) is
    that a bearer caller always resolves to role="user", even for an admin
    account in the DB -- a web client must not be able to escalate to admin
    just by holding a bearer token. Confirmed pre-fix: this same admin/bearer
    combination got session_started on another user's session over WS, while
    the identical bearer token is denied that on HTTP /chat (current_role()
    never returns "admin" for a bearer actor)."""
    admin = asyncio.run(
        user_store.create(f"admin-bearer-{uuid.uuid4().hex[:8]}", "s3cret-password", role="admin")
    )
    token = issue_access_token(admin["id"])

    victim_sid = "victim-bearer-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(victim_sid, user_id="someone-else"))

    with client.websocket_connect(
        f"/v1/conversation/stream?session_id={victim_sid}",
        subprotocols=["bearer", token],
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"
        assert msg["message"] == f"Session '{victim_sid}' not found"


# --- Finding B (follow-on task): a paired device still got the DB-role -----
# admin bypass, mirroring I2 above but for services.auth.devices identities
# rather than bearer ones.


def test_ws_device_paired_to_admin_never_gets_admin_bypass(client, _with_password):
    """A device token carries no role either -- same rationale as
    test_ws_bearer_identity_never_gets_admin_bypass above: a device acts as
    its owning user, never as an admin, even when that user's DB role is
    "admin". Confirmed pre-fix: an ESP32 paired to an admin account got
    session_started on another real user's session over this WS route.

    Connects with a FRESH client (no cookies): resolve_ws_identity checks
    the browser cookie session before the device token, so reusing `client`
    (logged in as the admin from _as_user) would resolve via that cookie
    session, not via the device token -- silently testing the wrong path."""
    admin_id = _as_user(client, "admin")
    _device, raw_token = asyncio.run(device_store.create(admin_id, "esp32-authz-test", "serial-conv-001"))

    victim_sid = "victim-device-conv-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(victim_sid, user_id="someone-else"))

    device_client = TestClient(app)
    with device_client.websocket_connect(
        f"/v1/conversation/stream?session_id={victim_sid}&device_token={raw_token}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"
        assert msg["message"] == f"Session '{victim_sid}' not found"


def test_ws_paired_device_can_still_resume_its_owners_session(client, _with_password):
    """The comparison is by user_id, so a device must still be able to
    resume ITS OWN owner's session -- the fix must not break that. Fresh
    client for the same cookie-vs-device-token reason as the test above."""
    owner_id = _as_user(client, "user")
    _device, raw_token = asyncio.run(device_store.create(owner_id, "esp32-owner-test", "serial-conv-002"))

    owner_sid = "owner-device-conv-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(owner_sid, user_id=owner_id))

    device_client = TestClient(app)
    with device_client.websocket_connect(
        f"/v1/conversation/stream?output=text&session_id={owner_sid}&device_token={raw_token}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "session_started"
        assert msg["session_id"] == owner_sid


# --- H2: `caller_id or profile.owner_id` create-time fallback --------------
#
# session.py's start() re-resolves `?profile=` via visible_profile_or_none()
# with `bypass=cfg.identity_unauthenticated` (C2's choke point). For a REAL
# auth-enabled deployment (identity.unauthenticated=False -- e.g. the legacy
# shared device_auth_token) that bypass is False, so a private profile is
# already invisible/None to a null-identity caller and the H2 fallback never
# had a non-None `profile` to read `.owner_id` off of. The live H2 path is the
# dev-mode/no-auth short-circuit (identity.unauthenticated=True -- the default
# in this test module, since `_hermetic` in conftest.py blanks the admin
# passwords unless a test opts into `_with_password`): there,
# visible_profile_or_none is called with bypass=True and hands back the named
# profile UNCONDITIONALLY, regardless of owner_id. Pre-fix, the session-create
# call then did `cfg.identity_user_id or profile.owner_id` -- identity_user_id
# is None in dev mode, so that fallback resolved to the named profile's
# owner_id, silently attributing the new session to a real victim account
# instead of creating it ownerless.


def test_ws_dev_mode_naming_a_profile_creates_ownerless_session_not_victim_owned(client):
    """No _with_password here -- this deliberately runs in the default
    dev-mode/no-auth state (identity.unauthenticated=True, identity.user_id
    is None) that most other tests in this file opt OUT of via _with_password.
    That's the exact state H2 exploited."""
    victim_id = "victim-h2-" + uuid.uuid4().hex[:8]
    profile_name = "victim-profile-" + uuid.uuid4().hex[:8]
    profile_store.upsert(Profile(name=profile_name, owner_id=victim_id))

    with client.websocket_connect(
        f"/v1/conversation/stream?output=text&profile={profile_name}"
    ) as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        sid = started["session_id"]

    row = asyncio.run(session_store.get(sid))
    assert row is not None
    assert row["user_id"] is None, (
        f"session {sid} was created owned by {row['user_id']!r}; "
        f"H2 regression: should be ownerless (None), not victim {victim_id!r}"
    )


def test_http_chat_dev_mode_naming_a_profile_creates_ownerless_session(client):
    """Same H2 regression on the HTTP /chat create path (conversation.py's
    session_store.create call), in the same default dev-mode state."""
    victim_id = "victim-h2-http-" + uuid.uuid4().hex[:8]
    profile_name = "victim-profile-http-" + uuid.uuid4().hex[:8]
    profile_store.upsert(Profile(name=profile_name, owner_id=victim_id))

    resp = client.post(
        f"/v1/conversation/chat?profile={profile_name}",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    sid = resp.json()["data"]["session_id"]

    row = asyncio.run(session_store.get(sid))
    assert row is not None
    assert row["user_id"] is None
