import pytest

from app.services.auth.tokens import (
    ACCESS_TTL_SECONDS,
    issue_access_token,
    issue_refresh_token,
    verify_access_token,
    verify_refresh_token,
)


def test_access_token_roundtrips_user_id():
    token = issue_access_token("user-123")
    assert verify_access_token(token) == "user-123"


def test_refresh_token_roundtrips_user_id():
    token = issue_refresh_token("user-123")
    assert verify_refresh_token(token) == "user-123"


def test_refresh_token_is_not_usable_as_access_token():
    """Salt tách biệt hai loại token. Nếu không, refresh token 30 ngày sẽ
    dùng thay access token được -- biến TTL 1h thành vô nghĩa."""
    refresh = issue_refresh_token("user-123")
    assert verify_access_token(refresh) is None


def test_access_token_is_not_usable_as_refresh_token():
    access = issue_access_token("user-123")
    assert verify_refresh_token(access) is None


def test_tampered_token_is_rejected():
    token = issue_access_token("user-123")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert verify_access_token(tampered) is None


def test_garbage_token_is_rejected():
    assert verify_access_token("not-a-token") is None
    assert verify_access_token("") is None


def test_expired_access_token_is_rejected(monkeypatch):
    import app.services.auth.tokens as tokens_mod

    token = issue_access_token("user-123")
    # Giả lập token phát hành từ quá khứ bằng cách thu TTL về 0 rồi lùi thời gian.
    monkeypatch.setattr(tokens_mod, "ACCESS_TTL_SECONDS", -1)
    assert verify_access_token(token) is None


def test_access_ttl_is_one_hour():
    assert ACCESS_TTL_SECONDS == 3600


def test_token_payload_carries_no_role():
    """Ràng buộc bảo mật cốt lõi: token không mang role. Nếu payload có role
    thì sớm muộn sẽ có người đọc và tin nó."""
    from itsdangerous import URLSafeTimedSerializer

    from app.core.settings import settings

    token = issue_access_token("user-123")
    payload = URLSafeTimedSerializer(
        settings.effective_session_secret, salt="lugo-access"
    ).loads(token)
    assert payload == "user-123"
