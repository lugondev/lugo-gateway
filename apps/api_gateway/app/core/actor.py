"""Accessor an toàn cho danh tính đang gọi request.

Hai nguồn danh tính:
- request.state.actor -- do AuthGuardMiddleware đặt khi request mang bearer
  token. Luôn tường minh cả user_id lẫn role.
- cookie session -- đường của admin webui. Fallback khi không có actor.

Nhánh session fallback role thiếu thành "admin": khi settings.admin_password
rỗng (dev mode), AuthGuardMiddleware no-op cho mọi prefix nên route có thể
chạy với session hoàn toàn rỗng. Coi role thiếu là "admin" khớp với hành vi
dev-mode thực tế (một caller không xác thực, toàn quyền) thay vì crash.
"""

from dataclasses import dataclass

from starlette.requests import Request


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str


def _state_actor(request: Request) -> Actor | None:
    return getattr(request.state, "actor", None)


def current_user_id(request: Request) -> str | None:
    actor = _state_actor(request)
    if actor is not None:
        return actor.user_id
    return request.session.get("user_id")


def current_role(request: Request) -> str:
    actor = _state_actor(request)
    if actor is not None:
        return actor.role
    # Invariant mà fallback này phụ thuộc: không được ghi "user_id" vào session
    # mà không ghi "role" cùng chỗ (hôm nay chỉ /api/auth/login làm việc đó, và
    # nó luôn set cả hai -- xem api/routes/auth.py). Đường bearer KHÔNG đi qua
    # đây: nó luôn đặt request.state.actor với role tường minh.
    return request.session.get("role") or "admin"
