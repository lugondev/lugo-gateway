"""Adversarial regression tests for R1: the guard MUST classify on the router's
dispatch path (get_route_path), not on request.url.path.

Closes the four findings the audit filed against auth_guard.py:
- H1: `#`/`%23`-truncation defeats _STATIC_ALLOWLIST -> whole static mount served
  unauthenticated. request.url.path drops everything from the first `#`; uvicorn
  decodes `%23`->`#` before routing, so the guard sees the allowlisted string
  while the router serves the real, non-allowlisted file.
- M1: path-param shadowing. `POST /v1/devices/mine/revoke` rides the
  `/v1/devices/mine` user carve-out but the ROUTER dispatches it to the admin
  `revoke_any_device(device_id="mine")`. Same for PATCH/DELETE on
  `/v1/model_registry/options` (dispatched to admin `update(entry_id="options")`).
- M2: a plain OPTIONS was exempt from the guard, enumerating the admin surface
  via the auto `405 Allow:`; only a genuine CORS preflight may skip the guard.
- M3: under `--root-path`, request.url.path keeps the mount prefix the router
  strips, so every admin prefix fell through to the user floor.

NOTE on `_with_password`: tests/conftest.py's autouse `_hermetic` blanks the
admin passwords, making settings.auth_enabled False and short-circuiting the
whole middleware. Any test asserting on guard behaviour must turn auth back on.
"""

import pytest
from fastapi.testclient import TestClient
from starlette._utils import get_route_path
from starlette.requests import Request

from app.core.auth_guard import _classify, _hostile_target, _is_own_device_action
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


def _req(path: str, *, raw_path: bytes | None = None, root_path: str = "", method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": raw_path if raw_path is not None else path.encode("latin-1"),
            "root_path": root_path,
            "query_string": b"",
            "headers": [],
        }
    )


# --------------------------------------------------------------------------- #
# H1 -- `#`/`%23`-truncation static bypass                                     #
# --------------------------------------------------------------------------- #


def test_hash_truncation_of_static_path_is_rejected_as_hostile():
    """The exact H1 exploit: url.path truncates to the allowlisted
    /static/login.html, raw_path still carries %23/../index.html. A request that
    needs `%23` to classify one way and dispatch another is hostile -> deny."""
    req = _req("/static/login.html", raw_path=b"/static/login.html%23/../index.html")
    assert _hostile_target(req) is True


def test_literal_hash_and_dot_segments_are_hostile():
    assert _hostile_target(_req("/static/x", raw_path=b"/static/x#y")) is True
    assert _hostile_target(_req("/v1/../v1/users", raw_path=b"/v1/../v1/users")) is True
    assert _hostile_target(_req("/a/./b", raw_path=b"/a/./b")) is True
    assert _hostile_target(_req("/a/x", raw_path=b"/a/%2e%2e/x")) is True


def test_normal_paths_are_not_hostile():
    for p in ("/static/login.html", "/static/js/auth.js", "/v1/devices/mine", "/openapi.json"):
        assert _hostile_target(_req(p, raw_path=p.encode())) is False, p


def test_real_routed_path_decides_not_the_truncated_string():
    """Even setting the hostile-reject aside: the guard must classify the string
    the router dispatches. The allowlisted login page is public, but the real
    admin console index.html is NOT allowlisted and must require a login."""
    assert _classify("/static/login.html", "GET") == "public"
    assert _classify("/static/index.html", "GET") == "user"


# --------------------------------------------------------------------------- #
# M1 -- path-param shadowing of admin handlers                                 #
# --------------------------------------------------------------------------- #


def test_devices_mine_revoke_collision_is_admin_not_user():
    """`POST /v1/devices/mine/revoke` dispatches to the admin
    revoke_any_device(device_id="mine") -- it must NOT ride the user carve-out."""
    assert _classify("/v1/devices/mine/revoke", "POST") == "admin"


def test_own_device_revoke_subpath_stays_user():
    """`POST /v1/devices/mine/{device_id}/revoke` is the user's own-device revoke
    and must stay user-level (regression guard: closing M1 must not brick it)."""
    assert _classify("/v1/devices/mine/dev-123/revoke", "POST") == "user"
    assert _classify("/v1/devices/mine", "GET") == "user"
    assert _classify("/v1/devices", "GET") == "admin"


def test_is_own_device_action_shape():
    assert _is_own_device_action("/v1/devices/mine/abc/revoke") is True
    assert _is_own_device_action("/v1/devices/mine/abc/profile") is True
    assert _is_own_device_action("/v1/devices/mine/revoke") is False  # the attack
    assert _is_own_device_action("/v1/devices/mine/profile") is False  # same attack shape
    assert _is_own_device_action("/v1/devices/mine") is False
    assert _is_own_device_action("/v1/devices/x/revoke") is False
    assert _is_own_device_action("/v1/devices/mine//revoke") is False


def test_own_device_action_list_is_an_allowlist():
    """An action NOT named in _OWN_DEVICE_ACTIONS gets no carve-out, so a future
    route under /v1/devices/mine/{id}/ cannot inherit user access by accident --
    it has to be added deliberately. Unmatched paths fall through to the
    /v1/devices ADMIN prefix, which is the safe direction to fail."""
    assert _is_own_device_action("/v1/devices/mine/abc/rename") is False
    assert _classify("/v1/devices/mine/abc/rename", "POST") == "admin"


