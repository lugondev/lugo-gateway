"""Pins middleware registration order in app.main.

Starlette's `app.add_middleware()` inserts at index 0 of `app.user_middleware`,
so the LAST middleware *added* ends up OUTERMOST in the actual request chain
(it runs first on the way in, last on the way out). If CORSMiddleware is added
before AuthGuardMiddleware, CORS ends up INNERMOST -- any 401/403 that
AuthGuardMiddleware short-circuits never passes back through CORSMiddleware,
so the response ships with no Access-Control-Allow-Origin header. A
cross-origin browser client can't even read the status code of such a
response (it sees an opaque network failure), so client-side logic that
reacts to 401 (re-login, token refresh) never fires.

This test drives that exact scenario end-to-end through the real app: an
unauthenticated cross-origin request to an admin-guarded route must still
carry the ACAO header on its 401.
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


def test_auth_guard_401_still_carries_cors_header(client, _with_password):
    """AuthGuardMiddleware short-circuits this (admin-guarded, no session) with
    a 401 -- assert the status explicitly so this test fails loudly (not
    vacuously) if the fixture ever stops triggering the guard. Then assert
    CORSMiddleware still wrapped that short-circuited response."""
    resp = client.get("/v1/system/status", headers={"Origin": "https://example.com"})

    assert resp.status_code == 401
    assert "access-control-allow-origin" in resp.headers


def test_authorized_request_still_carries_cors_header(client, _with_password):
    """Same route, but authenticated as admin so it reaches the real handler
    instead of being short-circuited -- proves the reorder didn't break
    normal (non-short-circuited) CORS behavior."""
    _login_as(client, "root", "s3cret", role="admin")

    resp = client.get("/v1/system/status", headers={"Origin": "https://example.com"})

    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers
