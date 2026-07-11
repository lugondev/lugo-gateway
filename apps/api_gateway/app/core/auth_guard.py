import hmac

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
_ADMIN_PREFIXES = ("/v1/system", "/v1/models", "/v1/users", "/v1/devices")


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if not settings.admin_password:
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


def ws_authenticated(websocket: WebSocket) -> bool:
    """Auth check for WS handshakes — AuthGuardMiddleware can't run here since
    BaseHTTPMiddleware never runs for websocket scope. Browsers reuse the same
    cookie session as the HTTP UI; devices (no browser login flow) use a shared
    token passed as a query param at connect time."""
    if not settings.admin_password:
        return True
    if websocket.session.get("authenticated"):
        return True
    token = websocket.query_params.get("device_token")
    return bool(settings.device_auth_token) and hmac.compare_digest(token or "", settings.device_auth_token)
