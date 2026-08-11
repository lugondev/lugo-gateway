import pytest

from app.core.auth_guard import _ADMIN_PREFIXES, _USER_EXACT, _classify
from app.services.auth.tokens import verify_plugin_token
from app.services.plugins.models import Plugin
from app.services.plugins.store import plugin_store


@pytest.fixture(autouse=True)
def _fresh_store():
    plugin_store.invalidate()
    yield
    plugin_store.invalidate()


def _seed(name="livehost", **over):
    data = {"name": name, "url": "http://127.0.0.1:8091", "secret": "plugin-secret"}
    data.update(over)
    plugin_store.upsert(Plugin(**data))


# --- auth classification (pure, no client needed) ---


def test_the_plugins_prefix_is_admin():
    assert "/v1/plugins" in _ADMIN_PREFIXES
    assert _classify("/v1/plugins/x", "PUT") == "admin"
    assert _classify("/v1/plugins/x", "DELETE") == "admin"


def test_listing_is_a_user_carve_out_for_reads_only():
    assert _USER_EXACT["/v1/plugins"] == frozenset({"GET", "HEAD"})
    assert _classify("/v1/plugins", "GET") == "user"
    assert _classify("/v1/plugins", "POST") == "admin"


def test_the_ticket_carve_out_is_post_only():
    """GET/PUT/DELETE /v1/plugins/ticket would route to the /{name} handlers
    with name='ticket'. Restricting the carve-out to POST -- for which no
    /{name} route exists -- is what stops that shadowing."""
    assert _USER_EXACT["/v1/plugins/ticket"] == frozenset({"POST"})
    assert _classify("/v1/plugins/ticket", "POST") == "user"
    assert _classify("/v1/plugins/ticket", "DELETE") == "admin"


# --- routes ---


def test_list_masks_the_secret_from_non_admins(user_client):
    _seed()
    r = user_client.get("/v1/plugins")
    assert r.status_code == 200
    assert r.json()["data"]["livehost"]["secret"] == "***"


def test_list_shows_the_secret_to_admins(admin_client):
    _seed()
    r = admin_client.get("/v1/plugins")
    assert r.json()["data"]["livehost"]["secret"] == "plugin-secret"


def test_create_requires_admin(user_client):
    r = user_client.post(
        "/v1/plugins",
        json={"name": "livehost", "url": "http://127.0.0.1:8091", "secret": "s"},
    )
    assert r.status_code == 403


def test_create_then_duplicate_is_409(admin_client):
    body = {"name": "livehost", "url": "http://127.0.0.1:8091", "secret": "s"}
    assert admin_client.post("/v1/plugins", json=body).status_code == 200
    assert admin_client.post("/v1/plugins", json=body).status_code == 409


def test_ticket_is_audience_bound_to_the_named_plugin(user_client):
    _seed()
    r = user_client.post("/v1/plugins/ticket", json={"plugin": "livehost"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["url"] == "http://127.0.0.1:8091"
    assert data["expires_in"] == 60
    assert verify_plugin_token(data["token"], "livehost") is not None
    assert verify_plugin_token(data["token"], "lugo") is None


def test_no_ticket_for_an_unknown_plugin(user_client):
    assert user_client.post("/v1/plugins/ticket", json={"plugin": "nope"}).status_code == 404


def test_no_ticket_for_a_disabled_plugin(user_client):
    _seed(enabled=False)
    assert user_client.post("/v1/plugins/ticket", json={"plugin": "livehost"}).status_code == 404


def test_delete_requires_admin(admin_client, user_client):
    _seed()
    assert user_client.delete("/v1/plugins/livehost").status_code == 403
    assert admin_client.delete("/v1/plugins/livehost").status_code == 200
    assert plugin_store.get("livehost") is None
