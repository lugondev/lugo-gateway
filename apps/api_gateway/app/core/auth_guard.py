import hmac
from dataclasses import dataclass

from starlette._utils import get_route_path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.websockets import WebSocket

from app.core.actor import Actor
from app.core.settings import settings
from app.services.auth.tokens import verify_access_token

# Reachable with no credentials at all. Everything NOT classified here or in
# the user/admin tuples below is denied by default -- see dispatch().
_PUBLIC_PATHS = frozenset({"/", "/health"})
_STATIC_ALLOWLIST = {
    "/static/login.html",
    "/static/js/auth.js",
    "/static/styles.css",
    "/static/brand/favicon.svg",
    "/static/brand/logo-mark-light.svg",
}
# Reachable with no credentials. Matched with _matches(), i.e. on a segment
# boundary -- NOT a bare startswith. A bare startswith here would make a future
# "/api/authz/..." mount silently public, which is precisely the bug class this
# module's default-deny exists to kill.
_NO_AUTH_PREFIXES = (
    # Login/signup/refresh/logout: the only way to obtain a session in the first
    # place, so it cannot itself require one.
    "/api/auth",
    # Device-side pairing handshake (the device itself has no login).
    "/v1/devices/pair/init",
    "/v1/devices/pair/status",
)
# Any logged-in session (admin or user). Matched with _matches() (segment
# boundary). NONE of these may overlap an _ADMIN_PREFIXES entry: the classifier
# checks admin BEFORE these broad user prefixes (see _classify), so an overlap
# would be silently upgraded to admin. The user carve-outs that DO sit inside an
# admin prefix live in _USER_EXACT below instead, precisely so they can be
# matched exactly (and by method) rather than as a swallow-everything prefix.
_USER_PREFIXES = (
    "/ui",
    "/static/",
    # Live SSE transcript/TTS-result streams. These carry the actual content of
    # a session (partial + final transcripts, synthesized audio urls); they were
    # unauthenticated purely because nobody had listed them anywhere.
    "/v1/events",
    "/v1/conversation",
    "/v1/livehost",
    "/v1/profiles",
    "/v1/mcp",
    "/v1/stt",
    "/v1/tts",
    "/v1/sessions",
)
# User carve-outs that sit INSIDE an admin prefix. Matched EXACTLY (never as a
# prefix) and BY METHOD, checked BEFORE _ADMIN_PREFIXES. Both properties are
# load-bearing against M1 (path-param shadowing of admin handlers):
#
#  - EXACT, not prefix: `/v1/devices/mine` as a bare prefix also admits
#    `POST /v1/devices/mine/revoke`, which the ROUTER dispatches to the admin
#    `revoke_any_device(device_id="mine")` -- the user carve-out would smuggle a
#    non-admin into an admin handler. The legitimate subpaths under it,
#    `POST /v1/devices/mine/{device_id}/{revoke,profile}`, are handled by
#    _is_own_device_action() below, whose shape check `/mine/revoke` cannot match.
#
#  - BY METHOD: `/v1/model_registry/options` is the user dropdown feed for GET,
#    but PATCH/DELETE on that exact string dispatch to the admin
#    `update/delete_entry(entry_id="options")` (the `/{entry_id}` route). The
#    carve-out therefore only covers the safe read methods; every other method
#    falls through to the `/v1/model_registry` admin rule. Same reasoning for
#    `/v1/model_registry/defaults` and `/v1/usage/me`.
#
# `/v1/devices/pair/claim` is a POST user route with no admin route sharing its
# exact path, so its carve-out is {"POST"}.
_USER_EXACT: dict[str, frozenset[str]] = {
    "/v1/usage/me": frozenset({"GET", "HEAD"}),
    "/v1/model_registry/options": frozenset({"GET", "HEAD"}),
    "/v1/model_registry/defaults": frozenset({"GET", "HEAD"}),
    "/v1/devices/mine": frozenset({"GET", "HEAD"}),
    "/v1/devices/pair/claim": frozenset({"POST"}),
}
# role == "admin" required.
_ADMIN_PREFIXES = (
    "/v1/system",
    "/v1/models",
    "/v1/users",
    "/v1/devices",
    "/v1/model_registry",
    "/v1/providers",
    "/v1/usage",
    "/v1/quotas",
    # The internal agent runbook: AGENTS.md + docs/api.md + the full API map,
    # served as one plaintext bundle. Operator documentation, not a public page.
    "/agents-docs",
    # FastAPI's auto-generated API surface map. /docs also serves
    # /docs/oauth2-redirect, which the segment-boundary match below covers.
    "/docs",
    "/redoc",
    "/openapi.json",
)


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """Segment-aware prefix match.

    A raw startswith would make `/v1/usage/metrics` match the `/v1/usage/me`
    user carve-out and silently escape the admin rule it sits inside.
    """
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


