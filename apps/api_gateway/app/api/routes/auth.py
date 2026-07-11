from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.errors import AuthError
from app.services.auth.users import user_store

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignupRequest(BaseModel):
    username: str
    password: str


@router.post("/signup")
async def signup(body: SignupRequest) -> dict:
    created = await user_store.create(body.username, body.password, role="user")
    return {"success": True, "data": {"username": created["username"]}}


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> dict:
    user = await user_store.verify_login(body.username, body.password)
    if user is None or user.disabled:
        raise AuthError("invalid username or password")
    request.session["user_id"] = user.id
    request.session["role"] = user.role
    # TODO(task-5): AuthGuardMiddleware / ws_authenticated still gate on this
    # legacy flag; drop once the middleware role split reads user_id instead.
    request.session["authenticated"] = True
    return {"success": True}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"success": True}


@router.get("/status")
async def status(request: Request) -> dict:
    user_id = request.session.get("user_id")
    if not user_id:
        return {"authenticated": False}
    user = await user_store.get_by_id(user_id)
    if user is None or user.disabled:
        request.session.clear()
        return {"authenticated": False}
    return {
        "authenticated": True,
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "can_use_testing": user.can_use_testing,
    }
