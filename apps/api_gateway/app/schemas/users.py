from pydantic import BaseModel


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


class UpdateUserRequest(BaseModel):
    disabled: bool | None = None
    role: str | None = None
    can_use_testing: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str
