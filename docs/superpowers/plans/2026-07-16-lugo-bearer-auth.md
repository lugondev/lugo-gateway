# Lugo Bearer Auth (Giai đoạn 0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cho phép API gateway chấp nhận bearer token bên cạnh session cookie, với ràng buộc đường bearer **luôn** là `role="user"` — để Lugo web client (plan riêng, làm sau) có thể gọi API từ domain khác.

**Architecture:** Tách phân giải danh tính khỏi kiểm tra quyền. `AuthGuardMiddleware` giữ nguyên toàn bộ logic gate (`_USER_PREFIXES` / `_ADMIN_PREFIXES`); chỉ nguồn danh tính rộng thêm một đường. Bearer resolve xong thì ghi `request.state.actor`, và `actor.py` đọc `request.state` trước, fallback về session. Admin webui không đổi một dòng.

**Tech Stack:** FastAPI/Starlette, `itsdangerous` 2.2.0 (đã là dependency trực tiếp — **không thêm PyJWT**, ta không cần interop JWT với bên nào), pytest.

**Spec:** `docs/superpowers/specs/2026-07-16-lugo-web-client-design.md`

## Global Constraints

- **Đường bearer không bao giờ đọc role từ token.** Hardcode `role="user"`. Token payload chỉ chứa `user_id`. Không có đường nào trong code từ bearer tới `"admin"`.
- **Không sửa admin webui** (`apps/api_gateway/app/static`) và không migrate nó sang bearer. Session cookie giữ nguyên.
- **Không nới `_ADMIN_PREFIXES` hay `_USER_PREFIXES`.** Danh sách prefix giữ nguyên y hệt.
- **Không đụng `apps/api_gateway/app/api/routes/lugo.py`** — đó là Lugo *device* protocol (ESP32, auth bằng `device_token`), trùng tên nhưng khác hệ.
- Access token TTL **3600s**. Refresh token TTL **30 ngày**.
- WS token đi qua **subprotocol**, không bao giờ qua query string.
- Chạy test: `.venv/bin/pytest <path> -v` (pythonpath đã trỏ `apps/api_gateway` trong pyproject).
- Commit thường xuyên, mỗi task một commit. **Không push** (main tự động deploy prod).

## File Structure

| File | Trách nhiệm |
|---|---|
| `apps/api_gateway/app/services/auth/tokens.py` (mới) | Phát hành & xác minh access/refresh token. Không biết gì về HTTP. |
| `apps/api_gateway/app/core/settings.py` (sửa) | Thêm `effective_session_secret` — một nguồn secret duy nhất. |
| `apps/api_gateway/app/main.py` (sửa) | Dùng `effective_session_secret` thay vì tự sinh. |
| `apps/api_gateway/app/core/actor.py` (sửa) | Đọc `request.state.actor` trước, fallback session. |
| `apps/api_gateway/app/core/auth_guard.py` (sửa) | Đường bearer cho HTTP + subprotocol cho WS. |
| `apps/api_gateway/app/api/routes/auth.py` (sửa) | Endpoint `/api/auth/token`, `/api/auth/refresh`. |
| `apps/api_gateway/app/api/routes/{conversation,livehost,stt}.py` (sửa) | Echo subprotocol khi accept. |

---

### Task 1: Nguồn secret duy nhất

Hiện `main.py:191` tự sinh secret: `_session_secret = settings.session_secret or secrets.token_hex(32)`. Token service cần **cùng** secret đó, nhưng import từ `main` sẽ vòng tròn (main → routes → tokens → main). Đẩy việc phân giải secret xuống `settings`.

**Files:**
- Modify: `apps/api_gateway/app/core/settings.py`
- Modify: `apps/api_gateway/app/main.py:191`
- Test: `tests/unit/test_settings_secret.py` (mới)

**Interfaces:**
- Produces: `settings.effective_session_secret -> str` — secret ký cho cả cookie session lẫn bearer token. Ổn định trong một process.

- [ ] **Step 1: Viết test thất bại**

Tạo `tests/unit/test_settings_secret.py`:

```python
from app.core.settings import settings


def test_effective_secret_uses_configured_value(monkeypatch):
    monkeypatch.setattr(settings, "session_secret", "configured-secret")
    assert settings.effective_session_secret == "configured-secret"


def test_effective_secret_is_stable_across_calls_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "session_secret", "")
    first = settings.effective_session_secret
    second = settings.effective_session_secret
    assert first == second
    assert len(first) >= 32
```

