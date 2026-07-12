"""Safe accessors for the acting identity on a request. Both fall back
gracefully when settings.admin_password is unset (dev mode): AuthGuardMiddleware
no-ops for every prefix in that case, so a route can run with a completely
empty session -- request.session["role"] would raise KeyError there. Treating
a missing role as "admin" matches today's actual dev-mode behavior (a single
unauthenticated caller has unrestricted access) rather than crashing."""

from starlette.requests import Request


def current_user_id(request: Request) -> str | None:
    return request.session.get("user_id")


def current_role(request: Request) -> str:
    # Invariant this fallback depends on: nothing may ever write "user_id" into
    # the session without also writing "role" in the same place (today only
    # /api/auth/login does either, and it always sets both together -- see
    # api/routes/auth.py). If that invariant is ever broken by a future auth
    # path, a session with user_id but no role would incorrectly resolve to
    # "admin" here. Keep both fields set atomically wherever session identity
    # is established.
    return request.session.get("role") or "admin"