# Final segments of `/v1/devices/mine/{device_id}/<action>` that a logged-in
# user may POST to for a device they own. An ALLOW-list, so adding a route under
# /v1/devices/mine/ does NOT silently inherit the carve-out: a new action stays
# at the default-deny floor until it is named here deliberately.
_OWN_DEVICE_ACTIONS = frozenset({"revoke", "profile"})


def _is_own_device_action(path: str) -> bool:
    """True ONLY for `/v1/devices/mine/{device_id}/<allowed action>` -- the user's
    own-device subresources (legitimate user subpaths under the exact
    `/v1/devices/mine` carve-out). Deliberately shape-precise so it does NOT match
    the M1 attack `/v1/devices/mine/revoke` (which the router dispatches to the
    admin `revoke_any_device(device_id="mine")`): that string has an empty middle
    segment and is rejected by the `device_id` non-empty check."""
    parts = path.split("/")
    return (
        len(parts) == 6
        and parts[:4] == ["", "v1", "devices", "mine"]
        and parts[4] != ""
        and parts[5] in _OWN_DEVICE_ACTIONS
    )


def _hostile_target(request: Request) -> bool:
    """Defense in depth (fix #2): a request whose raw target needs `#`, `%23`,
    or a `.`/`..` path segment to classify one way while the router dispatches it
    another is hostile -- reject BEFORE classification. This is the H1 root cause
    (request.url.path truncates at `#`; uvicorn decodes `%23`->`#` before
    routing) plus encoded/literal dot-segment traversal, caught regardless of how
    any particular server populates scope["path"]."""
    raw = request.scope.get("raw_path")
    raw_str = raw.decode("latin-1", "replace") if isinstance(raw, (bytes, bytearray)) else ""
    for text in (request.url.path, raw_str):
        lowered = text.lower()
        if "#" in text or "%23" in lowered or "%2e" in lowered:
            return True
    # Literal dot-segments in either the decoded route path or url.path.
    for candidate in (get_route_path(request.scope), request.url.path):
        if any(seg in (".", "..") for seg in candidate.split("/")):
            return True
    return False


