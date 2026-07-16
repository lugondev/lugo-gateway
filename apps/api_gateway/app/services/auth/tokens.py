"""Bearer token cho Lugo web client.

Payload cố ý chỉ chứa user_id. Đường bearer luôn là role="user" (xem
auth_guard), nên một role claim sẽ là lời nói dối chờ được tin.
"""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.settings import settings

ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600

# Salt tách access khỏi refresh: cùng secret nhưng không thể dùng thay nhau.
_ACCESS_SALT = "lugo-access"
_REFRESH_SALT = "lugo-refresh"


def _serializer(salt: str) -> URLSafeTimedSerializer:
    # Dựng mỗi lần gọi thay vì cache: settings.effective_session_secret có thể
    # bị monkeypatch trong test, và chi phí dựng là không đáng kể.
    return URLSafeTimedSerializer(settings.effective_session_secret, salt=salt)


def _issue(user_id: str, salt: str) -> str:
    return _serializer(salt).dumps(user_id)


def _verify(token: str, salt: str, max_age: int) -> str | None:
    if not token:
        return None
    try:
        payload = _serializer(salt).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, str):
        return None
    return payload


def issue_access_token(user_id: str) -> str:
    return _issue(user_id, _ACCESS_SALT)


def issue_refresh_token(user_id: str) -> str:
    return _issue(user_id, _REFRESH_SALT)


def verify_access_token(token: str) -> str | None:
    return _verify(token, _ACCESS_SALT, ACCESS_TTL_SECONDS)


def verify_refresh_token(token: str) -> str | None:
    return _verify(token, _REFRESH_SALT, REFRESH_TTL_SECONDS)