Điểm mấu chốt của test thứ hai: secret sinh ngẫu nhiên phải **ổn định trong process**. Nếu sinh mới mỗi lần gọi thì mọi token phát ra sẽ vô hiệu ngay lập tức.

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `.venv/bin/pytest tests/unit/test_settings_secret.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'effective_session_secret'`

- [ ] **Step 3: Implement**

Ở đầu `apps/api_gateway/app/core/settings.py`, thêm import và hằng module-level:

```python
import secrets

from pydantic_settings import BaseSettings, SettingsConfigDict

# Sinh một lần lúc import -> ổn định trong process, reset khi restart.
# Giữ đúng hành vi cũ của main.py (secrets.token_hex(32) ở module scope).
_GENERATED_SESSION_SECRET = secrets.token_hex(32)
```

Trong class `Settings`, cạnh property `auth_enabled` (khoảng dòng 89), thêm:

```python
    @property
    def effective_session_secret(self) -> str:
        """Secret ký cho cả cookie session lẫn Lugo bearer token. Rỗng ->
        secret ngẫu nhiên mỗi process: session và token cùng reset khi
        restart, đúng bằng hành vi trước đây."""
        return self.session_secret or _GENERATED_SESSION_SECRET
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `.venv/bin/pytest tests/unit/test_settings_secret.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Đổi main.py dùng nguồn chung**

Sửa `apps/api_gateway/app/main.py:191` từ:

```python
_session_secret = settings.session_secret or secrets.token_hex(32)
```

thành:

```python
_session_secret = settings.effective_session_secret
```

Nếu `secrets` không còn được dùng ở chỗ nào khác trong `main.py`, xoá luôn `import secrets`. Kiểm tra bằng: `grep -n "secrets\." apps/api_gateway/app/main.py`

- [ ] **Step 6: Chạy test auth hiện có để xác nhận session cookie không hỏng**

Run: `.venv/bin/pytest tests/unit/test_auth_guard.py tests/unit/test_auth_routes.py -v`
Expected: PASS toàn bộ, không có test nào đổi trạng thái

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/core/settings.py apps/api_gateway/app/main.py tests/unit/test_settings_secret.py
git commit -m "refactor(settings): một nguồn secret duy nhất cho session và token"
```

---

### Task 2: Token service

**Files:**
- Create: `apps/api_gateway/app/services/auth/tokens.py`
- Test: `tests/unit/test_auth_tokens.py` (mới)

**Interfaces:**
- Consumes: `settings.effective_session_secret` (Task 1)
- Produces:
  - `issue_access_token(user_id: str) -> str`
  - `issue_refresh_token(user_id: str) -> str`
  - `verify_access_token(token: str) -> str | None` — trả `user_id`, hoặc `None` nếu sai/hết hạn
  - `verify_refresh_token(token: str) -> str | None`
  - `ACCESS_TTL_SECONDS = 3600`, `REFRESH_TTL_SECONDS = 2592000`

- [ ] **Step 1: Viết test thất bại**

Tạo `tests/unit/test_auth_tokens.py`:

```python
import pytest

from app.services.auth.tokens import (
    ACCESS_TTL_SECONDS,
    issue_access_token,
    issue_refresh_token,
    verify_access_token,
    verify_refresh_token,
)


def test_access_token_roundtrips_user_id():
    token = issue_access_token("user-123")
    assert verify_access_token(token) == "user-123"


def test_refresh_token_roundtrips_user_id():
    token = issue_refresh_token("user-123")
    assert verify_refresh_token(token) == "user-123"


def test_refresh_token_is_not_usable_as_access_token():
    """Salt tách biệt hai loại token. Nếu không, refresh token 30 ngày sẽ
    dùng thay access token được -- biến TTL 1h thành vô nghĩa."""
    refresh = issue_refresh_token("user-123")
    assert verify_access_token(refresh) is None


def test_access_token_is_not_usable_as_refresh_token():
    access = issue_access_token("user-123")
    assert verify_refresh_token(access) is None


def test_tampered_token_is_rejected():
    token = issue_access_token("user-123")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert verify_access_token(tampered) is None


