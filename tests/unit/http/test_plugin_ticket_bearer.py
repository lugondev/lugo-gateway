"""A plugin ticket, used as a bearer, must work for exactly the narrow
allowlist auth_guard._PLUGIN_TICKET_BEARER_ROUTES names -- and nowhere else.

Found live: livehost.js's cross-origin page has no gateway session cookie of
its own, so it has to send its plugin ticket as Authorization: Bearer for
the handful of gateway calls it makes before opening the voice socket
(profile/engine dropdowns, STT warm). Nothing exercised this against a real
gateway until then -- the fake gateway used elsewhere in this plan never
validated a token cryptographically, only checked it was passed through.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.tokens import issue_access_token, issue_plugin_token
from app.services.auth.users import user_store
from app.services.plugins.models import Plugin
from app.services.plugins.store import plugin_store


@pytest.fixture(autouse=True)
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def real_user():
    return asyncio.run(user_store.create("plugin-page-user", "s3cret-password"))


@pytest.fixture(autouse=True)
def _fresh_plugin_store():
    plugin_store.invalidate()
    yield
    plugin_store.invalidate()


def _register(name="livehost", enabled=True):
    plugin_store.upsert(Plugin(name=name, url="http://127.0.0.1:8091", secret="s", enabled=enabled))


def _bearer(client, token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/profiles"),
        ("GET", "/v1/tts/profiles"),
        ("GET", "/v1/stt/engines"),
        ("POST", "/v1/stt/warm"),
    ],
)
def test_a_plugin_ticket_authenticates_each_allowlisted_route(client, real_user, method, path):
    _register()
    ticket = issue_plugin_token(real_user["id"], "livehost")
    resp = client.request(method, path, headers=_bearer(client, ticket))
    assert resp.status_code != 401, resp.text


def test_a_plugin_ticket_does_not_authenticate_an_unlisted_route(client, real_user):
    """The widening is an exact allowlist, not "any bearer that verifies
    somehow" -- a route this plan never named must still reject it."""
    _register()
    ticket = issue_plugin_token(real_user["id"], "livehost")
    resp = client.get("/v1/sessions", headers=_bearer(client, ticket))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_a_plugin_ticket_never_grants_admin(real_user):
    """Bearer auth caps at role=user regardless of the underlying account,
    same guarantee _bearer_actor already gives access tokens. None of the 4
    allowlisted routes is admin-gated, so this can't be observed over HTTP --
    it has to be checked at the source, the way test_auth_guard.py's own
    cookie-session tests check _session_actor/resolve_ws_identity directly."""
    from starlette.requests import Request

    from app.core.auth_guard import _bearer_actor

    await user_store.set_fields(real_user["id"], role="admin")
    _register()
    ticket = issue_plugin_token(real_user["id"], "livehost")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/profiles",
            "root_path": "",
            "headers": [(b"authorization", f"Bearer {ticket}".encode())],
        }
    )
    actor = await _bearer_actor(request)
    assert actor is not None
    assert actor.role == "user"


def test_a_ticket_for_an_unregistered_plugin_is_refused(client, real_user):
    """The allowlist is by route, not by which plugin registered it -- any
    registered, enabled plugin's ticket works for these shared, non-secret
    routes. What must still fail is a name nothing registered at all."""
    _register("livehost")
    ticket = issue_plugin_token(real_user["id"], "some-plugin-nobody-registered")
    resp = client.get("/v1/profiles", headers=_bearer(client, ticket))
    assert resp.status_code == 401


def test_a_disabled_plugins_ticket_is_refused(client, real_user):
    """Mirrors introspect's own enabled check: a plugin taken out of service
    must stop authenticating its old tickets immediately, not just stop
    minting new ones."""
    _register("livehost", enabled=False)
    ticket = issue_plugin_token(real_user["id"], "livehost")
    resp = client.get("/v1/profiles", headers=_bearer(client, ticket))
    assert resp.status_code == 401


def test_garbage_bearer_is_refused_on_an_allowlisted_route(client):
    _register()
    resp = client.get("/v1/profiles", headers=_bearer(client, "not-a-real-token"))
    assert resp.status_code == 401


def test_a_real_access_token_still_works_on_an_allowlisted_route(client, real_user):
    """The widening is additive: verify_access_token is tried first and an
    ordinary bearer session must keep working unchanged."""
    _register()
    resp = client.get("/v1/profiles", headers=_bearer(client, issue_access_token(real_user["id"])))
    assert resp.status_code != 401
