import hmac

from fastapi import APIRouter, HTTPException, Request

from app.core.client_ip import client_ip
from app.core.errors import AuthError
from app.core.rate_limit import SlidingWindowRateLimiter
from app.schemas.auth import (
    IntrospectRequest,
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenRequest,
)
from app.services.auth.tokens import (
    ACCESS_TTL_SECONDS,
    issue_access_token,
    issue_refresh_token,
    verify_plugin_token,
    verify_refresh_token,
)
from app.services.auth.users import user_store
from app.services.plugins.store import plugin_store

router = APIRouter(prefix="/api/auth", tags=["auth"])

# These three routes are the app's only unauthenticated write surface
# (auth_guard._NO_AUTH_PREFIXES) and until now had no limiter of any kind, so a
# password could be guessed at full speed forever. Two keys, because they stop
# different attacks:
#
#  - per (ip, username): a targeted brute force against one account. Tight,
#    since a real person mistyping their own password does not need 10 tries
#    a minute.
#  - per ip: credential SPRAYING, one guess each across many usernames, which
#    the per-account key alone never sees. Deliberately loose: with
#    trusted_proxy_hops unset (the default) every client behind the reverse
#    proxy shares this key, and a tight cap there would let one attacker spend
#    the whole deployment's login budget. 300/min still crushes a sweep --
#    PBKDF2 costs ~100-300ms of server CPU per attempt anyway -- while sitting
#    far above anything a real user population reaches. Set TRUSTED_PROXY_HOPS
#    to make it genuinely per-client.
#
# Only FAILED attempts are charged (see `charge` calls below): charging a
# success would let anyone lock a user out of their own account by burning the
# budget for them.
LOGIN_MAX_FAILURES_PER_ACCOUNT = 10
LOGIN_MAX_FAILURES_PER_IP = 300
SIGNUP_MAX_PER_IP = 20

login_account_limiter = SlidingWindowRateLimiter(
    max_events=LOGIN_MAX_FAILURES_PER_ACCOUNT, window_seconds=60.0
)
login_ip_limiter = SlidingWindowRateLimiter(
    max_events=LOGIN_MAX_FAILURES_PER_IP, window_seconds=60.0
)
signup_limiter = SlidingWindowRateLimiter(
    max_events=SIGNUP_MAX_PER_IP, window_seconds=60.0
)

_TOO_MANY = "too many attempts, try again shortly"


def _password_attempt_keys(request: Request, username: str) -> tuple[str, str]:
    ip = client_ip(request)
    return f"{ip}:{username}", ip


async def _authenticate(request: Request, username: str, password: str):
    """Shared body of /login and /token: rate-limit, verify, charge on failure.

    Raises AuthError on bad credentials and HTTPException(429) when the caller
    is over budget. Returns the user otherwise.
    """
    account_key, ip_key = _password_attempt_keys(request, username)
    if not login_account_limiter.check(account_key) or not login_ip_limiter.check(ip_key):
        raise HTTPException(status_code=429, detail=_TOO_MANY)

    user = await user_store.verify_login(username, password)
    if user is None or user.disabled:
        login_account_limiter.charge(account_key)
        login_ip_limiter.charge(ip_key)
        # Same message for a bad password, an unknown username and a disabled
        # account -- pair it with verify_login's constant-time miss path or the
        # three become distinguishable again.
        raise AuthError("invalid username or password")
    return user


@router.post("/signup")
async def signup(body: SignupRequest, request: Request) -> dict:
    if not signup_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail=_TOO_MANY)
    created = await user_store.create(body.username, body.password, role="user")
    return {"success": True, "data": {"username": created["username"]}}


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> dict:
    user = await _authenticate(request, body.username, body.password)
    request.session["user_id"] = user.id
    # Kept for compatibility with anything still reading it; the auth guard no
    # longer does -- it re-reads the role from the DB on every request, so a
    # demotion takes effect immediately (see core/auth_guard._session_actor).
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
async def token(body: TokenRequest, request: Request) -> dict:
    """Phát hành bearer token cho Lugo web client. Cố ý KHÔNG set session
    cookie: đây là đường auth tách biệt hoàn toàn với admin webui.

    Cùng limiter với /login, chung key: nếu không, đây là đúng cùng một phép
    thử mật khẩu ở một URL khác và người tấn công chỉ việc đổi endpoint."""
    user = await _authenticate(request, body.username, body.password)
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


@router.post("/introspect")
async def introspect(body: IntrospectRequest, request: Request) -> dict:
    """Đổi vé plugin lấy user_id. Người gọi là plugin, không phải người dùng.

    Endpoint này nằm dưới /api/auth, tức là trong _NO_AUTH_PREFIXES (đăng nhập
    buộc phải ở đó). Nên nó tự xác thực bằng Plugin.secret: thiếu bước này, bất
    kỳ ai đọc được vé trong access log đều tra ra được user_id.

    Plugin lạ, plugin đã tắt, và secret sai đều trả về đúng một phản hồi 401 --
    một người gọi chưa xác thực không được phép dò xem plugin nào đang đăng ký.
    """
    entry = plugin_store.get(body.plugin)
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if (
        entry is None
        or not entry.enabled
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(presented.strip(), entry.secret)
    ):
        raise AuthError("invalid plugin credentials")
    user_id = verify_plugin_token(body.token, body.plugin)
    return {"success": True, "data": {"active": user_id is not None, "user_id": user_id}}