def test_garbage_token_is_rejected():
    assert verify_access_token("not-a-token") is None
    assert verify_access_token("") is None


def test_expired_access_token_is_rejected(monkeypatch):
    import app.services.auth.tokens as tokens_mod

    token = issue_access_token("user-123")
    # Giả lập token phát hành từ quá khứ bằng cách thu TTL về 0 rồi lùi thời gian.
    monkeypatch.setattr(tokens_mod, "ACCESS_TTL_SECONDS", -1)
    assert verify_access_token(token) is None


def test_access_ttl_is_one_hour():
    assert ACCESS_TTL_SECONDS == 3600


def test_token_payload_carries_no_role():
    """Ràng buộc bảo mật cốt lõi: token không mang role. Nếu payload có role
    thì sớm muộn sẽ có người đọc và tin nó."""
    from itsdangerous import URLSafeTimedSerializer

    from app.core.settings import settings

    token = issue_access_token("user-123")
    payload = URLSafeTimedSerializer(
        settings.effective_session_secret, salt="lugo-access"
    ).loads(token)
    assert payload == "user-123"
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `.venv/bin/pytest tests/unit/test_auth_tokens.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.auth.tokens'`

- [ ] **Step 3: Implement**

Tạo `apps/api_gateway/app/services/auth/tokens.py`:

```python
"""Bearer token cho Lugo web client.

Payload cố ý chỉ chứa user_id. Đường bearer luôn là role="user" (xem
auth_guard), nên một role claim sẽ là lời nói dối chờ được tin.
"""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.settings import settings

ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600

# Salt tách access khỏi refresh: cùng secret nhưng không thể dùng thay nhau.
_ACCESS_SALT = "lugo-access"
_REFRESH_SALT = "lugo-refresh"


def _serializer(salt: str) -> URLSafeTimedSerializer:
    # Dựng mỗi lần gọi thay vì cache: settings.effective_session_secret có thể
    # bị monkeypatch trong test, và chi phí dựng là không đáng kể.
    return URLSafeTimedSerializer(settings.effective_session_secret, salt=salt)


def _issue(user_id: str, salt: str) -> str:
    return _serializer(salt).dumps(user_id)


def _verify(token: str, salt: str, max_age: int) -> str | None:
    if not token:
        return None
    try:
        payload = _serializer(salt).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, str):
        return None
    return payload


def issue_access_token(user_id: str) -> str:
    return _issue(user_id, _ACCESS_SALT)


def issue_refresh_token(user_id: str) -> str:
    return _issue(user_id, _REFRESH_SALT)


def verify_access_token(token: str) -> str | None:
    return _verify(token, _ACCESS_SALT, ACCESS_TTL_SECONDS)


def verify_refresh_token(token: str) -> str | None:
    return _verify(token, _REFRESH_SALT, REFRESH_TTL_SECONDS)
```

Lưu ý: `verify_access_token` đọc `ACCESS_TTL_SECONDS` qua module global nên test monkeypatch TTL hoạt động — **không** inline hằng số vào default argument.

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `.venv/bin/pytest tests/unit/test_auth_tokens.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/auth/tokens.py tests/unit/test_auth_tokens.py
git commit -m "feat(auth): token service cho Lugo web client (payload không mang role)"
```

---

### Task 3: Actor đọc `request.state` trước

`current_role()` hiện trả `"admin"` khi có `user_id` mà thiếu `role` — docstring của nó ghi rõ invariant "không được viết user_id mà không viết role". Đường bearer phải **không bao giờ** đi qua nhánh đó.

**Files:**
- Modify: `apps/api_gateway/app/core/actor.py`
- Test: `tests/unit/test_actor.py` (mới)

**Interfaces:**
- Produces: `Actor(user_id: str, role: str)` dataclass; `current_user_id(request)` và `current_role(request)` đọc `request.state.actor` trước, fallback về session.

- [ ] **Step 1: Viết test thất bại**

Tạo `tests/unit/test_actor.py`:

