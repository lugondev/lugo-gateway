from fastapi import APIRouter, Request

from app.core.errors import AuthError
from app.schemas.auth import LoginRequest, RefreshRequest, SignupRequest, TokenRequest
from app.services.auth.tokens import (
    ACCESS_TTL_SECONDS,
    issue_access_token,
    issue_refresh_token,
    verify_refresh_token,
)
from app.services.auth.users import user_store

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/signup")
async def signup(body: SignupRequest) -> dict:
    created = await user_store.create(body.username, body.password, role="user")
    return {"success": True, "data": {"username": created["username"]}}


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> dict:
    user = await user_store.verify_login(body.username, body.password)
    if user is None or user.disabled:
        raise AuthError("invalid username or password")
    request.session["user_id"] = user.id
    request.session["role"] = user.role
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


@router.post("/token")
async def token(body: TokenRequest) -> dict:
    """Phát hành bearer token cho Lugo web client. Cố ý KHÔNG set session
    cookie: đây là đường auth tách biệt hoàn toàn với admin webui."""
    user = await user_store.verify_login(body.username, body.password)
    if user is None or user.disabled:
        raise AuthError("invalid username or password")
    return {
        "success": True,
        "data": {
            "access_token": issue_access_token(user.id),
            "refresh_token": issue_refresh_token(user.id),
            "expires_in": ACCESS_TTL_SECONDS,
        },
    }


@router.post("/refresh")
async def refresh(body: RefreshRequest) -> dict:
    user_id = verify_refresh_token(body.refresh_token)
    if not user_id:
        raise AuthError("invalid refresh token")
    # Kiểm tra lại user ở mỗi lần refresh: đây là chốt chặn duy nhất khiến
    # việc vô hiệu hoá user có hiệu lực, vì access token không tra cứu gì.
    user = await user_store.get_by_id(user_id)
    if user is None or user.disabled:
        raise AuthError("invalid refresh token")
    return {
        "success": True,
        "data": {
            "access_token": issue_access_token(user.id),
            "expires_in": ACCESS_TTL_SECONDS,
        },
    }
