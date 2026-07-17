import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.tokens import issue_access_token
from app.services.auth.users import user_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


@pytest.fixture
async def admin_user():
    created = await user_store.create("bearer-admin", "pw12345678", role="admin")
    return created


@pytest.fixture
async def normal_user():
    created = await user_store.create("bearer-user", "pw12345678", role="user")
    return created


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_bearer_grants_user_prefix(client, _with_password, normal_user):
    token = issue_access_token(normal_user["id"])
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code != 401


async def test_no_bearer_is_rejected(client, _with_password):
    resp = client.get("/v1/sessions")
    assert resp.status_code == 401


async def test_invalid_bearer_is_rejected(client, _with_password):
    resp = client.get("/v1/sessions", headers=_auth("garbage"))
    assert resp.status_code == 401


async def test_bearer_for_unknown_user_is_rejected(client, _with_password):
    token = issue_access_token("no-such-user-id")
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code == 401


async def test_bearer_for_disabled_user_is_rejected(client, _with_password, normal_user):
    await user_store.set_fields(normal_user["id"], disabled=True)
    token = issue_access_token(normal_user["id"])
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code == 401


async def test_bearer_never_reaches_admin_prefix_even_for_admin_user(
    client, _with_password, admin_user
):
    """Ràng buộc cốt lõi: token của một user role=admin trong DB vẫn KHÔNG
    mở được đường admin, vì đường bearer hardcode role="user"."""
    token = issue_access_token(admin_user["id"])
    resp = client.get("/v1/system/status", headers=_auth(token))
    assert resp.status_code == 403


async def test_admin_prefix_still_works_via_session_cookie(client, _with_password, admin_user):
    """Admin webui không được hỏng."""
    client.post("/api/auth/login", json={"username": "bearer-admin", "password": "pw12345678"})
    resp = client.get("/v1/system/status")
    assert resp.status_code == 200


async def test_invalid_bearer_with_valid_admin_cookie_is_401_on_user_prefix(
    client, _with_password, admin_user
):
    """Chính sách mới: 1 phương thức, không fallback. Bearer hỏng thì KHÔNG
    được rơi về danh tính cookie, kể cả khi cookie hợp lệ và là admin."""
    client.post("/api/auth/login", json={"username": "bearer-admin", "password": "pw12345678"})
    resp = client.get("/v1/sessions", headers=_auth("garbage"))
    assert resp.status_code == 401


async def test_invalid_bearer_with_valid_admin_cookie_is_401_on_admin_prefix(
    client, _with_password, admin_user
):
    """Không phải 403 (nghĩa là có danh tính nhưng thiếu quyền), không phải
    200 (nghĩa là fallback cookie) -- request này không có danh tính nào cả."""
    client.post("/api/auth/login", json={"username": "bearer-admin", "password": "pw12345678"})
    resp = client.get("/v1/system/status", headers=_auth("garbage"))
    assert resp.status_code == 401


async def test_expired_or_tampered_bearer_with_no_cookie_is_401(client, _with_password, normal_user):
    token = issue_access_token(normal_user["id"])
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    resp = client.get("/v1/sessions", headers=_auth(tampered))
    assert resp.status_code == 401


async def test_basic_auth_header_with_valid_cookie_is_not_bearer_401(client, _with_password, admin_user):
    """Basic <something> không phải là một lần thử bearer -- không được kích
    hoạt đường 401 của bearer. Cookie vẫn thắng như trước."""
    client.post("/api/auth/login", json={"username": "bearer-admin", "password": "pw12345678"})
    resp = client.get("/v1/system/status", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 200


async def test_no_authorization_header_with_valid_cookie_is_unchanged(client, _with_password, admin_user):
    client.post("/api/auth/login", json={"username": "bearer-admin", "password": "pw12345678"})
    resp = client.get("/v1/system/status")
    assert resp.status_code == 200


async def test_valid_bearer_user_prefix_unchanged(client, _with_password, normal_user):
    token = issue_access_token(normal_user["id"])
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code != 401


async def test_valid_bearer_admin_prefix_still_403(client, _with_password, admin_user):
    token = issue_access_token(admin_user["id"])
    resp = client.get("/v1/system/status", headers=_auth(token))
    assert resp.status_code == 403


async def test_failed_bearer_401_is_json_not_redirect_even_with_html_accept(
    client, _with_password, admin_user
):
    """Client trình bày token, không phải trình duyệt điều hướng -- 302 tới
    trang login sẽ làm hỏng logic refresh của SPA."""
    client.post("/api/auth/login", json={"username": "bearer-admin", "password": "pw12345678"})
    resp = client.get(
        "/v1/sessions",
        headers={**_auth("garbage"), "Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert resp.headers.get("content-type", "").startswith("application/json")


async def test_bearer_devices_mine_is_not_401(client, _with_password, normal_user):
    """Bug: /v1/devices/mine reads request.session directly instead of using
    current_user_id(), so a bearer-only (cross-origin, cookie-less) request
    was rejected with 401 even though the guard already let it through."""
    token = issue_access_token(normal_user["id"])
    resp = client.get("/v1/devices/mine", headers=_auth(token))
    assert resp.status_code == 200


async def test_bearer_devices_revoke_is_not_401(client, _with_password, normal_user):
    """A 404 (device not found) proves auth passed and the route ran -- that
    is what distinguishes this from the auth-layer 401 bug."""
    token = issue_access_token(normal_user["id"])
    resp = client.post("/v1/devices/mine/no-such-device/revoke", headers=_auth(token))
    assert resp.status_code != 401


async def test_bearer_devices_pair_claim_is_not_401(client, _with_password, normal_user):
    """Worst symptom of the bug: pairing itself was unreachable over bearer.
    A bogus code should fail on the pairing code, not on auth."""
    token = issue_access_token(normal_user["id"])
    resp = client.post(
        "/v1/devices/pair/claim",
        json={"code": "000000", "name": "my-device"},
        headers=_auth(token),
    )
    assert resp.status_code != 401


async def test_bearer_devices_mine_no_auth_is_still_401(client, _with_password):
    resp = client.get("/v1/devices/mine")
    assert resp.status_code == 401


async def test_bearer_never_reaches_admin_devices_listing(client, _with_password, admin_user):
    """Invariant of the whole design: bearer must never reach admin routes,
    even for a user whose DB role is admin (bearer path hardcodes role=user)."""
    token = issue_access_token(admin_user["id"])
    resp = client.get("/v1/devices", headers=_auth(token))
    assert resp.status_code == 403


async def test_cookie_session_devices_mine_still_works(client, _with_password, normal_user):
    """The admin webui depends on the cookie-session path continuing to work."""
    client.post(
        "/api/auth/login",
        json={"username": "bearer-user", "password": "pw12345678"},
    )
    resp = client.get("/v1/devices/mine")
    assert resp.status_code == 200