```python
from starlette.requests import Request

from app.core.actor import Actor, current_role, current_user_id


def _request(session: dict, actor: Actor | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "session": session,
        "state": {},
    }
    request = Request(scope)
    if actor is not None:
        request.state.actor = actor
    return request


def test_session_path_unchanged_for_admin():
    request = _request({"user_id": "u1", "role": "admin"})
    assert current_user_id(request) == "u1"
    assert current_role(request) == "admin"


def test_session_missing_role_still_falls_back_to_admin():
    """Hành vi dev-mode cũ giữ nguyên -- task này không sửa nhánh session."""
    request = _request({"user_id": "u1"})
    assert current_role(request) == "admin"


def test_state_actor_takes_precedence_over_session():
    request = _request({"user_id": "u1", "role": "admin"}, actor=Actor(user_id="u2", role="user"))
    assert current_user_id(request) == "u2"
    assert current_role(request) == "user"


def test_state_actor_with_empty_session():
    request = _request({}, actor=Actor(user_id="u2", role="user"))
    assert current_user_id(request) == "u2"
    assert current_role(request) == "user"
```

Test thứ ba là bản chất của cả task: khi có `state.actor`, session **không** được ghi đè nó.

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `.venv/bin/pytest tests/unit/test_actor.py -v`
Expected: FAIL — `ImportError: cannot import name 'Actor' from 'app.core.actor'`

- [ ] **Step 3: Implement**

Thay toàn bộ nội dung `apps/api_gateway/app/core/actor.py`:

```python
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
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `.venv/bin/pytest tests/unit/test_actor.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Chạy toàn bộ test auth để xác nhận không regress**

Run: `.venv/bin/pytest tests/unit/test_auth_guard.py tests/unit/test_auth_routes.py tests/integration/ -v`
Expected: PASS toàn bộ

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/core/actor.py tests/unit/test_actor.py
git commit -m "refactor(actor): tách nguồn danh tính, state.actor ưu tiên hơn session"
```

---

### Task 4: Bearer trong AuthGuardMiddleware

**Files:**
- Modify: `apps/api_gateway/app/core/auth_guard.py`
- Test: `tests/integration/test_bearer_auth.py` (mới)

**Interfaces:**
- Consumes: `verify_access_token` (Task 2), `Actor` (Task 3)
- Produces: request mang `Authorization: Bearer <token>` hợp lệ → `request.state.actor = Actor(user_id, "user")`; `_ADMIN_PREFIXES` luôn trả 403 cho bearer.

- [ ] **Step 1: Viết test thất bại**

Tạo `tests/integration/test_bearer_auth.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.tokens import issue_access_token
from app.services.auth.users import user_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


@pytest.fixture
async def admin_user():
    created = await user_store.create("bearer-admin", "pw12345678", role="admin")
    return created


@pytest.fixture
async def normal_user():
    created = await user_store.create("bearer-user", "pw12345678", role="user")
    return created


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_bearer_grants_user_prefix(client, _with_password, normal_user):
    token = issue_access_token(normal_user["id"])
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code != 401


async def test_no_bearer_is_rejected(client, _with_password):
    resp = client.get("/v1/sessions")
    assert resp.status_code == 401


async def test_invalid_bearer_is_rejected(client, _with_password):
    resp = client.get("/v1/sessions", headers=_auth("garbage"))
    assert resp.status_code == 401


async def test_bearer_for_unknown_user_is_rejected(client, _with_password):
    token = issue_access_token("no-such-user-id")
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code == 401


async def test_bearer_for_disabled_user_is_rejected(client, _with_password, normal_user):
    await user_store.set_fields(normal_user["id"], disabled=True)
    token = issue_access_token(normal_user["id"])
    resp = client.get("/v1/sessions", headers=_auth(token))
    assert resp.status_code == 401


async def test_bearer_never_reaches_admin_prefix_even_for_admin_user(
    client, _with_password, admin_user
):
    """Ràng buộc cốt lõi: token của một user role=admin trong DB vẫn KHÔNG
    mở được đường admin, vì đường bearer hardcode role="user"."""
    token = issue_access_token(admin_user["id"])
    resp = client.get("/v1/system/status", headers=_auth(token))
    assert resp.status_code == 403


async def test_admin_prefix_still_works_via_session_cookie(client, _with_password, admin_user):
    """Admin webui không được hỏng."""
    client.post("/api/auth/login", json={"username": "bearer-admin", "password": "pw12345678"})
    resp = client.get("/v1/system/status")
    assert resp.status_code == 200
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `.venv/bin/pytest tests/integration/test_bearer_auth.py -v`
Expected: FAIL — các test bearer trả 401 vì middleware chưa biết header `Authorization`

