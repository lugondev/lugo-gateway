"""The guard must fail CLOSED: a path nobody classified requires auth.

Before this, AuthGuardMiddleware ended in `return await call_next(request)`,
so /v1/events, /agents-docs, /artifacts and /openapi.json were all reachable
with no credentials purely because nobody had added them to a prefix tuple.

NOTE on the `_with_password` fixture: tests/conftest.py's autouse `_hermetic`
fixture blanks `admin_password`/`admin_bootstrap_password`, which makes
`settings.auth_enabled` False and short-circuits the whole middleware. Every
test below that asserts on guard behaviour therefore has to turn auth back on
first -- same pattern as tests/unit/http/test_auth_guard.py.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


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


@pytest.mark.parametrize("path", ["/", "/health"])
def test_public_paths_need_no_auth(client, _with_password, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/v1/events/sessions/abc",
        "/v1/events/jobs/abc",
        "/artifacts/deadbeef.wav",
        "/agents-docs",
        "/openapi.json",
        "/docs",
        "/redoc",
        "/some/router/nobody/classified",
        # The /api/auth bypass used to be a bare `path.startswith("/api/auth")`,
        # so every one of these inherited it and was served anonymously.
        "/api/authz/whatever",
        "/api/authenticate",
        "/api/auth-bypass",
    ],
)
def test_previously_open_paths_now_require_auth(client, _with_password, path):
    """Anonymous callers must be rejected (401/403), never served (2xx)."""
    resp = client.get(path)
    assert resp.status_code in (401, 403), f"{path} returned {resp.status_code}"


def test_login_page_assets_stay_public(client, _with_password):
    """The login page must load before anyone has a session."""
    for path in ("/static/login.html", "/static/js/auth.js", "/static/styles.css"):
        assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("path", ["/api/auth/status", "/api/auth"])
def test_auth_routes_stay_public(client, _with_password, path):
    """Login/signup is the only way to GET a session, so it cannot require one.
    Moving "/api/auth" out of the bare startswith and into _NO_AUTH_PREFIXES
    must not have narrowed it: both the prefix itself and its children stay
    anonymous."""
    assert client.get(path).status_code != 401


def test_login_still_works_end_to_end_through_the_guard(client, _with_password):
    """The whole carve-out exists for this flow; assert it, not just its shape."""
    signup = client.post("/api/auth/signup", json={"username": "newbie", "password": "s3cret"})
    assert signup.status_code not in (401, 403), signup.text
    login = client.post("/api/auth/login", json={"username": "newbie", "password": "s3cret"})
    assert login.status_code == 200, login.text
    # And the session it hands back actually opens a guarded route.
    assert client.get("/v1/profiles").status_code not in (401, 403)


def test_pairing_handshake_stays_public(client, _with_password):
    """A device has no login; pair/init and pair/status must stay anonymous."""
    resp = client.post("/v1/devices/pair/init", json={"serial": "SN1"})
    assert resp.status_code != 401


def test_artifacts_reachable_for_logged_in_user(client, _with_password):
    """Default-deny must not lock out the legitimate caller: a logged-in user
    reaches the mount and gets a plain 404 for a file that isn't there, not a
    401 from the guard."""
    _login_as(client, "toan", "s3cret", role="user")
    assert client.get("/artifacts/deadbeef.wav").status_code == 404


def test_docs_reachable_for_admin(client, _with_password):
    """/openapi.json is admin-gated, not bricked."""
    _login_as(client, "root", "s3cret", role="admin")
    assert client.get("/openapi.json").status_code == 200


def test_docs_forbidden_for_regular_user(client, _with_password):
    _login_as(client, "toan", "s3cret", role="user")
    assert client.get("/openapi.json").status_code == 403


def test_segment_boundary_prevents_prefix_smuggling():
    """`/v1/usage/me` is a user carve-out inside the admin `/v1/usage` prefix.
    A raw startswith would also admit `/v1/usage/metrics` as user-level."""
    from app.core.auth_guard import _matches

    assert _matches("/v1/usage/me", ("/v1/usage/me",)) is True
    assert _matches("/v1/usage/me/detail", ("/v1/usage/me",)) is True
    assert _matches("/v1/usage/metrics", ("/v1/usage/me",)) is False
    assert _matches("/v1/model_registry/optionsets", ("/v1/model_registry/options",)) is False
    # The one prefix stored WITH a trailing slash must keep working.
    assert _matches("/static/js/auth.js", ("/static/",)) is True
    # /docs/oauth2-redirect is a real FastAPI auto-route and must inherit /docs.
    assert _matches("/docs/oauth2-redirect", ("/docs",)) is True
