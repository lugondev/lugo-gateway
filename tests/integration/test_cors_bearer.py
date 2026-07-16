import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_preflight_allows_authorization_header(client):
    resp = client.options(
        "/v1/sessions",
        headers={
            "Origin": "https://lugo.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.status_code < 400
    allowed = resp.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allowed or allowed == "*"


def test_arbitrary_origin_is_never_granted_credentials(client):
    """Với allow_origins=["*"], Starlette echo lại mọi origin. Nếu kèm
    Allow-Credentials: true thì bất kỳ website nào cũng đọc được response đã
    xác thực bằng cookie -- hôm nay chỉ SameSite=lax chặn lại. Bearer không
    cần credentials, nên tắt hẳn."""
    resp = client.options(
        "/v1/sessions",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.headers.get("access-control-allow-credentials") != "true"