- [ ] **Step 3: Implement**

Trong `apps/api_gateway/app/core/auth_guard.py`, thêm import:

```python
from app.core.actor import Actor
from app.services.auth.tokens import verify_access_token
```

Thêm helper trước class `AuthGuardMiddleware`:

```python
async def _bearer_actor(request: Request) -> "Actor | None":
    """Phân giải danh tính từ Authorization: Bearer. LUÔN trả role="user" --
    role trong token không được đọc, vì không tồn tại. Đây là lý do web client
    không thể leo thang lên admin dù người dùng là admin trong DB."""
    from app.services.auth.users import user_store

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    user_id = verify_access_token(token.strip())
    if not user_id:
        return None

    user = await user_store.get_by_id(user_id)
    if user is None or user.disabled:
        return None
    return Actor(user_id=user.id, role="user")
```

Trong `AuthGuardMiddleware.dispatch`, thay dòng:

```python
        user_id = request.session.get("user_id")
```

bằng:

```python
        actor = await _bearer_actor(request)
        if actor is not None:
            request.state.actor = actor
        user_id = actor.user_id if actor is not None else request.session.get("user_id")
```

Trong nhánh `_ADMIN_PREFIXES`, thay:

```python
            if request.session.get("role") != "admin":
```

bằng:

```python
            role = actor.role if actor is not None else request.session.get("role")
            if role != "admin":
```

Không đổi gì khác. `_USER_PREFIXES`, `_ADMIN_PREFIXES`, `_STATIC_ALLOWLIST`, `_NO_AUTH_PREFIXES` giữ nguyên.

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `.venv/bin/pytest tests/integration/test_bearer_auth.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Chạy toàn bộ suite để xác nhận không regress**

Run: `.venv/bin/pytest tests/unit tests/integration -v`
Expected: PASS toàn bộ. Nếu có test fail, **dừng lại** và báo — session cookie không được phép hỏng.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/core/auth_guard.py tests/integration/test_bearer_auth.py
git commit -m "feat(auth): chấp nhận bearer token ở HTTP, luôn gán role=user"
```

---

### Task 5: Endpoint phát hành & làm mới token

**Files:**
- Modify: `apps/api_gateway/app/api/routes/auth.py`
- Test: `tests/unit/test_auth_token_routes.py` (mới)

**Interfaces:**
- Consumes: `issue_access_token`, `issue_refresh_token`, `verify_refresh_token` (Task 2)
- Produces:
  - `POST /api/auth/token` body `{username, password}` → `{success, data: {access_token, refresh_token, expires_in}}`
  - `POST /api/auth/refresh` body `{refresh_token}` → `{success, data: {access_token, expires_in}}`

Cả hai nằm dưới `/api/auth` nên đã được `AuthGuardMiddleware` cho qua (dòng `path.startswith("/api/auth")`) — không cần đổi guard.

- [ ] **Step 1: Viết test thất bại**

Tạo `tests/unit/test_auth_token_routes.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth.tokens import verify_access_token, verify_refresh_token
from app.services.auth.users import user_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
async def user():
    return await user_store.create("token-route-user", "pw12345678", role="user")


async def test_token_endpoint_returns_both_tokens(client, user):
    resp = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert verify_access_token(data["access_token"]) == user["id"]
    assert verify_refresh_token(data["refresh_token"]) == user["id"]
    assert data["expires_in"] == 3600


async def test_token_endpoint_rejects_bad_password(client, user):
    resp = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "wrong-password"},
    )
    assert resp.status_code != 200


async def test_token_endpoint_rejects_disabled_user(client, user):
    await user_store.set_fields(user["id"], disabled=True)
    resp = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    )
    assert resp.status_code != 200


async def test_token_endpoint_does_not_set_session_cookie(client, user):
    """Đường bearer phải tách hẳn khỏi cookie. Nếu endpoint này set cookie thì
    web client vô tình có hai danh tính song song."""
    resp = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    )
    assert "set-cookie" not in {k.lower() for k in resp.headers}


async def test_refresh_returns_new_access_token(client, user):
    tokens = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    ).json()["data"]
    resp = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 200
    assert verify_access_token(resp.json()["data"]["access_token"]) == user["id"]


async def test_refresh_rejects_access_token_as_refresh(client, user):
    tokens = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    ).json()["data"]
    resp = client.post("/api/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert resp.status_code != 200


async def test_refresh_rejects_garbage(client):
    resp = client.post("/api/auth/refresh", json={"refresh_token": "garbage"})
    assert resp.status_code != 200


async def test_refresh_rejects_disabled_user(client, user):
    """Thu hồi không tức thì với access token (TTL 1h), nhưng refresh PHẢI
    kiểm tra lại -- nếu không, user bị vô hiệu hoá vẫn gia hạn được vĩnh viễn."""
    tokens = client.post(
        "/api/auth/token",
        json={"username": "token-route-user", "password": "pw12345678"},
    ).json()["data"]
    await user_store.set_fields(user["id"], disabled=True)
    resp = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code != 200
```