def _classify(path: str, method: str) -> str | None:
    """Classify the ROUTER's dispatch path into public/user/admin, or None for
    the default-deny floor. `path` MUST be get_route_path(scope) (root_path
    stripped, no `#` truncation) so the guard sees exactly what the router
    dispatches -- this is the R1 fix for H1 and M3.

    Ordering is the whole security contract:
      1. public allowlists;
      2. exact, method-scoped user carve-outs that sit inside admin prefixes
         (_USER_EXACT + the own-device-revoke subpath) -- exact+method so a
         crafted subpath/method cannot ride them into an admin handler (M1);
      3. _ADMIN_PREFIXES BEFORE the broad _USER_PREFIXES, so the more-restrictive
         admin rule always wins any overlap (fix #4). No broad user prefix
         overlaps an admin prefix (asserted by
         test_no_admin_prefix_is_shadowed_by_a_user_prefix), so step 3 only ever
         resolves the carve-out overlaps deterministically;
      4. broad user prefixes;
      5. default-deny (None) -> caller still needs a login (user floor)."""
    if path in _PUBLIC_PATHS:
        return "public"
    if path in _STATIC_ALLOWLIST:
        return "public"
    if _matches(path, _NO_AUTH_PREFIXES):
        return "public"
    allowed = _USER_EXACT.get(path)
    if allowed is not None and method in allowed:
        return "user"
    if method == "POST" and _is_own_device_action(path):
        return "user"
    if _matches(path, _ADMIN_PREFIXES):
        return "admin"
    if _matches(path, _USER_PREFIXES):
        return "user"
    return None


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
        # Fix #5 (M2): the guard has NO OPTIONS exemption. A genuine CORS
        # preflight (Origin + Access-Control-Request-Method, per the Fetch spec)
        # is answered by CORSMiddleware, which is registered OUTSIDE this guard
        # (main.py) and consumes the preflight before it ever reaches here -- so
        # an exemption would only ever apply to a NON-genuine OPTIONS (an
        # Origin-less request CORS ignores and passes through), which is exactly
        # the admin-surface method-enumeration oracle M2 describes (the router's
        # auto `405/200 Allow:` served with no credentials). OPTIONS is therefore
        # classified like any other method.
        if not settings.auth_enabled:
            return await call_next(request)

        # Fix #2: reject hostile targets before we trust any classification.
        if _hostile_target(request):
            return self._unauthenticated(request)

        # Fix #1 (H1, M3): classify on the SAME string the router dispatches on
        # (root_path stripped, no `#` truncation), never request.url.path.
        path = get_route_path(request.scope)
        kind = _classify(path, request.method)
        if kind == "public":
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

        if kind == "user":
            if not user_id:
                return self._unauthenticated(request)
            return await call_next(request)

        if kind == "admin":
            if not user_id:
                return self._unauthenticated(request)
            role = actor.role if actor is not None else request.session.get("role")
            if role != "admin":
                return JSONResponse({"success": False, "error": "admin only"}, status_code=403)
            return await call_next(request)

        # Default-DENY (kind is None). Anything not classified above is treated
        # as at least user-level rather than public, so a newly mounted router
        # that nobody remembered to classify fails closed instead of being
        # served to the internet (which is exactly how /v1/events, /agents-docs,
        # /artifacts and /openapi.json ended up unauthenticated).
        if not user_id:
            return self._unauthenticated(request)
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
    # True ONLY for the dev-mode short-circuit below (settings.auth_enabled is
    # False): there is no way to prove ownership of anything in that mode, so
    # a consumer like conversation.py's session-ownership check should treat
    # it as fully unscoped, matching current_role()'s identical "no session
    # role -> admin" dev-mode default used on the HTTP side. The legacy shared
    # device_auth_token below ALSO resolves to user_id=None, but is a real,
    # auth-enabled deployment with no way to derive an owner -- it must stay
    # False here, or a caller holding that fleet-wide secret could read/
    # corrupt any real user's session by id (round-1 review, I1).
    unauthenticated: bool = False
    # True ONLY when this identity came from a bearer token
    # (Sec-WebSocket-Protocol). Mirrors _bearer_actor's documented invariant
    # just below: a bearer caller always resolves to role="user", even for an
    # admin account in the DB, so a web client can't escalate to admin just by
    # holding a bearer token. Provenance marker only as of the `via_login`
    # rewrite below (round-2 review): ws_session_owner_denied's admin bypass
    # is now an ALLOW-list keyed on `via_login`, which a bearer identity
    # never sets, so it's excluded from the bypass by construction rather
    # than by anything checking this flag directly.
    via_bearer: bool = False
    # True ONLY when this identity came from a paired-device token
    # (services.auth.devices.device_store), i.e. an ESP32/RPi that completed
    # the pairing handshake. Same provenance-marker-only note as via_bearer
    # above -- excluded from the admin bypass by the `via_login` allow-list
    # (round-2 review), not by a dedicated `not via_device` check.
    via_device: bool = False
    # True ONLY when this identity came from the browser-cookie-session
    # branch below (websocket.session.get("user_id")), i.e. an actual
    # interactive login -- the ONE identity source that legitimately gets
    # ws_session_owner_denied's admin-role DB-lookup bypass. Deliberately an
    # ALLOW-list, not the deny-list this module used through two rounds of
    # review (`not via_bearer`, then `... and not via_device`): a deny-list
    # fails OPEN for every identity source added to resolve_ws_identity
    # later -- whoever adds one must remember to both mint a new flag AND
    # extend the deny-list, or the new source silently inherits the admin
    # bypass. That's exactly how this bug class recurred twice already
    # (bearer, then paired-device). Default False and set True ONLY at the
    # cookie-session branch, so a future branch that forgets to set it is
    # denied the bypass by construction instead of granted it by omission.
    via_login: bool = False
    # True ONLY when this identity came from the legacy shared
    # device_auth_token fallback (a real, auth-enabled deployment with no
    # per-device pairing yet). Purely a provenance marker, same as
    # via_bearer/via_device -- this identity always has user_id=None, so
    # it's already excluded from the admin bypass by the `identity.user_id
    # and` guard in ws_session_owner_denied regardless of this flag. Gives
    # that identity source an explicit positive marker instead of it being
    # identifiable only as "user_id is None and not unauthenticated",
    # previously documented in prose across two comment blocks (round-2
    # review, minor).
    via_fleet_token: bool = False


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
        return WsIdentity(user_id=None, device_id=None, unauthenticated=True)

    bearer = _bearer_from_subprotocols(websocket)
    if bearer:
        user_id = verify_access_token(bearer)
        if not user_id:
            return None
        user = await user_store.get_by_id(user_id)
        if user is None or user.disabled:
            return None
        return WsIdentity(user_id=user.id, device_id=None, via_bearer=True)

    session_user_id = websocket.session.get("user_id")
    if session_user_id:
        user = await user_store.get_by_id(session_user_id)
        if user is None or user.disabled:
            return None
        return WsIdentity(user_id=user.id, device_id=None, via_login=True)

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
        return WsIdentity(user_id=device.user_id, device_id=device.id, via_device=True)

    if settings.device_auth_token and hmac.compare_digest(token, settings.device_auth_token):
        return WsIdentity(user_id=None, device_id=None, via_fleet_token=True)
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


