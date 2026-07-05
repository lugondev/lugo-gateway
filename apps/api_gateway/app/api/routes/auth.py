import hmac

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.errors import AuthError
from app.core.settings import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> dict:
    if not settings.admin_password or not hmac.compare_digest(body.password, settings.admin_password):
        raise AuthError("invalid password")
    request.session["authenticated"] = True
    return {"success": True}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"success": True}


@router.get("/status")
async def status(request: Request) -> dict:
    return {"authenticated": bool(request.session.get("authenticated"))}