Test cuối là quan trọng nhất: không có nó, refresh token 30 ngày biến việc vô hiệu hoá user thành vô nghĩa.

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `.venv/bin/pytest tests/unit/test_auth_token_routes.py -v`
Expected: FAIL — 404 vì route chưa tồn tại

- [ ] **Step 3: Implement**

Trong `apps/api_gateway/app/api/routes/auth.py`, thêm import:

```python
from app.services.auth.tokens import (
    ACCESS_TTL_SECONDS,
    issue_access_token,
    issue_refresh_token,
    verify_refresh_token,
)
```

Thêm vào cuối file:

```python
class TokenRequest(BaseModel):
    username: str
    password: str


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


class RefreshRequest(BaseModel):
    refresh_token: str


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
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `.venv/bin/pytest tests/unit/test_auth_token_routes.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/auth.py tests/unit/test_auth_token_routes.py
git commit -m "feat(auth): endpoint /api/auth/token và /api/auth/refresh"
```

---

### Task 6: Bearer cho WebSocket qua subprotocol

Token đi qua subprotocol, **không** qua query string (query string bị ghi vào access log và lịch sử proxy). Client gửi `Sec-WebSocket-Protocol: bearer, <token>`; server phải echo lại `bearer` khi accept, nếu không trình duyệt sẽ đóng kết nối.

**Files:**
- Modify: `apps/api_gateway/app/core/auth_guard.py` (hàm `resolve_ws_identity`, thêm `ws_subprotocol`)
- Modify: `apps/api_gateway/app/api/routes/conversation.py:192`
- Modify: `apps/api_gateway/app/api/routes/livehost.py:88`
- Modify: `apps/api_gateway/app/api/routes/stt.py:158`
- Test: `tests/integration/test_ws_bearer_auth.py` (mới)

**KHÔNG** sửa `apps/api_gateway/app/api/routes/lugo.py` — đó là device protocol, dùng `device_token`.

**Interfaces:**
- Consumes: `verify_access_token` (Task 2)
- Produces: `ws_subprotocol(websocket) -> str | None` — trả `"bearer"` nếu client chào subprotocol bearer, ngược lại `None`.

- [ ] **Step 1: Viết test thất bại**

Tạo `tests/integration/test_ws_bearer_auth.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.tokens import issue_access_token
from app.services.auth.users import user_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


@pytest.fixture
async def ws_user():
    return await user_store.create("ws-bearer-user", "pw12345678", role="user")


async def test_ws_accepts_valid_bearer_subprotocol(client, _with_password, ws_user):
    token = issue_access_token(ws_user["id"])
    with client.websocket_connect(
        "/v1/stt/stream", subprotocols=["bearer", token]
    ) as ws:
        assert ws is not None


async def test_ws_rejects_invalid_bearer_subprotocol(client, _with_password):
    with pytest.raises(Exception):  # noqa: B017 -- TestClient raises khi server đóng
        with client.websocket_connect("/v1/stt/stream", subprotocols=["bearer", "garbage"]):
            pass


async def test_ws_rejects_disabled_user(client, _with_password, ws_user):
    await user_store.set_fields(ws_user["id"], disabled=True)
    token = issue_access_token(ws_user["id"])
    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect("/v1/stt/stream", subprotocols=["bearer", token]):
            pass