def test_own_device_profile_subpath_is_user_and_post_only():
    assert _classify("/v1/devices/mine/dev-123/profile", "POST") == "user"
    # Any other method on that path is not carved out; /v1/devices is an admin
    # prefix, so it must land on the admin rule rather than sliding through.
    assert _classify("/v1/devices/mine/dev-123/profile", "DELETE") == "admin"


def test_model_registry_options_is_method_aware():
    """GET /options is the user dropdown feed; PATCH/DELETE on that same string
    dispatch to the admin update/delete(entry_id="options") and must be admin."""
    assert _classify("/v1/model_registry/options", "GET") == "user"
    assert _classify("/v1/model_registry/defaults", "GET") == "user"
    assert _classify("/v1/model_registry/options", "PATCH") == "admin"
    assert _classify("/v1/model_registry/options", "DELETE") == "admin"
    assert _classify("/v1/model_registry/defaults", "PATCH") == "admin"


def test_usage_me_is_get_only_carveout():
    assert _classify("/v1/usage/me", "GET") == "user"
    assert _classify("/v1/usage/summary", "GET") == "admin"
    # A crafted mutation on the carve-out path must not be user-level.
    assert _classify("/v1/usage/me", "POST") == "admin"


def test_pair_claim_is_a_post_user_carveout():
    assert _classify("/v1/devices/pair/claim", "POST") == "user"


# --------------------------------------------------------------------------- #
# M3 -- root_path divergence                                                   #
# --------------------------------------------------------------------------- #


def test_root_path_admin_prefix_still_classifies_admin():
    """Under --root-path /gw the guard must strip /gw exactly as the router does
    (get_route_path), or admin prefixes fall through to the user floor."""
    scope = {"type": "http", "path": "/gw/v1/system/config", "root_path": "/gw"}
    assert _classify(get_route_path(scope), "GET") == "admin"
    scope2 = {"type": "http", "path": "/gw/v1/users", "root_path": "/gw"}
    assert _classify(get_route_path(scope2), "GET") == "admin"
    scope3 = {"type": "http", "path": "/gw/v1/usage/me", "root_path": "/gw"}
    assert _classify(get_route_path(scope3), "GET") == "user"


# --------------------------------------------------------------------------- #
# Integration through the real middleware                                      #
# --------------------------------------------------------------------------- #


def test_post_devices_mine_revoke_non_admin_is_forbidden(client, _with_password):
    """M1 end-to-end: a logged-in non-admin must not be served by the carve-out;
    the admin rule runs and denies with 403."""
    _login_as(client, "m1user", "s3cret", role="user")
    resp = client.post("/v1/devices/mine/revoke")
    assert resp.status_code == 403, resp.text


def test_own_device_revoke_still_reaches_route_for_non_admin(client, _with_password):
    """Regression guard: the legit own-device revoke stays user-level, so a
    non-admin reaches the handler and gets 404 (device not found), not 403/401."""
    _login_as(client, "ownr", "s3cret", role="user")
    resp = client.post("/v1/devices/mine/no-such-device/revoke")
    assert resp.status_code == 404, resp.text


def test_patch_model_registry_options_non_admin_is_forbidden(client, _with_password):
    _login_as(client, "m1user2", "s3cret", role="user")
    resp = client.patch("/v1/model_registry/options", json={})
    assert resp.status_code == 403, resp.text


def test_get_model_registry_options_ok_for_non_admin(client, _with_password):
    """The carve-out must keep working for its real purpose: the user dropdown."""
    _login_as(client, "reader", "s3cret", role="user")
    resp = client.get("/v1/model_registry/options")
    assert resp.status_code not in (401, 403), resp.text


# --------------------------------------------------------------------------- #
# M2 -- OPTIONS enumeration                                                    #
# --------------------------------------------------------------------------- #


def test_plain_options_is_guarded(client, _with_password):
    """A plain OPTIONS with no CORS preflight header must be classified like any
    other method, not exempted -- anonymous -> denied."""
    resp = client.options("/v1/users")
    assert resp.status_code in (401, 403), resp.status_code


def test_options_with_acrm_but_no_origin_is_guarded(client, _with_password):
    """The M2 re-open: Access-Control-Request-Method WITHOUT Origin is not a
    genuine preflight (Fetch spec requires both). CORSMiddleware ignores it for
    lack of Origin and passes it through, so the guard must still deny it --
    otherwise this one header re-opens the admin-surface enumeration oracle."""
    resp = client.options("/v1/users", headers={"Access-Control-Request-Method": "GET"})
    assert resp.status_code in (401, 403), resp.status_code


def test_genuine_cors_preflight_passes(client, _with_password):
    """A GENUINE preflight carries BOTH Origin and Access-Control-Request-Method.
    CORSMiddleware (outside the guard) answers it before the guard runs, so it is
    never blocked -- and never reaches the router's enumeration oracle either."""
    resp = client.options(
        "/v1/users",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code not in (401, 403), resp.status_code
