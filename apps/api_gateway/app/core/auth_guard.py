import hmac
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.websockets import WebSocket

from app.core.actor import Actor
from app.core.settings import settings
from app.services.auth.tokens import verify_access_token

_STATIC_ALLOWLIST = {
    "/static/login.html",
    "/static/js/auth.js",
    "/static/styles.css",
    "/static/brand/favicon.svg",
    "/static/brand/logo-mark-light.svg",
}
# Unauthenticated device-side pairing handshake (the device itself has no login).
_NO_AUTH_PREFIXES = ("/v1/devices/pair/init", "/v1/devices/pair/status")
# Any logged-in session (admin or user).
_USER_PREFIXES = (
    "/ui",
    "/static/",
    "/v1/conversation",
    "/v1/livehost",
    "/v1/profiles",
    "/v1/mcp",
    "/v1/stt",
    "/v1/tts",
    "/v1/sessions",
    "/v1/devices/mine",
    "/v1/devices/pair/claim",
    # User-facing read of an otherwise admin-only prefix: /v1/model_registry/options
    # is THE feed every user's profile-editor dropdowns read. _USER_PREFIXES is
    # matched before _ADMIN_PREFIXES, so this carve-out wins over the
    # "/v1/model_registry" admin rule below while the rest of the CRUD surface
    # stays admin-only.
    "/v1/model_registry/options",
    # Same carve-out, for the caller's own usage totals: /v1/usage/me is
    # every logged-in user's "my usage" view, out of the otherwise
    # admin-only /v1/usage prefix (see _ADMIN_PREFIXES below). Checked
    # first, so this wins over the admin rule while /v1/usage/summary stays
    # admin-only.
    "/v1/usage/me",
)
# role == "admin" required.
_ADMIN_PREFIXES = ("/v1/system", "/v1/models", "/v1/users", "/v1/devices", "/v1/model_registry", "/v1/providers", "/v1/usage")


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


async def _bearer_actor(request: Request) -> "Actor | None":
    """Phân giải danh tính từ Authorization: Bearer. LUÔN trả role="user" --
    role trong token không được đọc, vì không tồn tại. Đây là lý do web client
    không thể leo thang lên admin dù người dùng là admin trong DB."""
    from app.services.auth.users import user_store

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    user_id = verify_access_token(token.strip())
    if not user_id:
        return None

    user = await user_store.get_by_id(user_id)
    if user is None or user.disabled:
        return None
    return Actor(user_id=user.id, role="user")


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if not settings.auth_enabled:
            return await call_next(request)

        path = request.url.path
        if path in _STATIC_ALLOWLIST or path.startswith("/api/auth") or _matches(path, _NO_AUTH_PREFIXES):
            return await call_next(request)

        # "authentication chỉ dùng 1, không fallback" -- nếu request chào
        # scheme bearer, bearer LÀ danh tính duy nhất cho request đó. Token
        # hỏng -> 401 ngay, không được rơi về cookie session.
        header = request.headers.get("authorization", "")
        scheme, _, _token = header.partition(" ")
        if scheme.lower() == "bearer":
            actor = await _bearer_actor(request)
            if actor is None:
                return self._bearer_unauthenticated()
            request.state.actor = actor
            user_id = actor.user_id
        else:
            actor = None
            user_id = request.session.get("user_id")

        if _matches(path, _USER_PREFIXES):
            if not user_id:
                return self._unauthenticated(request)
            return await call_next(request)

        if _matches(path, _ADMIN_PREFIXES):
            if not user_id:
                return self._unauthenticated(request)
            role = actor.role if actor is not None else request.session.get("role")
            if role != "admin":
                return JSONResponse({"success": False, "error": "admin only"}, status_code=403)
            return await call_next(request)

        return await call_next(request)

    @staticmethod
    def _unauthenticated(request: Request):
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/static/login.html")
        return JSONResponse({"success": False, "error": "login required"}, status_code=401)

    @staticmethod
    def _bearer_unauthenticated():
        # Never redirect here: an API client presenting a bad token is not a
        # browser navigating, and a 302 to the login page would confuse the
        # SPA's refresh logic. Always JSON, regardless of Accept.
        return JSONResponse({"success": False, "error": "invalid bearer token"}, status_code=401)


@dataclass
class WsIdentity:
    user_id: str | None
    device_id: str | None


async def resolve_ws_identity(websocket: WebSocket) -> "WsIdentity | None":
    """Resolves the identity behind a WS connection. Checks the bearer
    subprotocol first, then the browser cookie session (and re-verifies the
    user isn't disabled -- a stale cookie from before a disable shouldn't
    grant a fresh connection), then a paired-device token (services.auth.devices),
    then the legacy shared device_auth_token as a temporary fallback for
    un-paired fleets."""
    from app.services.auth.devices import device_store
    from app.services.auth.users import user_store

    if not settings.auth_enabled:
        return WsIdentity(user_id=None, device_id=None)

    bearer = _bearer_from_subprotocols(websocket)
    if bearer:
        user_id = verify_access_token(bearer)
        if not user_id:
            return None
        user = await user_store.get_by_id(user_id)
        if user is None or user.disabled:
            return None
        return WsIdentity(user_id=user.id, device_id=None)

    session_user_id = websocket.session.get("user_id")
    if session_user_id:
        user = await user_store.get_by_id(session_user_id)
        if user is None or user.disabled:
            return None
        return WsIdentity(user_id=user.id, device_id=None)

    token = websocket.query_params.get("device_token")
    if not token:
        return None

    device = await device_store.get_by_token(token)
    if device is not None:
        if device.revoked:
            return None
        owner = await user_store.get_by_id(device.user_id)
        if owner is None or owner.disabled:
            return None
        await device_store.touch_last_seen(device.id)
        return WsIdentity(user_id=device.user_id, device_id=device.id)

    if settings.device_auth_token and hmac.compare_digest(token, settings.device_auth_token):
        return WsIdentity(user_id=None, device_id=None)
    return None


def _bearer_from_subprotocols(websocket: WebSocket) -> str | None:
    """Client chào: Sec-WebSocket-Protocol: bearer, <token>. Token nằm ở phần
    tử ngay sau "bearer"."""
    protocols = websocket.scope.get("subprotocols") or []
    for index, proto in enumerate(protocols):
        if proto == "bearer" and index + 1 < len(protocols):
            return protocols[index + 1]
    return None


def ws_subprotocol(websocket: WebSocket) -> str | None:
    """Subprotocol mà server phải echo lại khi accept. Trình duyệt đóng kết
    nối nếu server không echo đúng cái nó chào."""
    return "bearer" if _bearer_from_subprotocols(websocket) else None