async def test_ws_rejects_token_in_query_string(client, _with_password, ws_user):
    """Access token KHÔNG được chấp nhận qua query string -- chỉ device_token
    (khác loại, đường riêng) mới đi lối đó."""
    token = issue_access_token(ws_user["id"])
    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(f"/v1/stt/stream?device_token={token}"):
            pass
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `.venv/bin/pytest tests/integration/test_ws_bearer_auth.py -v`
Expected: FAIL — test đầu fail vì `resolve_ws_identity` chưa biết subprotocol nên đóng kết nối

- [ ] **Step 3: Implement helper trong auth_guard.py**

Thêm hàm sau `resolve_ws_identity`:

```python
def _bearer_from_subprotocols(websocket: WebSocket) -> str | None:
    """Client chào: Sec-WebSocket-Protocol: bearer, <token>. Token nằm ở phần
    tử ngay sau "bearer"."""
    protocols = websocket.scope.get("subprotocols") or []
    for index, proto in enumerate(protocols):
        if proto == "bearer" and index + 1 < len(protocols):
            return protocols[index + 1]
    return None


def ws_subprotocol(websocket: WebSocket) -> str | None:
    """Subprotocol mà server phải echo lại khi accept. Trình duyệt đóng kết
    nối nếu server không echo đúng cái nó chào."""
    return "bearer" if _bearer_from_subprotocols(websocket) else None
```

Trong `resolve_ws_identity`, chèn khối bearer **ngay sau** khối `if not settings.auth_enabled:` và **trước** khối `session_user_id`:

```python
    bearer = _bearer_from_subprotocols(websocket)
    if bearer:
        user_id = verify_access_token(bearer)
        if not user_id:
            return None
        user = await user_store.get_by_id(user_id)
        if user is None or user.disabled:
            return None
        return WsIdentity(user_id=user.id, device_id=None)
```

`user_store` đã được import ở đầu hàm; thêm `verify_access_token` vào import cùng chỗ:

```python
    from app.services.auth.tokens import verify_access_token
```

- [ ] **Step 4: Echo subprotocol ở ba route**

Trong `apps/api_gateway/app/api/routes/conversation.py`, thêm `ws_subprotocol` vào import ở dòng 8:

```python
from app.core.auth_guard import resolve_ws_identity, ws_subprotocol
```

Đổi dòng 192 từ `await websocket.accept()` thành:

```python
    await websocket.accept(subprotocol=ws_subprotocol(websocket))
```

Lặp lại y hệt cho:
- `apps/api_gateway/app/api/routes/livehost.py` — import dòng 11, accept dòng 88
- `apps/api_gateway/app/api/routes/stt.py` — import dòng 14, accept dòng 158

**Không** đổi `lugo.py`.

- [ ] **Step 5: Chạy test để xác nhận pass**

Run: `.venv/bin/pytest tests/integration/test_ws_bearer_auth.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Chạy test WS hiện có để xác nhận device và cookie không hỏng**

Run: `.venv/bin/pytest tests/integration/test_ws_auth.py tests/integration/test_lugo_auth.py -v`
Expected: PASS toàn bộ — device protocol và cookie session không được đổi hành vi

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/core/auth_guard.py apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/api/routes/stt.py tests/integration/test_ws_bearer_auth.py
git commit -m "feat(auth): bearer token cho WS qua subprotocol"
```

---

### Task 7: Khoá CORS wildcard + credentials

**Đã xác minh bằng thực nghiệm trước khi viết plan** (preflight `OPTIONS /v1/sessions` từ `https://lugo.example.com`):

```
status: 200
access-control-allow-origin: 'https://lugo.example.com'
access-control-allow-credentials: 'true'
access-control-allow-headers: 'authorization'
```

Hai kết luận:

1. **Preflight đã cho `authorization` qua.** Bearer từ domain khác chạy được ngay, không cần sửa gì cho mục tiêu chính.
2. **Nhưng `main.py:184-185` là cấu hình sai:** `allow_origins=settings.cors_origins_list` (mặc định `["*"]`) đi cùng `allow_credentials=True`. Starlette xử lý tổ hợp này bằng cách **echo lại bất kỳ origin nào** kèm `Allow-Credentials: true` — tức mọi website đều được phép gửi request kèm cookie tới API và đọc kết quả.

Hiện **chưa khai thác được**, vì `SessionMiddleware` đặt `same_site="lax"` (`main.py:195`) nên trình duyệt không gửi cookie kèm request cross-site. Đây là lỗ hổng tiềm ẩn được chặn bởi một lớp phòng thủ duy nhất, không phải sự cố đang xảy ra.

