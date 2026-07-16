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
async def ws_user():
    return await user_store.create("ws-bearer-user", "pw12345678", role="user")


async def test_ws_accepts_valid_bearer_subprotocol(client, _with_password, ws_user):
    token = issue_access_token(ws_user["id"])
    with client.websocket_connect(
        "/v1/stt/stream", subprotocols=["bearer", token]
    ) as ws:
        assert ws.accepted_subprotocol == "bearer"


async def test_ws_rejects_invalid_bearer_subprotocol(client, _with_password):
    with pytest.raises(Exception):  # noqa: B017 -- TestClient raises khi server đóng
        with client.websocket_connect("/v1/stt/stream", subprotocols=["bearer", "garbage"]):
            pass


async def test_ws_rejects_disabled_user(client, _with_password, ws_user):
    await user_store.set_fields(ws_user["id"], disabled=True)
    token = issue_access_token(ws_user["id"])
    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect("/v1/stt/stream", subprotocols=["bearer", token]):
            pass


async def test_ws_rejects_token_in_query_string(client, _with_password, ws_user):
    """Access token KHÔNG được chấp nhận qua query string -- chỉ device_token
    (khác loại, đường riêng) mới đi lối đó."""
    token = issue_access_token(ws_user["id"])
    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(f"/v1/stt/stream?device_token={token}"):
            pass
