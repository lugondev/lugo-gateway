import hmac
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.websockets import WebSocket

from app.core.settings import settings

_STATIC_ALLOWLIST = {"/static/login.html", "/static/js/auth.js", "/static/styles.css"}
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
    "/v1/tts_profiles",
    "/v1/sessions",
    "/v1/devices/mine",
    "/v1/devices/pair/claim",
)
# role == "admin" required.
_ADMIN_PREFIXES = ("/v1/system", "/v1/models", "/v1/users", "/v1/devices", "/v1/model_registry")


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if not settings.auth_enabled:
            return await call_next(request)

        path = request.url.path
        if path in _STATIC_ALLOWLIST or path.startswith("/api/auth") or _matches(path, _NO_AUTH_PREFIXES):
            return await call_next(request)

        user_id = request.session.get("user_id")

        if _matches(path, _USER_PREFIXES):
            if not user_id:
                return self._unauthenticated(request)
            return await call_next(request)

        if _matches(path, _ADMIN_PREFIXES):
            if not user_id:
                return self._unauthenticated(request)
            if request.session.get("role") != "admin":
                return JSONResponse({"success": False, "error": "admin only"}, status_code=403)
            return await call_next(request)

        return await call_next(request)

    @staticmethod
    def _unauthenticated(request: Request):
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/static/login.html")
        return JSONResponse({"success": False, "error": "login required"}, status_code=401)


@dataclass
class WsIdentity:
    user_id: str | None
    device_id: str | None


async def resolve_ws_identity(websocket: WebSocket) -> "WsIdentity | None":
    """Resolves the identity behind a WS connection. Checks the browser
    cookie session first (and re-verifies the user isn't disabled -- a stale
    cookie from before a disable shouldn't grant a fresh connection), then a
    paired-device token (services.auth.devices), then the legacy shared
    device_auth_token as a temporary fallback for un-paired fleets."""
    from app.services.auth.devices import device_store
    from app.services.auth.users import user_store

    if not settings.auth_enabled:
        return WsIdentity(user_id=None, device_id=None)

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