Ta sắp thêm hẳn một client cross-origin, nên siết lại bây giờ là đúng lúc. `allow_credentials=False` an toàn: bearer không dùng cookie, còn admin webui được phục vụ từ chính app này (`/static`) nên là same-origin và không bao giờ đi qua CORS.

**Files:**
- Modify: `apps/api_gateway/app/main.py:185`
- Test: `tests/integration/test_cors_bearer.py` (mới)

**Interfaces:**
- Consumes: không
- Produces: preflight cho phép `authorization`; không bao giờ trả `Allow-Credentials: true` cùng origin wildcard/echo.

- [ ] **Step 1: Viết test**

Tạo `tests/integration/test_cors_bearer.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_preflight_allows_authorization_header(client):
    resp = client.options(
        "/v1/sessions",
        headers={
            "Origin": "https://lugo.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.status_code < 400
    allowed = resp.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allowed or allowed == "*"


def test_arbitrary_origin_is_never_granted_credentials(client):
    """Với allow_origins=["*"], Starlette echo lại mọi origin. Nếu kèm
    Allow-Credentials: true thì bất kỳ website nào cũng đọc được response đã
    xác thực bằng cookie -- hôm nay chỉ SameSite=lax chặn lại. Bearer không
    cần credentials, nên tắt hẳn."""
    resp = client.options(
        "/v1/sessions",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.headers.get("access-control-allow-credentials") != "true"
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `.venv/bin/pytest tests/integration/test_cors_bearer.py -v`
Expected: `test_preflight_allows_authorization_header` PASS (đã đúng sẵn),
`test_arbitrary_origin_is_never_granted_credentials` **FAIL** — nhận
`access-control-allow-credentials: 'true'`

- [ ] **Step 3: Implement**

Sửa `apps/api_gateway/app/main.py:182-188` từ:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

thành:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    # Tắt credentials: Lugo web client dùng bearer (không cookie), còn admin
    # webui được phục vụ từ chính app này (/static) nên same-origin và không
    # đi qua CORS. Bật credentials cùng allow_origins=["*"] sẽ khiến Starlette
    # echo lại mọi origin kèm Allow-Credentials -- mọi website đọc được
    # response xác thực bằng cookie, chỉ còn SameSite=lax chặn.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Chạy lại: `.venv/bin/pytest tests/integration/test_cors_bearer.py -v` → PASS (2 passed)

- [ ] **Step 4: Chạy toàn bộ suite lần cuối**

Run: `.venv/bin/pytest tests/unit tests/integration -v`
Expected: PASS toàn bộ. So sánh số test fail với baseline trước khi bắt đầu plan — không được có test nào mới fail.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_cors_bearer.py apps/api_gateway/app/main.py
git commit -m "fix(cors): không cấp credentials cho origin tuỳ ý

allow_origins=['*'] + allow_credentials=True khiến Starlette echo lại mọi
origin kèm Allow-Credentials. Chưa khai thác được nhờ SameSite=lax, nhưng
web client cross-origin sắp tới dùng bearer nên credentials là thừa."
```

---

## Xác minh cuối (không phải test tự động)

- [ ] **Chạy gateway thật và thử bằng curl**

```bash
# Terminal 1
.venv/bin/uvicorn app.main:app --app-dir apps/api_gateway --port 8000

# Terminal 2 -- lấy token
curl -s -X POST localhost:8000/api/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"<user có sẵn>","password":"<password>"}'

# Gọi endpoint user -- kỳ vọng 200
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/sessions \
  -H "Authorization: Bearer <access_token>"

# Gọi endpoint admin -- kỳ vọng 403 KỂ CẢ khi user là admin
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/system/status \
  -H "Authorization: Bearer <access_token>"
```

- [ ] **Mở admin webui trong trình duyệt, đăng nhập, kiểm tra các tab vẫn chạy** — đây là thứ dễ hỏng nhất mà test không phủ hết.

## Ngoài phạm vi plan này

- Web client (React SPA) — plan riêng, viết sau khi task 1-7 xong và verify
- Migrate admin webui sang bearer
- Denylist thu hồi token tức thì
- Sửa gitlink hỏng của `esp32-assistant`
