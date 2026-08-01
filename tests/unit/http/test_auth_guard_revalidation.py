"""The cookie-session branch of AuthGuardMiddleware trusted `user_id` and
`role` straight out of the signed cookie with no DB lookup, so disabling a
user did not lock them out and demoting an admin did not remove admin -- for
the full lifetime of the cookie.

Every other identity path in this app already re-checks: `_bearer_actor`
(auth_guard), `resolve_ws_identity` (auth_guard, whose docstring says why),
and `/api/auth/status`. These pin the cookie path to the same rule.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.users import user_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username, password, role="user"):
    client.post("/api/auth/signup", json={"username": username, "password": password})
    user = asyncio.run(user_store.get_by_username(username))
    if role == "admin":
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    assert client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).status_code == 200
    return user


def test_disabling_a_user_revokes_their_live_cookie_session(client, _with_password):
    user = _signup_login(client, "victim", "s3cret", role="user")
    assert client.get("/v1/profiles").status_code == 200

    asyncio.run(user_store.set_fields(user.id, disabled=True))

    assert client.get("/v1/profiles").status_code == 401


def test_demoting_an_admin_revokes_admin_on_their_live_cookie_session(
    client, _with_password
):
    user = _signup_login(client, "exadmin", "s3cret", role="admin")
    assert client.get("/v1/system/status").status_code == 200

    asyncio.run(user_store.set_fields(user.id, role="user"))

    assert client.get("/v1/system/status").status_code == 403


def test_promoting_a_user_grants_admin_on_their_live_cookie_session(
    client, _with_password
):
    """The same rule read the other way: role comes from the DB, so a promotion
    takes effect without the user logging out and back in."""
    user = _signup_login(client, "risen", "s3cret", role="user")
    assert client.get("/v1/system/status").status_code == 403

    asyncio.run(user_store.set_fields(user.id, role="admin"))

    assert client.get("/v1/system/status").status_code == 200


def test_deleted_user_with_a_live_cookie_is_rejected(client, _with_password):
    user = _signup_login(client, "ghost", "s3cret", role="user")
    assert client.get("/v1/profiles").status_code == 200

    # No delete route today; a row that vanishes any other way must behave the
    # same as a disabled one rather than sail through on the cookie alone.
    async def _drop():
        from sqlalchemy import delete

        from app.services.db.engine import db_session
        from app.services.db.models import User

        async with db_session() as s:
            await s.execute(delete(User).where(User.id == user.id))
            await s.commit()

    asyncio.run(_drop())

    assert client.get("/v1/profiles").status_code == 401
