import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.history.store import session_store


@pytest.fixture
def client():
    return TestClient(app)


def test_guard_noop_when_admin_password_unset(client):
    assert settings.admin_password == ""
    resp = client.get("/v1/system/status")
    assert resp.status_code != 401


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _login_as(client, username: str, password: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": password})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": password})


def test_guard_blocks_admin_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/system/status")
    assert resp.status_code == 401


def test_guard_403s_admin_route_for_regular_user(client, _with_password):
    _login_as(client, "toan", "s3cret", role="user")
    resp = client.get("/v1/system/status")
    assert resp.status_code == 403


def test_guard_allows_admin_route_for_admin(client, _with_password):
    _login_as(client, "root", "s3cret", role="admin")
    resp = client.get("/v1/system/status")
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_guard_allows_user_route_for_regular_user(client, _with_password):
    _login_as(client, "toan", "s3cret", role="user")
    resp = client.get("/v1/profiles")
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_guard_blocks_user_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/profiles")
    assert resp.status_code == 401


def test_guard_blocks_tts_profiles_route_when_logged_out(client, _with_password):
    # Regression: _USER_PREFIXES previously had a typo ("/v1/tts_profiles" instead
    # of the router's actual "/v1/tts/profiles" prefix), so this whole surface fell
    # through the middleware unauthenticated whenever auth was enabled.
    resp = client.get("/v1/tts/profiles")
    assert resp.status_code == 401


def test_guard_allows_tts_profiles_route_for_regular_user(client, _with_password):
    _login_as(client, "toan", "s3cret", role="user")
    resp = client.get("/v1/tts/profiles")
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_guard_allows_device_pairing_init_without_login(client, _with_password):
    resp = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"})
    assert resp.status_code != 401


def test_guard_blocks_pair_claim_when_logged_out(client, _with_password):
    resp = client.post("/v1/devices/pair/claim", json={"code": "000000", "name": "x"})
    assert resp.status_code == 401


def test_guard_allows_device_pairing_status_without_login(client, _with_password):
    # /v1/devices/pair/init and /v1/devices/pair/status are the only genuinely
    # unauthenticated device-side routes (the device itself has no login) --
    # see _NO_AUTH_PREFIXES. /v1/devices/pair/init already has its own test
    # above; this covers the other entry.
    resp = client.get("/v1/devices/pair/status", params={"poll_token": "does-not-exist"})
    assert resp.status_code != 401


def test_guard_blocks_stt_engines_route_when_logged_out(client, _with_password):
    # /v1/stt/engines is an STT engine listing, not a device route -- it now
    # lives in _USER_PREFIXES alongside the rest of /v1/stt and /v1/tts, so it
    # requires a logged-in session like any other user-facing surface.
    resp = client.get("/v1/stt/engines")
    assert resp.status_code == 401


def test_guard_allows_auth_routes_without_login(client, _with_password):
    resp = client.get("/api/auth/status")
    assert resp.status_code != 401


