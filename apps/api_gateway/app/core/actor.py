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

from fastapi import HTTPException
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


def scope_user_id(request: Request) -> str | None:
    """None cho admin, hoặc khi auth tắt hẳn (dev mode -- xem current_role ở
    trên): không lọc gì cả, đúng như hành vi hiện tại. Ngược lại trả id của
    chính caller, nên user thường chỉ thấy tài nguyên của mình.

    Các route giữ alias `_scope_user_id` trỏ vào đây thay vì tự định nghĩa
    lại: giữ tên module-level để test vẫn monkeypatch được từng route riêng."""
    return None if current_role(request) == "admin" else current_user_id(request)


def require_admin(request: Request) -> None:
    """403 nếu caller không phải admin. Dùng cho những route nằm dưới prefix
    của user nhưng thao tác lên state dùng chung toàn server (LLM mặc định
    trong Model Registry, write surface của MCP servers) -- gate inline ở đây
    để đường public không đổi, thay vì dời route sang prefix admin."""
    if current_role(request) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
