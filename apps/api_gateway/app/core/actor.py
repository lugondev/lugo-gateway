"""Accessor an toàn cho danh tính đang gọi request.

Nguồn danh tính DUY NHẤT khi auth bật: request.state.actor, do
AuthGuardMiddleware đặt cho CẢ hai đường vào -- bearer (_bearer_actor) lẫn
cookie session (_session_actor). Cả hai đều tra lại user trong DB ở mỗi
request, nên `role` ở đây luôn là role hiện tại, không phải role đã ký vào
cookie lúc đăng nhập. Trước đây nhánh cookie đọc thẳng session["role"]: hạ
quyền một admin không hề gỡ được quyền admin cho tới khi cookie hết hạn.

Fallback bên dưới (session, rồi "admin") CHỈ còn với chể độ dev: khi
settings.auth_enabled False, AuthGuardMiddleware no-op nên không có actor nào
được đặt và route chạy với session rỗng. Coi role thiếu là "admin" khớp với
hành vi dev-mode thực tế (một caller không xác thực, toàn quyền) thay vì crash.
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
    # Không còn đường xác thực nào rơi tới đây khi auth bật: guard đặt
    # request.state.actor cho cả bearer lẫn cookie (xem docstring ở trên).
    # Còn lại đúng nhánh dev-mode, nơi session rỗng và "admin" là mặc định.
    return request.session.get("role") or "admin"
