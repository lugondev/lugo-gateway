from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.websockets import WebSocket

from app.core.settings import settings

_STATIC_ALLOWLIST = {"/static/login.html", "/static/js/auth.js", "/static/styles.css"}
_GUARDED_PREFIXES = (
    "/ui",
    "/static/",
    "/v1/system",
    "/v1/models",
    "/v1/profiles",
    "/v1/mcp",
    "/v1/sessions",
)


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if not settings.admin_password:
            return await call_next(request)

        path = request.url.path
        if path in _STATIC_ALLOWLIST or path.startswith("/api/auth"):
            return await call_next(request)

        if any(path == prefix or path.startswith(prefix) for prefix in _GUARDED_PREFIXES):
            if not request.session.get("authenticated"):
                if "text/html" in request.headers.get("accept", ""):
                    return RedirectResponse("/static/login.html")
                return JSONResponse({"success": False, "error": "login required"}, status_code=401)

        return await call_next(request)


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
    return bool(settings.device_auth_token) and token == settings.device_auth_token