def test_guard_allows_options_preflight_without_login(client, _with_password):
    # A GENUINE CORS preflight carries BOTH Origin and
    # Access-Control-Request-Method (Fetch spec). It is answered by
    # CORSMiddleware (outside the guard) before the guard ever runs, so the
    # browser can complete the handshake before login.
    resp = client.options(
        "/v1/system/status",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code != 401


def test_guard_blocks_plain_options_without_preflight_header(client, _with_password):
    # M2: a plain OPTIONS (no preflight header) must be classified like any other
    # method, not exempted -- otherwise it enumerates the admin surface via the
    # router's auto `405/200 Allow:` with no credentials.
    resp = client.options("/v1/system/status")
    assert resp.status_code in (401, 403)


def test_guard_blocks_options_with_acrm_but_no_origin(client, _with_password):
    # The M2 re-open the reviewer found: ACRM without Origin is NOT a genuine
    # preflight -- CORSMiddleware ignores it (no Origin) and passes it through,
    # so the guard must still classify and deny it. Otherwise this single header
    # re-opens the enumeration oracle.
    resp = client.options("/v1/system/status", headers={"Access-Control-Request-Method": "GET"})
    assert resp.status_code in (401, 403)


def test_guard_enforces_when_only_bootstrap_password_set(client, monkeypatch):
    assert settings.admin_password == ""
    monkeypatch.setattr(settings, "admin_bootstrap_password", "boot-pw")
    try:
        resp = client.get("/v1/system/status")
        assert resp.status_code == 401
    finally:
        monkeypatch.setattr(settings, "admin_bootstrap_password", "")


class _FakeWebSocket:
    def __init__(
        self,
        session: dict | None = None,
        query_params: dict | None = None,
        subprotocols: list | None = None,
    ):
        self.session = session or {}
        self.query_params = query_params or {}
        self.scope = {"subprotocols": subprotocols or []}


@pytest.mark.asyncio
async def test_resolve_identity_noop_when_admin_password_unset():
    from app.core.auth_guard import resolve_ws_identity

    assert settings.admin_password == ""
    identity = await resolve_ws_identity(_FakeWebSocket())
    assert identity is not None
    assert identity.user_id is None and identity.device_id is None


@pytest.mark.asyncio
async def test_resolve_identity_from_browser_cookie_session(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    identity = await resolve_ws_identity(_FakeWebSocket(session={"user_id": user["id"]}))
    assert identity is not None
    assert identity.user_id == user["id"]
    assert identity.device_id is None
    # round-2 review: the cookie-session branch is the ONE identity source
    # that sets via_login=True -- that's what ws_session_owner_denied's
    # admin-bypass allow-list is keyed on.
    assert identity.via_login is True
    assert identity.via_bearer is False
    assert identity.via_device is False
    assert identity.via_fleet_token is False


@pytest.mark.asyncio
async def test_resolve_identity_rejects_disabled_user_cookie(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    await user_store.set_fields(user["id"], disabled=True)
    identity = await resolve_ws_identity(_FakeWebSocket(session={"user_id": user["id"]}))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_rejects_missing_cookie_and_token(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    assert await resolve_ws_identity(_FakeWebSocket()) is None


@pytest.mark.asyncio
async def test_resolve_identity_from_a_plugin_session_token(_with_password):
    """Found live: a plugin's own backend opens its upstream
    /v1/conversation/stream connection with a plugin session token (minted by
    POST /api/auth/introspect, server-to-server) in the SAME bearer
    subprotocol slot a real access token would use. This is the one thing
    Task 7's Upstream got wrong originally -- the token rode as a `?token=`
    query param, which resolve_ws_identity never reads at all."""
    from app.core.auth_guard import resolve_ws_identity
    from app.services.auth.tokens import issue_plugin_session_token

    user = await user_store.create("toan", "pw")
    token = issue_plugin_session_token(user["id"])
    identity = await resolve_ws_identity(_FakeWebSocket(subprotocols=["bearer", token]))
    assert identity is not None
    assert identity.user_id == user["id"]
    # Same guarantee a real bearer session gets: role="user" is not even a
    # field this identity carries an escalation path for, and it must be
    # excluded from ws_session_owner_denied's via_login-only admin bypass.
    assert identity.via_bearer is True
    assert identity.via_login is False


@pytest.mark.asyncio
async def test_resolve_identity_still_prefers_a_real_access_token(_with_password):
    """The fallback is additive: verify_access_token is tried first and an
    ordinary bearer WS connection must keep working unchanged."""
    from app.core.auth_guard import resolve_ws_identity
    from app.services.auth.tokens import issue_access_token

    user = await user_store.create("toan", "pw")
    token = issue_access_token(user["id"])
    identity = await resolve_ws_identity(_FakeWebSocket(subprotocols=["bearer", token]))
    assert identity is not None
    assert identity.user_id == user["id"]


@pytest.mark.asyncio
async def test_resolve_identity_rejects_a_plugin_ticket_directly(_with_password):
    """The ticket itself (audience-bound to one plugin, designed to survive a
    browser/URL) must NOT open this socket -- only the session token traded
    for it server-side may. If this ever passed, the whole point of minting a
    separate, never-browser-visible credential would be moot."""
    from app.core.auth_guard import resolve_ws_identity
    from app.services.auth.tokens import issue_plugin_token

    user = await user_store.create("toan", "pw")
    ticket = issue_plugin_token(user["id"], "livehost")
    identity = await resolve_ws_identity(_FakeWebSocket(subprotocols=["bearer", ticket]))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_rejects_an_expired_plugin_session_token(
    _with_password, monkeypatch
):
    from app.core.auth_guard import resolve_ws_identity
    from app.services.auth import tokens

    user = await user_store.create("toan", "pw")
    token = tokens.issue_plugin_session_token(user["id"])
    monkeypatch.setattr(tokens, "PLUGIN_SESSION_TTL_SECONDS", -1)
    identity = await resolve_ws_identity(_FakeWebSocket(subprotocols=["bearer", token]))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_rejects_a_plugin_session_token_for_a_disabled_user(_with_password):
    from app.core.auth_guard import resolve_ws_identity
    from app.services.auth.tokens import issue_plugin_session_token

    user = await user_store.create("toan", "pw")
    token = issue_plugin_session_token(user["id"])
    await user_store.set_fields(user["id"], disabled=True)
    identity = await resolve_ws_identity(_FakeWebSocket(subprotocols=["bearer", token]))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_from_paired_device_token(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    device, raw_token = await device_store.create(user["id"], "ESP32", "AA:BB:CC")
    identity = await resolve_ws_identity(_FakeWebSocket(query_params={"device_token": raw_token}))
    assert identity is not None
    assert identity.user_id == user["id"]
    assert identity.device_id == device["id"]
    assert identity.via_device is True
    assert identity.via_login is False


@pytest.mark.asyncio
async def test_resolve_identity_rejects_revoked_device(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    device, raw_token = await device_store.create(user["id"], "ESP32", "AA:BB:CC")
    await device_store.revoke(device["id"])
    identity = await resolve_ws_identity(_FakeWebSocket(query_params={"device_token": raw_token}))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_rejects_device_of_disabled_owner(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    device, raw_token = await device_store.create(user["id"], "ESP32", "AA:BB:CC")
    await user_store.set_fields(user["id"], disabled=True)
    identity = await resolve_ws_identity(_FakeWebSocket(query_params={"device_token": raw_token}))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_accepts_legacy_shared_device_token(_with_password, monkeypatch):
    from app.core.auth_guard import resolve_ws_identity

    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    identity = await resolve_ws_identity(
        _FakeWebSocket(query_params={"device_token": "d3vice-secret"})
    )
    assert identity is not None
    assert identity.user_id is None and identity.device_id is None
    # round-2 review, minor: gives this identity source an explicit positive
    # marker instead of it being identifiable only as "user_id is None and
    # not unauthenticated".
    assert identity.via_fleet_token is True
    assert identity.via_login is False


@pytest.mark.asyncio
async def test_resolve_identity_rejects_wrong_token(_with_password, monkeypatch):
    from app.core.auth_guard import resolve_ws_identity

    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    identity = await resolve_ws_identity(_FakeWebSocket(query_params={"device_token": "wrong"}))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_enforced_when_only_bootstrap_password_set(monkeypatch):
    from app.core.auth_guard import resolve_ws_identity

    assert settings.admin_password == ""
    monkeypatch.setattr(settings, "admin_bootstrap_password", "boot-pw")
    try:
        identity = await resolve_ws_identity(_FakeWebSocket())
        assert identity is None
    finally:
        monkeypatch.setattr(settings, "admin_bootstrap_password", "")


# --- round-2 review: ws_session_owner_denied's admin bypass is an ALLOW-list
# (via_login), not a deny-list. These pin the property that motivated the
# rewrite, plus behavioral equivalence with the pre-rewrite deny-list for the
# one case that must still work.


@pytest.mark.asyncio
async def test_ws_session_owner_denied_fails_closed_for_unclassified_identity(_with_password):
    """The whole point of the allow-list rewrite: a WsIdentity with a
    user_id but NONE of the provenance flags set -- simulating a future
    identity source resolve_ws_identity doesn't classify yet -- must NOT get
    the admin bypass, even when the underlying DB user is a real admin. This
    is exactly the property that would have caught both prior recurrences
    (bearer identities kept the bypass until round-1 review's I2; paired-
    device identities kept it until round-2 review) the moment each identity
    source was introduced, rather than needing a dedicated review pass to
    notice the deny-list wasn't extended."""
    from app.core.auth_guard import WsIdentity, ws_session_owner_denied

    admin = await user_store.create(
        f"future-source-admin-{uuid.uuid4().hex[:8]}", "pw", role="admin"
    )
    victim_sid = "victim-unclassified-" + uuid.uuid4().hex[:8]
    await session_store.create(victim_sid, user_id="someone-else")

    mystery_identity = WsIdentity(user_id=admin["id"], device_id=None)  # every flag defaults False
    assert await ws_session_owner_denied(victim_sid, mystery_identity) is True


@pytest.mark.asyncio
async def test_ws_session_owner_denied_still_bypasses_for_real_login(_with_password):
    """Behavioral-equivalence check for the rewrite: the one legitimate
    bypass source (an interactive cookie login, via_login=True) must still
    get it -- the allow-list must be a pure reshaping of "just the
    cookie-session branch", not an accidental narrowing.

    Driven through resolve_ws_identity rather than a hand-built WsIdentity, so
    it proves the property for the identity PRODUCTION actually hands in. That
    matters now that the admin role is carried on the identity (captured by the
    cookie branch that already read the row) instead of re-fetched here.
    """
    from app.core.auth_guard import resolve_ws_identity, ws_session_owner_denied

    admin = await user_store.create(f"real-admin-login-{uuid.uuid4().hex[:8]}", "pw", role="admin")
    victim_sid = "victim-real-login-" + uuid.uuid4().hex[:8]
    await session_store.create(victim_sid, user_id="someone-else")

    identity = await resolve_ws_identity(_CookieWs(admin["id"]))
    assert identity.via_login is True
    assert identity.role == "admin"
    assert await ws_session_owner_denied(victim_sid, identity) is False


class _CookieWs:
    """The bare minimum resolve_ws_identity reads off a WebSocket."""

    def __init__(self, user_id: str) -> None:
        self.scope = {"subprotocols": []}
        self.session = {"user_id": user_id}
        self.query_params = {}


@pytest.mark.asyncio
async def test_ws_session_owner_denied_fails_closed_without_a_carried_role(_with_password):
    """The admin bypass now reads `identity.role`, and that field defaults to
    None. A future identity source that sets via_login but forgets the role is
    therefore denied the bypass by construction -- the same fail-closed shape
    the allow-list itself has, extended to the new field rather than working
    around it."""
    from app.core.auth_guard import WsIdentity, ws_session_owner_denied

    admin = await user_store.create(f"roleless-admin-{uuid.uuid4().hex[:8]}", "pw", role="admin")
    victim_sid = "victim-roleless-" + uuid.uuid4().hex[:8]
    await session_store.create(victim_sid, user_id="someone-else")

    roleless = WsIdentity(user_id=admin["id"], device_id=None, via_login=True)  # role defaults None
    assert await ws_session_owner_denied(victim_sid, roleless) is True