async def ws_session_owner_denied(session_id: str, identity: WsIdentity) -> bool:
    """True if `session_id` already exists and is not owned by the caller.

    Shared by every WS route that lets a caller resume a session by id --
    conversation.py's /v1/conversation/stream and lugo.py's /v1/lugo/stream
    both resolve identity via resolve_ws_identity and take a caller-supplied
    session id straight into resume_sid; neither ConversationSession.start()
    nor session_store checks ownership on its own. Mirrors sessions.py's
    get_session/_scope_user_id, adapted for the ways a WS identity can
    diverge from an HTTP one:

    - `identity.unauthenticated` (resolve_ws_identity's dev-mode
      short-circuit when auth is disabled) is unscoped/full-access, matching
      current_role()'s identical dev-mode default. The legacy shared
      device_auth_token ALSO resolves to `user_id=None`, but is not
      `unauthenticated` -- it falls through to the plain identity-vs-owner
      comparison below like any other identity, so it can only ever match an
      ownerless session, never a real user's (round-1 review, I1).
    - The admin-role DB lookup is an ALLOW-list keyed on `identity.via_login`
      -- ONLY the browser-cookie-session branch of resolve_ws_identity sets
      it -- rather than a deny-list of excluded sources. A deny-list
      (`not identity.via_bearer`, then `... and not identity.via_device`,
      this function's shape through two rounds of review) fails OPEN for
      every identity source added to resolve_ws_identity later: adding one
      means remembering to both mint a new flag AND extend the deny-list, or
      the new source silently inherits the admin bypass -- exactly how this
      recurred twice already (bearer identities had the bypass until
      round-1 review's I2; paired-device identities had it until round-2
      review). The allow-list fails CLOSED instead: a new branch that
      forgets to set `via_login=True` is denied the bypass by construction,
      never granted it by omission.

    A session that doesn't exist yet is not denied -- it's the normal "start
    a fresh session under this id" path."""
    if identity.unauthenticated:
        return False
    if identity.user_id and identity.via_login:
        from app.services.auth.users import user_store

        caller = await user_store.get_by_id(identity.user_id)
        if caller is not None and caller.role == "admin":
            return False
    from app.services.history.store import session_store

    sess = await session_store.get(session_id)
    return bool(sess and sess.get("user_id") != identity.user_id)
