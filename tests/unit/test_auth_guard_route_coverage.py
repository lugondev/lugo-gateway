"""Every mounted HTTP path must be explicitly classified by the auth guard.

The default-deny floor added in `AuthGuardMiddleware.dispatch` fails closed in
the *authentication* dimension (an unclassified path needs a logged-in user),
but its floor is USER, not DENY. So it does NOT fail closed in the *role*
dimension: a future admin-only router mounted without an `_ADMIN_PREFIXES`
entry would be silently reachable by every logged-in user.

This test closes that gap the only way a middleware-based guard can -- by
walking the real route table and asserting nobody added a path that no tuple
covers. If it fails, the fix is to classify the named path in
`apps/api_gateway/app/core/auth_guard.py`, not to relax the assertion.
"""

import re

import pytest
from starlette.routing import Mount, WebSocketRoute

from app.core.auth_guard import (
    _ADMIN_PREFIXES,
    _NO_AUTH_PREFIXES,
    _PUBLIC_PATHS,
    _STATIC_ALLOWLIST,
    _USER_PREFIXES,
    _matches,
)
from app.main import app

# A path parameter matches any single non-empty segment; substituting a literal
# is enough for prefix classification (`/v1/profiles/{name}/memories` classifies
# identically to `/v1/profiles/x/memories`).
_PARAM = re.compile(r"\{[^}]+\}")

# StaticFiles mounts have no child routes to enumerate, so probe one.
_MOUNT_PROBE = "probe.bin"


def _concrete(path: str) -> str:
    return _PARAM.sub("x", path)


def _mounted_http_paths() -> list[tuple[str, str]]:
    """(path, source) for every path AuthGuardMiddleware can actually see.

    WebSocket routes are deliberately excluded: `BaseHTTPMiddleware.__call__`
    returns early for any `scope["type"] != "http"`, so the guard never runs for
    them at all -- their auth is `resolve_ws_identity`, tested separately in
    test_auth_guard.py. Do NOT "fix" this by including them; a WS path appearing
    here would be asserting something the middleware does not do.

    FastAPI wraps `include_router()`ed routers in an `_IncludedRouter` object
    rather than splicing their routes into `app.routes`, so a naive walk of
    `app.routes` silently sees almost nothing. Descending through
    `.original_router.routes` is required.
    """
    found: list[tuple[str, str]] = []

    def walk(routes) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
                continue
            if isinstance(route, WebSocketRoute):
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            if isinstance(route, Mount):
                # The bare mount root (e.g. "/static") is not asserted here: it
                # is never a real asset and falls to the default-deny floor,
                # which is user-level -- safe. What matters is the children.
                found.append((f"{path}/{_MOUNT_PROBE}", f"mount {path}"))
                continue
            found.append((_concrete(path), "route"))

    walk(app.routes)
    return found


def _classify(path: str) -> str | None:
    """Mirrors the branch order of AuthGuardMiddleware.dispatch."""
    if path in _PUBLIC_PATHS:
        return "public"
    if path in _STATIC_ALLOWLIST:
        return "public"
    if _matches(path, _NO_AUTH_PREFIXES):
        return "public"
    if _matches(path, _USER_PREFIXES):
        return "user"
    if _matches(path, _ADMIN_PREFIXES):
        return "admin"
    return None


def test_the_route_walker_actually_finds_the_app():
    """Guard against the walker silently returning nothing (which would make
    the coverage test below vacuously pass forever)."""
    paths = {p for p, _ in _mounted_http_paths()}
    # 97 distinct HTTP paths at the time of writing (125 route entries, minus
    # the 4 WebSocket routes, deduped across methods, plus the 2 mount probes).
    # A broken walker returns a handful, not ~90.
    assert len(paths) > 90, f"walker found only {len(paths)} paths -- it is broken"
    # Spot-check one path from each of the four shapes that are easy to miss.
    assert "/v1/system/status" in paths  # router declaring prefix="/v1"
    assert "/v1/models/recommend" in paths  # second router declaring prefix="/v1"
    assert "/agents-docs" in paths  # router declaring no prefix at all
    assert f"/artifacts/{_MOUNT_PROBE}" in paths  # StaticFiles mount


def test_every_mounted_path_is_classified():
    unclassified = sorted(
        f"{path}  (from {source})"
        for path, source in _mounted_http_paths()
        if _classify(path) is None
    )
    assert not unclassified, (
        "These HTTP paths are mounted but no auth_guard tuple covers them, so "
        "they fall through to the default-deny floor -- which requires a login "
        "but grants EVERY logged-in user access, regardless of role.\n"
        "Classify each one in apps/api_gateway/app/core/auth_guard.py: add it "
        "to _USER_PREFIXES, _ADMIN_PREFIXES, _NO_AUTH_PREFIXES or _PUBLIC_PATHS.\n"
        + "\n".join(f"  - {entry}" for entry in unclassified)
    )


def test_no_admin_prefix_is_shadowed_by_a_user_prefix():
    """_USER_PREFIXES is checked before _ADMIN_PREFIXES, so a user entry that
    covers an admin entry silently downgrades it. That ordering is deliberate
    (it is how the /v1/usage/me and /v1/model_registry/options carve-outs work),
    but it only works one way: the carve-out must be *narrower* than the admin
    rule. A future "/v1/profiles/admin-purge" in _ADMIN_PREFIXES would sit
    inside the "/v1/profiles" user rule and never be admin-gated."""
    shadowed = sorted(prefix for prefix in _ADMIN_PREFIXES if _matches(prefix, _USER_PREFIXES))
    assert not shadowed, (
        "These _ADMIN_PREFIXES entries are swallowed by a broader _USER_PREFIXES "
        f"entry and are therefore NOT admin-gated: {shadowed}"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # The four surfaces this task closed.
        ("/v1/events/sessions/abc", "user"),
        ("/v1/events/jobs/abc", "user"),
        ("/artifacts/deadbeef.wav", "user"),
        ("/agents-docs", "admin"),
        ("/openapi.json", "admin"),
        ("/docs", "admin"),
        ("/docs/oauth2-redirect", "admin"),
        ("/redoc", "admin"),
        # Public surface stays public.
        ("/", "public"),
        ("/health", "public"),
        ("/api/auth/login", "public"),
        ("/static/login.html", "public"),
        ("/v1/devices/pair/init", "public"),
        # Carve-outs still resolve on the correct side of their admin rule.
        ("/v1/usage/me", "user"),
        ("/v1/usage/summary", "admin"),
        ("/v1/model_registry/options", "user"),
        ("/v1/model_registry/prices", "admin"),
        ("/v1/devices/mine", "user"),
        ("/v1/devices", "admin"),
    ],
)
def test_spot_check_classifications(path, expected):
    assert _classify(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/api/authz/whatever",
        "/api/authenticate",
        "/api/auth-bypass",
    ],
)
def test_api_auth_carve_out_stops_at_a_segment_boundary(path):
    """Regression: the /api/auth bypass used to be a bare
    `path.startswith("/api/auth")`, so anything mounted at a sibling path with
    the same textual prefix would have been served anonymously."""
    assert _classify(path) is None, f"{path} must not inherit the /api/auth carve-out"
    assert _classify("/api/auth/login") == "public"
