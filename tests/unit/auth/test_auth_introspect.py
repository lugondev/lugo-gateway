import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth.tokens import issue_plugin_token
from app.services.plugins.models import Plugin
from app.services.plugins.store import plugin_store


@pytest.fixture(autouse=True)
def _registered_plugin():
    plugin_store.invalidate()
    plugin_store.upsert(
        Plugin(name="livehost", url="http://127.0.0.1:8091", secret="plugin-secret")
    )
    yield
    plugin_store.invalidate()


def _post(client, token, plugin="livehost", secret="plugin-secret"):
    return client.post(
        "/api/auth/introspect",
        json={"token": token, "plugin": plugin},
        headers={"Authorization": f"Bearer {secret}"},
    )


def test_a_valid_ticket_resolves_to_its_user():
    with TestClient(app) as client:
        r = _post(client, issue_plugin_token("user-1", "livehost"))
        assert r.status_code == 200
        assert r.json()["data"] == {"active": True, "user_id": "user-1"}


def test_a_ticket_for_another_plugin_is_inactive():
    with TestClient(app) as client:
        r = _post(client, issue_plugin_token("user-1", "lugo"))
        assert r.status_code == 200
        assert r.json()["data"]["active"] is False
        assert r.json()["data"]["user_id"] is None


def test_a_wrong_plugin_secret_is_401():
    """The whole reason introspection is authenticated: /api/auth sits in
    _NO_AUTH_PREFIXES, so without this an anyone-who-read-a-log lookup turns a
    ticket into a user_id."""
    with TestClient(app) as client:
        r = _post(client, issue_plugin_token("user-1", "livehost"), secret="wrong")
        assert r.status_code == 401


def test_a_missing_authorization_header_is_401():
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/introspect",
            json={"token": issue_plugin_token("user-1", "livehost"), "plugin": "livehost"},
        )
        assert r.status_code == 401


def test_an_unknown_plugin_is_401_not_404():
    """Same response as a bad secret: an unauthenticated caller must not be
    able to enumerate which plugins are registered."""
    with TestClient(app) as client:
        r = _post(client, "whatever", plugin="nope")
        assert r.status_code == 401


def test_a_disabled_plugin_cannot_introspect():
    plugin_store.upsert(
        Plugin(name="livehost", url="http://127.0.0.1:8091", secret="plugin-secret", enabled=False)
    )
    with TestClient(app) as client:
        r = _post(client, issue_plugin_token("user-1", "livehost"))
        assert r.status_code == 401


def test_garbage_token_is_inactive_not_an_error():
    with TestClient(app) as client:
        r = _post(client, "not-a-token")
        assert r.status_code == 200
        assert r.json()["data"]["active"] is False
