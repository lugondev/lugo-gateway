# Identity, Auth & Device Pairing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single shared admin password / device token with real per-user accounts (admin + user roles), role-based route authorization, a periodic disabled/revoked re-check on live WS connections, a device-pairing flow so ESP32/RPi connections resolve to a specific owner, and the UI (rebranded "Lugo"/"Lugo BOT") to drive all of it.

**Architecture:** Two new SQLAlchemy tables (`users`, `devices`) on the existing async engine, alongside `ChatSession`/`ChatMessage`. Two new service modules (`app/services/auth/`) following the existing `SessionStore`-style thin-async-class pattern. `AuthGuardMiddleware` gains a role-aware prefix split; `ws_authenticated()` is replaced by an identity-resolving `resolve_ws_identity()` used by all four WS routes. A new shared `IdentityWatchdog` primitive, modeled on `lugo.py`'s existing idle-timeout watchdog, adds periodic disabled/revoked re-checks to long-lived WS sessions. Device pairing is a short-lived in-memory registry (same pattern as `livehost_registry`) plus two new DB-backed routes. UI work reuses the existing `styles.css` theme and per-tab JS-module pattern (`profiles.js`, `mcp-servers.js`) — no new palette.

**Tech Stack:** FastAPI, SQLAlchemy (async engine, `aiosqlite`), Starlette `SessionMiddleware`/`BaseHTTPMiddleware`, stdlib `hashlib`/`hmac`/`secrets` (no new dependency), pytest + pytest-asyncio (`asyncio_mode = "auto"`), vanilla ES modules (no framework, no JS test tooling).

## Global Constraints

- No new Python dependency for password hashing — stdlib `hashlib.pbkdf2_hmac` only (matches the codebase's existing `hmac.compare_digest` precedent for the device token).
- No new CSS palette/tokens — reuse `app/static/styles.css`'s existing `--bg-1`/`--bg-2`/`--accent`/`--accent-2`/`--text`/`--muted`/`--danger` variables and existing component classes (`.model-row`, `.mini`, `.danger`, `.hint`, `.section`, `.seg`). This round is a text-level rename ("Speech Text Transformer" → "Lugo" / "Lugo BOT"), not a redesign.
- All new UI copy is in English, matching every existing nav label/button in `index.html`/`login.html` (Vietnamese is reserved for bot speech content, e.g. `settings.conversation_goodbye_text` — not admin/UI chrome).
- `settings.admin_password` and `settings.device_auth_token` both remain valid as legacy fallbacks (bootstrap admin source; WS legacy branch) — neither is removed in this plan.
- `serial` conflicts on device re-pairing always require an explicit revoke first — never auto-reclaim, never silently duplicate.
- The periodic disabled/revoked re-check only applies to sessions that resolved a `user_id`/`device_id`; a connection still using the legacy shared `device_auth_token` is exempt (no owner to check).
- Every new/changed backend behavior gets a pytest test in `tests/unit/` or `tests/integration/` (this repo has no JS test tooling — frontend tasks are verified manually, not skipped).
- Run all commands from the repo root (`/Users/lugon/code/speech-text-transformer`); `pythonpath = ["apps/api_gateway"]` is already configured in `pyproject.toml`, so `app.*` imports resolve without a `cd`.

---

## Task 1: `User` model + password hashing utility

**Files:**
- Create: `apps/api_gateway/app/services/auth/__init__.py` (empty)
- Create: `apps/api_gateway/app/services/auth/password.py`
- Modify: `apps/api_gateway/app/services/db/models.py`
- Test: `tests/unit/test_password.py`

**Interfaces:**
- Produces: `hash_password(password: str) -> str`, `verify_password(password: str, encoded: str) -> bool` (from `app.services.auth.password`); `User` SQLAlchemy model (`app.services.db.models.User`) with columns `id, username, password_hash, role, can_use_testing, disabled, created_at`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_password.py
from app.services.auth.password import hash_password, verify_password


def test_hash_then_verify_round_trip():
    encoded = hash_password("correct-horse")
    assert verify_password("correct-horse", encoded) is True


def test_verify_rejects_wrong_password():
    encoded = hash_password("correct-horse")
    assert verify_password("wrong", encoded) is False


def test_hash_is_salted_differently_each_time():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a) is True
    assert verify_password("same-password", b) is True


def test_verify_rejects_malformed_hash():
    assert verify_password("anything", "not-a-valid-hash") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_password.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.auth'`

- [ ] **Step 3: Write the implementation**

```python
# apps/api_gateway/app/services/auth/password.py
"""Stdlib PBKDF2 password hashing -- no new dependency, matching the codebase's
existing preference for stdlib crypto primitives (hmac.compare_digest for the
device token). Encoded format: "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>",
with the iteration count stored alongside the hash so the cost factor can be
raised later without breaking already-stored hashes."""

from __future__ import annotations

import hashlib
import hmac
import os

_SCHEME = "pbkdf2_sha256"
_ITERATIONS = 600_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_SCHEME}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations_s, salt_hex, hash_hex = encoded.split("$")
        if scheme != _SCHEME:
            return False
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)
```

```python
# apps/api_gateway/app/services/auth/__init__.py
```

In `apps/api_gateway/app/services/db/models.py`, change the existing import line to add `Boolean`:

```python
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
```

Append at the end of the file:

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="user")
    can_use_testing: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    serial: Mapped[str] = mapped_column(String(128), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
```

(`Device` is added here too, not in a later task, since it lives in the same file and this avoids a second edit pass over `models.py`; `DeviceStore` itself is still built in Task 7.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_password.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/auth/__init__.py apps/api_gateway/app/services/auth/password.py apps/api_gateway/app/services/db/models.py tests/unit/test_password.py
git commit -m "feat(auth): add User/Device models + stdlib password hashing"
```

---

## Task 2: `UserStore` service

**Files:**
- Create: `apps/api_gateway/app/services/auth/users.py`
- Modify: `apps/api_gateway/app/core/errors.py`
- Test: `tests/unit/test_user_store.py`

**Interfaces:**
- Consumes: `hash_password`, `verify_password` (Task 1); `app.services.db.engine.db_session`; `app.services.db.models.User`.
- Produces: `UserStore` class + singleton `user_store` (`app.services.auth.users.user_store`) with `async count()`, `async create(username, password, role="user") -> dict`, `async get_by_username(username) -> User | None`, `async get_by_id(user_id) -> User | None`, `async verify_login(username, password) -> User | None`, `async list() -> list[dict]`, `async set_fields(user_id, **fields) -> dict | None`, `async reset_password(user_id, new_password) -> bool`. `UsernameTakenError` (`app.core.errors.UsernameTakenError`, status 409).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_user_store.py
import pytest

from app.core.errors import UsernameTakenError
from app.services.auth.users import UserStore


@pytest.fixture
def store():
    return UserStore()


@pytest.mark.asyncio
async def test_create_and_get_by_username(store):
    await store.create("toan", "s3cret", role="admin")
    user = await store.get_by_username("toan")
    assert user is not None
    assert user.role == "admin"
    assert user.disabled is False
    assert user.can_use_testing is False


@pytest.mark.asyncio
async def test_create_duplicate_username_raises(store):
    await store.create("toan", "s3cret")
    with pytest.raises(UsernameTakenError):
        await store.create("toan", "different")


@pytest.mark.asyncio
async def test_verify_login_correct_and_wrong_password(store):
    await store.create("toan", "s3cret")
    ok = await store.verify_login("toan", "s3cret")
    assert ok is not None and ok.username == "toan"
    assert await store.verify_login("toan", "wrong") is None
    assert await store.verify_login("nobody", "s3cret") is None


@pytest.mark.asyncio
async def test_list_and_count(store):
    assert await store.count() == 0
    await store.create("a", "pw1")
    await store.create("b", "pw2")
    assert await store.count() == 2
    usernames = sorted(u["username"] for u in await store.list())
    assert usernames == ["a", "b"]


@pytest.mark.asyncio
async def test_set_fields_updates_disabled_role_testing(store):
    created = await store.create("toan", "s3cret")
    updated = await store.set_fields(created["id"], disabled=True, can_use_testing=True)
    assert updated["disabled"] is True
    assert updated["can_use_testing"] is True
    assert await store.set_fields("missing-id", disabled=True) is None


@pytest.mark.asyncio
async def test_reset_password_changes_login(store):
    created = await store.create("toan", "old-pw")
    assert await store.reset_password(created["id"], "new-pw") is True
    assert await store.verify_login("toan", "old-pw") is None
    assert await store.verify_login("toan", "new-pw") is not None
    assert await store.reset_password("missing-id", "x") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_user_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.auth.users'`

- [ ] **Step 3: Write the implementation**

Add to `apps/api_gateway/app/core/errors.py` (after `AuthError`):

```python
class UsernameTakenError(AppError):
    """Raised on signup/create-user when the username already exists."""

    status_code = 409
```

```python
# apps/api_gateway/app/services/auth/users.py
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.errors import UsernameTakenError
from app.services.auth.password import hash_password, verify_password
from app.services.db.engine import db_session
from app.services.db.models import User


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "can_use_testing": u.can_use_testing,
        "disabled": u.disabled,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


class UserStore:
    async def count(self) -> int:
        async with db_session() as s:
            rows = (await s.execute(select(User.id))).scalars().all()
            return len(rows)

    async def create(self, username: str, password: str, role: str = "user") -> dict:
        async with db_session() as s:
            existing = (
                await s.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()
            if existing is not None:
                raise UsernameTakenError(f"username '{username}' is already taken")
            row = User(
                id=str(uuid.uuid4()), username=username,
                password_hash=hash_password(password), role=role,
            )
            s.add(row)
            await s.commit()
            return _user_dict(row)

    async def get_by_username(self, username: str) -> User | None:
        async with db_session() as s:
            return (
                await s.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()

    async def get_by_id(self, user_id: str) -> User | None:
        async with db_session() as s:
            return await s.get(User, user_id)

    async def verify_login(self, username: str, password: str) -> User | None:
        user = await self.get_by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            return None
        return user

    async def list(self) -> list[dict]:
        async with db_session() as s:
            rows = (await s.execute(select(User).order_by(User.username))).scalars().all()
            return [_user_dict(u) for u in rows]

    async def set_fields(self, user_id: str, **fields) -> dict | None:
        async with db_session() as s:
            row = await s.get(User, user_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await s.commit()
            return _user_dict(row)

    async def reset_password(self, user_id: str, new_password: str) -> bool:
        async with db_session() as s:
            row = await s.get(User, user_id)
            if row is None:
                return False
            row.password_hash = hash_password(new_password)
            await s.commit()
            return True


user_store = UserStore()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_user_store.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/auth/users.py apps/api_gateway/app/core/errors.py tests/unit/test_user_store.py
git commit -m "feat(auth): add UserStore service"
```

---

## Task 3: signup / login / logout / status routes

**Files:**
- Modify: `apps/api_gateway/app/api/routes/auth.py`
- Modify: `tests/unit/test_auth_routes.py`

**Interfaces:**
- Consumes: `user_store` (Task 2); `app.core.errors.AuthError` (existing).
- Produces: `POST /api/auth/signup {username, password}`, `POST /api/auth/login {username, password}` (sets `session["user_id"]`/`session["role"]`), `POST /api/auth/logout`, `GET /api/auth/status -> {authenticated, user_id?, username?, role?, can_use_testing?}`.

- [ ] **Step 1: Write the failing tests (replacing the old password-only tests)**

```python
# tests/unit/test_auth_routes.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def test_status_unauthenticated_by_default(client, _with_password):
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_signup_then_login_sets_session(client, _with_password):
    resp = client.post("/api/auth/signup", json={"username": "toan", "password": "s3cret"})
    assert resp.status_code == 200

    resp = client.post("/api/auth/login", json={"username": "toan", "password": "s3cret"})
    assert resp.status_code == 200

    status = client.get("/api/auth/status").json()
    assert status["authenticated"] is True
    assert status["username"] == "toan"
    assert status["role"] == "user"
    assert status["can_use_testing"] is False


def test_signup_duplicate_username_rejected(client, _with_password):
    client.post("/api/auth/signup", json={"username": "toan", "password": "pw1"})
    resp = client.post("/api/auth/signup", json={"username": "toan", "password": "pw2"})
    assert resp.status_code == 409


def test_login_wrong_password_rejected(client, _with_password):
    client.post("/api/auth/signup", json={"username": "toan", "password": "s3cret"})
    resp = client.post("/api/auth/login", json={"username": "toan", "password": "nope"})
    assert resp.status_code == 401
    assert resp.json()["success"] is False


def test_login_unknown_username_rejected(client, _with_password):
    resp = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert resp.status_code == 401


def test_logout_clears_session(client, _with_password):
    client.post("/api/auth/signup", json={"username": "toan", "password": "s3cret"})
    client.post("/api/auth/login", json={"username": "toan", "password": "s3cret"})
    client.post("/api/auth/logout")
    assert client.get("/api/auth/status").json() == {"authenticated": False}


def test_disabled_user_cannot_login(client, _with_password):
    from app.services.auth.users import user_store

    created = client.post(
        "/api/auth/signup", json={"username": "toan", "password": "s3cret"}
    )
    assert created.status_code == 200

    import asyncio

    asyncio.run(user_store.set_fields(_user_id_for("toan"), disabled=True))
    resp = client.post("/api/auth/login", json={"username": "toan", "password": "s3cret"})
    assert resp.status_code == 401


def _user_id_for(username: str) -> str:
    import asyncio

    from app.services.auth.users import user_store

    user = asyncio.run(user_store.get_by_username(username))
    return user.id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_auth_routes.py -v`
Expected: FAIL — old assertions (`{"password": ...}` body, plain `authenticated` bool) no longer match; `test_disabled_user_cannot_login` etc. fail because `login`/`signup` still use the old password-only shape.

- [ ] **Step 3: Write the implementation**

```python
# apps/api_gateway/app/api/routes/auth.py
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
```

Also fix the one other place in the test suite that still logs in with the old `{"password": ...}` shape — `tests/integration/test_ws_auth.py`'s `test_ws_accepts_valid_browser_cookie` (this file is revisited more thoroughly in Task 11; this is just the minimal fix so it doesn't break here):

```python
    login = client.post("/api/auth/login", json={"username": "toan", "password": "s3cret"})
```

(replaces `login = client.post("/api/auth/login", json={"password": "s3cret"})`)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_auth_routes.py tests/integration/test_ws_auth.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/auth.py tests/unit/test_auth_routes.py tests/integration/test_ws_auth.py
git commit -m "feat(auth): replace single admin password with username/password accounts"
```

---

## Task 4: admin bootstrap on startup

**Files:**
- Modify: `apps/api_gateway/app/core/settings.py`
- Modify: `apps/api_gateway/app/main.py`
- Test: `tests/unit/test_admin_bootstrap.py`

**Interfaces:**
- Consumes: `user_store` (Task 2).
- Produces: `settings.admin_bootstrap_username: str`, `settings.admin_bootstrap_password: str`; `app.main._bootstrap_admin_if_needed() -> None` (called from `lifespan`, also directly callable from tests without booting the whole app).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_admin_bootstrap.py
import pytest

from app.core.settings import settings
from app.main import _bootstrap_admin_if_needed
from app.services.auth.users import user_store


@pytest.mark.asyncio
async def test_bootstrap_creates_admin_from_bootstrap_settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_bootstrap_username", "root")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "r00t-pw")
    await _bootstrap_admin_if_needed()
    user = await user_store.get_by_username("root")
    assert user is not None
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_bootstrap_falls_back_to_legacy_admin_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_bootstrap_username", "")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "")
    monkeypatch.setattr(settings, "admin_password", "legacy-pw")
    await _bootstrap_admin_if_needed()
    user = await user_store.get_by_username("admin")
    assert user is not None
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_bootstrap_noop_when_users_already_exist(monkeypatch):
    monkeypatch.setattr(settings, "admin_bootstrap_username", "root")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "r00t-pw")
    await user_store.create("someone", "already-here")
    await _bootstrap_admin_if_needed()
    assert await user_store.get_by_username("root") is None


@pytest.mark.asyncio
async def test_bootstrap_noop_when_no_credentials_configured(monkeypatch):
    monkeypatch.setattr(settings, "admin_bootstrap_username", "")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "")
    monkeypatch.setattr(settings, "admin_password", "")
    await _bootstrap_admin_if_needed()
    assert await user_store.count() == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_admin_bootstrap.py -v`
Expected: FAIL with `ImportError: cannot import name '_bootstrap_admin_if_needed'`

- [ ] **Step 3: Write the implementation**

Add to `apps/api_gateway/app/core/settings.py`, right after the existing `device_auth_token` block:

```python
    # Bootstrap admin account, created once on startup if the `users` table is
    # empty. Falls back to admin_password (legacy single-secret login) with
    # username "admin" if these are unset, so upgrading an existing deployment
    # doesn't lock the operator out.
    admin_bootstrap_username: str = ""
    admin_bootstrap_password: str = ""
```

Add to `apps/api_gateway/app/main.py`, after `_warm_default_engines`:

```python
async def _bootstrap_admin_if_needed() -> None:
    from app.services.auth.users import user_store

    if await user_store.count() > 0:
        return
    username = settings.admin_bootstrap_username or "admin"
    password = settings.admin_bootstrap_password or settings.admin_password
    if not password:
        logger.warning(
            "no admin bootstrap credentials set (ADMIN_BOOTSTRAP_PASSWORD or "
            "legacy ADMIN_PASSWORD) -- create the first admin account manually"
        )
        return
    await user_store.create(username, password, role="admin")
    logger.info("bootstrap admin account created: %s", username)
```

In `lifespan`, call it right after `await init_db()`:

```python
    await init_db()
    await _bootstrap_admin_if_needed()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_admin_bootstrap.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/core/settings.py apps/api_gateway/app/main.py tests/unit/test_admin_bootstrap.py
git commit -m "feat(auth): bootstrap the first admin account on startup"
```

---

## Task 5: `AuthGuardMiddleware` role split (admin-only vs. any-user prefixes)

**Files:**
- Modify: `apps/api_gateway/app/core/auth_guard.py`
- Modify: `tests/unit/test_auth_guard.py` (HTTP-guard tests only — the `ws_authenticated` tests in this file are left untouched until Task 11)

**Interfaces:**
- Produces: `AuthGuardMiddleware` now checks `request.session.get("user_id")` (not `"authenticated"`) and splits guarded paths into `_USER_PREFIXES` (any logged-in session) and `_ADMIN_PREFIXES` (`role == "admin"` required); admin-only paths return `403` when logged in but not admin, `401`/redirect when not logged in at all.

- [ ] **Step 1: Write the failing tests**

Replace the HTTP-guard section of `tests/unit/test_auth_guard.py` (everything above the `_FakeWebSocket` class stays as-is for now) with:

```python
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_guard_noop_when_admin_password_unset(client):
    assert settings.admin_password == ""
    resp = client.get("/v1/system/status")
    assert resp.status_code != 401


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _login_as(client, username: str, password: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": password})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": password})


def test_guard_blocks_admin_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/system/status")
    assert resp.status_code == 401


def test_guard_403s_admin_route_for_regular_user(client, _with_password):
    _login_as(client, "toan", "s3cret", role="user")
    resp = client.get("/v1/system/status")
    assert resp.status_code == 403


def test_guard_allows_admin_route_for_admin(client, _with_password):
    _login_as(client, "root", "s3cret", role="admin")
    resp = client.get("/v1/system/status")
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_guard_allows_user_route_for_regular_user(client, _with_password):
    _login_as(client, "toan", "s3cret", role="user")
    resp = client.get("/v1/profiles")
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_guard_blocks_user_route_when_logged_out(client, _with_password):
    resp = client.get("/v1/profiles")
    assert resp.status_code == 401


def test_guard_allows_device_pairing_init_without_login(client, _with_password):
    resp = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"})
    assert resp.status_code != 401


def test_guard_blocks_pair_claim_when_logged_out(client, _with_password):
    resp = client.post("/v1/devices/pair/claim", json={"code": "000000", "name": "x"})
    assert resp.status_code == 401


def test_guard_allows_device_routes_without_login(client, _with_password):
    resp = client.get("/v1/stt/engines")
    assert resp.status_code != 401


def test_guard_allows_auth_routes_without_login(client, _with_password):
    resp = client.get("/api/auth/status")
    assert resp.status_code != 401


def test_guard_allows_options_preflight_without_login(client, _with_password):
    resp = client.options("/v1/system/status")
    assert resp.status_code != 401
```

(The `_FakeWebSocket`/`ws_authenticated` tests below this in the file are unchanged for now.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_auth_guard.py -v -k "not ws_auth"`
Expected: FAIL — `/v1/system/status` currently 401s for any logged-in session (no role check yet), and `/v1/devices/pair/init`/`claim` don't exist as routes yet (404, not the 401/!=401 this test expects — this will be resolved once Task 9 adds the routes; for now, run only to confirm the guard-logic tests fail on role behavior. If `pair/init`/`pair/claim` 404s make those two tests fail for the wrong reason, that's expected until Task 9 — proceed with Step 3 now and re-verify those two once Task 9 lands).

- [ ] **Step 3: Write the implementation**

```python
# apps/api_gateway/app/core/auth_guard.py
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.websockets import WebSocket

from app.core.settings import settings

_STATIC_ALLOWLIST = {"/static/login.html", "/static/js/auth.js", "/static/styles.css"}
# Unauthenticated device-side pairing handshake (the device itself has no login).
_NO_AUTH_PREFIXES = ("/v1/devices/pair/init", "/v1/devices/pair/status")
# Any logged-in session (admin or user).
_USER_PREFIXES = (
    "/ui",
    "/static/",
    "/v1/conversation",
    "/v1/livehost",
    "/v1/profiles",
    "/v1/mcp",
    "/v1/tts_profiles",
    "/v1/sessions",
    "/v1/devices/mine",
    "/v1/devices/pair/claim",
)
# role == "admin" required.
_ADMIN_PREFIXES = ("/v1/system", "/v1/models", "/v1/users", "/v1/devices")


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if not settings.admin_password:
            return await call_next(request)

        path = request.url.path
        if path in _STATIC_ALLOWLIST or path.startswith("/api/auth") or _matches(path, _NO_AUTH_PREFIXES):
            return await call_next(request)

        user_id = request.session.get("user_id")

        if _matches(path, _USER_PREFIXES):
            if not user_id:
                return self._unauthenticated(request)
            return await call_next(request)

        if _matches(path, _ADMIN_PREFIXES):
            if not user_id:
                return self._unauthenticated(request)
            if request.session.get("role") != "admin":
                return JSONResponse({"success": False, "error": "admin only"}, status_code=403)
            return await call_next(request)

        return await call_next(request)

    @staticmethod
    def _unauthenticated(request: Request):
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/static/login.html")
        return JSONResponse({"success": False, "error": "login required"}, status_code=401)


def ws_authenticated(websocket: WebSocket) -> bool:
    """Legacy boolean auth check for WS handshakes. Still used by conversation.py/
    stt.py/livehost.py until Task 11 replaces it with resolve_ws_identity()."""
    if not settings.admin_password:
        return True
    if websocket.session.get("user_id"):
        return True
    token = websocket.query_params.get("device_token")
    return bool(settings.device_auth_token) and hmac.compare_digest(token or "", settings.device_auth_token)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_auth_guard.py -v -k "not ws_auth"`
Expected: all pass except `test_guard_allows_device_pairing_init_without_login` and `test_guard_blocks_pair_claim_when_logged_out`, which 404 until Task 9 adds those routes — confirm they fail with 404 (route not found), not 401/403 (guard misbehaving), then proceed; re-run this exact file after Task 9 to confirm all green.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/core/auth_guard.py tests/unit/test_auth_guard.py
git commit -m "feat(auth): split AuthGuardMiddleware into admin-only vs any-user prefixes"
```

---

## Task 6: admin Users management routes

**Files:**
- Create: `apps/api_gateway/app/api/routes/users.py`
- Modify: `apps/api_gateway/app/main.py` (register the router)
- Test: `tests/unit/test_users_routes.py`

**Interfaces:**
- Consumes: `user_store` (Task 2).
- Produces: `POST /v1/users {username, password, role}`, `GET /v1/users`, `PATCH /v1/users/{id} {disabled?, role?, can_use_testing?}`, `POST /v1/users/{id}/reset_password {new_password}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_users_routes.py
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_create_list_users(client):
    resp = client.post("/v1/users", json={"username": "toan", "password": "pw", "role": "user"})
    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "toan"

    resp = client.get("/v1/users")
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()["data"]]
    assert usernames == ["toan"]


def test_create_duplicate_username_409(client):
    client.post("/v1/users", json={"username": "toan", "password": "pw"})
    resp = client.post("/v1/users", json={"username": "toan", "password": "pw2"})
    assert resp.status_code == 409


def test_create_invalid_role_400(client):
    resp = client.post("/v1/users", json={"username": "toan", "password": "pw", "role": "superuser"})
    assert resp.status_code == 400


def test_patch_disabled_role_testing(client):
    created = client.post("/v1/users", json={"username": "toan", "password": "pw"}).json()["data"]
    resp = client.patch(f"/v1/users/{created['id']}", json={"disabled": True, "can_use_testing": True})
    assert resp.status_code == 200
    assert resp.json()["data"]["disabled"] is True
    assert resp.json()["data"]["can_use_testing"] is True


def test_patch_missing_user_404(client):
    resp = client.patch("/v1/users/does-not-exist", json={"disabled": True})
    assert resp.status_code == 404


def test_reset_password(client):
    created = client.post("/v1/users", json={"username": "toan", "password": "old-pw"}).json()["data"]
    resp = client.post(f"/v1/users/{created['id']}/reset_password", json={"new_password": "new-pw"})
    assert resp.status_code == 200

    from app.services.auth.users import user_store
    import asyncio

    assert asyncio.run(user_store.verify_login("toan", "old-pw")) is None
    assert asyncio.run(user_store.verify_login("toan", "new-pw")) is not None


def test_reset_password_missing_user_404(client):
    resp = client.post("/v1/users/does-not-exist/reset_password", json={"new_password": "x"})
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_users_routes.py -v`
Expected: FAIL with 404s (router not mounted / doesn't exist)

- [ ] **Step 3: Write the implementation**

```python
# apps/api_gateway/app/api/routes/users.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.auth.users import user_store

router = APIRouter(prefix="/v1/users", tags=["users"])

_VALID_ROLES = ("admin", "user")


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


@router.post("")
async def create_user(payload: CreateUserRequest) -> dict:
    if payload.role not in _VALID_ROLES:
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")
    created = await user_store.create(payload.username, payload.password, role=payload.role)
    return {"success": True, "data": created}


@router.get("")
async def list_users() -> dict:
    return {"success": True, "data": await user_store.list()}


class UpdateUserRequest(BaseModel):
    disabled: bool | None = None
    role: str | None = None
    can_use_testing: bool | None = None


@router.patch("/{user_id}")
async def update_user(user_id: str, payload: UpdateUserRequest) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "role" in fields and fields["role"] not in _VALID_ROLES:
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")
    updated = await user_store.set_fields(user_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"user '{user_id}' not found")
    return {"success": True, "data": updated}


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.post("/{user_id}/reset_password")
async def reset_password(user_id: str, payload: ResetPasswordRequest) -> dict:
    ok = await user_store.reset_password(user_id, payload.new_password)
    if not ok:
        raise HTTPException(status_code=404, detail=f"user '{user_id}' not found")
    return {"success": True}
```

In `apps/api_gateway/app/main.py`, add the import alongside the other route imports:

```python
from app.api.routes.users import router as users_router
```

And register it alongside the other `app.include_router(...)` calls:

```python
app.include_router(users_router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_users_routes.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/users.py apps/api_gateway/app/main.py tests/unit/test_users_routes.py
git commit -m "feat(auth): add admin Users management routes"
```

---

## Task 7: `DeviceStore` service

**Files:**
- Create: `apps/api_gateway/app/services/auth/devices.py`
- Test: `tests/unit/test_device_store.py`

**Interfaces:**
- Consumes: `app.services.db.engine.db_session`; `app.services.db.models.Device` (Task 1); `app.services.auth.users.user_store` (Task 2, for the owning-user fixture in tests only).
- Produces: `DeviceStore` class + singleton `device_store` with `async find_active_by_serial(serial) -> Device | None`, `async create(user_id, name, serial) -> tuple[dict, str]` (returns the device dict and the raw bearer token — the only time the raw token exists), `async get_by_token(raw_token) -> Device | None`, `async get_by_id(device_id) -> Device | None` (used by Task 14's watchdog to re-check `.revoked` without the raw token), `async list_for_user(user_id) -> list[dict]`, `async list_all() -> list[dict]`, `async revoke(device_id, owner_user_id=None) -> bool`, `async touch_last_seen(device_id) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_device_store.py
import pytest

from app.services.auth.devices import DeviceStore
from app.services.auth.users import user_store


@pytest.fixture
def store():
    return DeviceStore()


async def _make_user(username="toan") -> str:
    created = await user_store.create(username, "pw")
    return created["id"]


@pytest.mark.asyncio
async def test_create_returns_device_and_raw_token(store):
    user_id = await _make_user()
    device, raw_token = await store.create(user_id, "ESP32 desk", "AA:BB:CC")
    assert device["user_id"] == user_id
    assert device["name"] == "ESP32 desk"
    assert device["serial"] == "AA:BB:CC"
    assert device["revoked"] is False
    assert isinstance(raw_token, str) and len(raw_token) > 16


@pytest.mark.asyncio
async def test_get_by_token_roundtrip(store):
    user_id = await _make_user()
    device, raw_token = await store.create(user_id, "ESP32", "AA:BB:CC")
    found = await store.get_by_token(raw_token)
    assert found is not None
    assert found.id == device["id"]
    assert await store.get_by_token("wrong-token") is None


@pytest.mark.asyncio
async def test_find_active_by_serial_ignores_revoked(store):
    user_id = await _make_user()
    device, _ = await store.create(user_id, "ESP32", "AA:BB:CC")
    assert (await store.find_active_by_serial("AA:BB:CC")).id == device["id"]
    await store.revoke(device["id"])
    assert await store.find_active_by_serial("AA:BB:CC") is None


@pytest.mark.asyncio
async def test_get_by_id(store):
    user_id = await _make_user()
    device, _ = await store.create(user_id, "ESP32", "AA:BB:CC")
    found = await store.get_by_id(device["id"])
    assert found is not None and found.id == device["id"]
    assert await store.get_by_id("missing-id") is None


@pytest.mark.asyncio
async def test_list_for_user_and_list_all(store):
    u1 = await _make_user("a")
    u2 = await _make_user("b")
    await store.create(u1, "dev1", "S1")
    await store.create(u2, "dev2", "S2")
    assert len(await store.list_for_user(u1)) == 1
    assert len(await store.list_for_user(u2)) == 1
    assert len(await store.list_all()) == 2


@pytest.mark.asyncio
async def test_revoke_scoped_to_owner(store):
    u1 = await _make_user("a")
    u2 = await _make_user("b")
    device, _ = await store.create(u1, "dev1", "S1")
    assert await store.revoke(device["id"], owner_user_id=u2) is False
    assert await store.revoke(device["id"], owner_user_id=u1) is True
    assert await store.revoke("missing-id") is False


@pytest.mark.asyncio
async def test_touch_last_seen(store):
    user_id = await _make_user()
    device, _ = await store.create(user_id, "ESP32", "AA:BB:CC")
    assert (await store.list_for_user(user_id))[0]["last_seen_at"] is None
    await store.touch_last_seen(device["id"])
    assert (await store.list_for_user(user_id))[0]["last_seen_at"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_device_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.auth.devices'`

- [ ] **Step 3: Write the implementation**

```python
# apps/api_gateway/app/services/auth/devices.py
from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy import select

from app.services.db.engine import db_session
from app.services.db.models import Device, utcnow


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _device_dict(d: Device) -> dict:
    return {
        "id": d.id,
        "user_id": d.user_id,
        "name": d.name,
        "serial": d.serial,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
        "revoked": d.revoked,
    }


class DeviceStore:
    async def find_active_by_serial(self, serial: str) -> Device | None:
        async with db_session() as s:
            return (
                await s.execute(
                    select(Device).where(Device.serial == serial, Device.revoked.is_(False))
                )
            ).scalar_one_or_none()

    async def create(self, user_id: str, name: str, serial: str) -> tuple[dict, str]:
        """Returns (device_dict, raw_token). The raw token exists only here and in
        the caller's response -- only its hash is ever persisted."""
        raw_token = secrets.token_urlsafe(32)
        async with db_session() as s:
            row = Device(
                id=str(uuid.uuid4()), user_id=user_id, name=name, serial=serial,
                token_hash=_hash_token(raw_token),
            )
            s.add(row)
            await s.commit()
            return _device_dict(row), raw_token

    async def get_by_token(self, raw_token: str) -> Device | None:
        async with db_session() as s:
            return (
                await s.execute(select(Device).where(Device.token_hash == _hash_token(raw_token)))
            ).scalar_one_or_none()

    async def get_by_id(self, device_id: str) -> Device | None:
        async with db_session() as s:
            return await s.get(Device, device_id)

    async def list_for_user(self, user_id: str) -> list[dict]:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(Device).where(Device.user_id == user_id).order_by(Device.created_at)
                )
            ).scalars().all()
            return [_device_dict(d) for d in rows]

    async def list_all(self) -> list[dict]:
        async with db_session() as s:
            rows = (await s.execute(select(Device).order_by(Device.created_at))).scalars().all()
            return [_device_dict(d) for d in rows]

    async def revoke(self, device_id: str, owner_user_id: str | None = None) -> bool:
        """owner_user_id restricts revoke to devices owned by that user (the
        'mine' route); None means any device (the admin route)."""
        async with db_session() as s:
            row = await s.get(Device, device_id)
            if row is None:
                return False
            if owner_user_id is not None and row.user_id != owner_user_id:
                return False
            row.revoked = True
            await s.commit()
            return True

    async def touch_last_seen(self, device_id: str) -> None:
        async with db_session() as s:
            row = await s.get(Device, device_id)
            if row is not None:
                row.last_seen_at = utcnow()
                await s.commit()


device_store = DeviceStore()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_device_store.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/auth/devices.py tests/unit/test_device_store.py
git commit -m "feat(auth): add DeviceStore service"
```

---

## Task 8: `PendingPairingRegistry`

**Files:**
- Create: `apps/api_gateway/app/services/auth/pairing.py`
- Test: `tests/unit/test_pairing_registry.py`

**Interfaces:**
- Produces: `PendingPairing` dataclass (`code, poll_token, serial, expires_at, claimed, device_id, token`); `PendingPairingRegistry` class + singleton `pending_pairings` with `create(serial) -> PendingPairing`, `get_by_code(code) -> PendingPairing | None`, `get_by_poll_token(poll_token) -> PendingPairing | None`, `mark_claimed(code, device_id, token) -> None`. Module-level `_TTL_SECONDS` (default 600, monkeypatchable in tests for fast expiry checks).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_pairing_registry.py
from app.services.auth import pairing as pairing_module
from app.services.auth.pairing import PendingPairingRegistry


def test_create_returns_code_and_poll_token():
    registry = PendingPairingRegistry()
    entry = registry.create("AA:BB:CC")
    assert len(entry.code) == 6 and entry.code.isdigit()
    assert entry.poll_token
    assert entry.serial == "AA:BB:CC"
    assert entry.claimed is False


def test_get_by_code_and_poll_token():
    registry = PendingPairingRegistry()
    entry = registry.create("AA:BB:CC")
    assert registry.get_by_code(entry.code) is entry
    assert registry.get_by_poll_token(entry.poll_token) is entry
    assert registry.get_by_code("000000") is None


def test_mark_claimed_sets_fields_and_removes_from_code_lookup():
    registry = PendingPairingRegistry()
    entry = registry.create("AA:BB:CC")
    registry.mark_claimed(entry.code, "device-1", "raw-token-abc")
    assert entry.claimed is True
    assert entry.device_id == "device-1"
    assert entry.token == "raw-token-abc"
    # code is single-use: a second claim attempt finds nothing
    assert registry.get_by_code(entry.code) is None
    # but the poll_token lookup (used by the device's status poll) still works
    assert registry.get_by_poll_token(entry.poll_token) is entry


def test_expired_entries_are_swept(monkeypatch):
    monkeypatch.setattr(pairing_module, "_TTL_SECONDS", -1)  # already expired
    registry = PendingPairingRegistry()
    entry = registry.create("AA:BB:CC")
    assert registry.get_by_code(entry.code) is None
    assert registry.get_by_poll_token(entry.poll_token) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_pairing_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.auth.pairing'`

- [ ] **Step 3: Write the implementation**

```python
# apps/api_gateway/app/services/auth/pairing.py
"""In-memory pending-device-pairing registry -- same pattern as
app.services.livehost.registry.livehost_registry (a process-global dict is
fine here: entries are short-lived (~10 min TTL) and losing them on restart
just means the device retries pair/init)."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

_TTL_SECONDS = 600.0


@dataclass
class PendingPairing:
    code: str
    poll_token: str
    serial: str
    expires_at: float
    claimed: bool = False
    device_id: str | None = None
    token: str | None = None


class PendingPairingRegistry:
    def __init__(self) -> None:
        self._by_code: dict[str, PendingPairing] = {}
        self._by_poll_token: dict[str, PendingPairing] = {}

    def create(self, serial: str) -> PendingPairing:
        self._sweep_expired()
        code = f"{secrets.randbelow(1_000_000):06d}"
        poll_token = secrets.token_urlsafe(24)
        entry = PendingPairing(
            code=code, poll_token=poll_token, serial=serial,
            expires_at=time.monotonic() + _TTL_SECONDS,
        )
        self._by_code[code] = entry
        self._by_poll_token[poll_token] = entry
        return entry

    def get_by_code(self, code: str) -> PendingPairing | None:
        self._sweep_expired()
        return self._by_code.get(code)

    def get_by_poll_token(self, poll_token: str) -> PendingPairing | None:
        self._sweep_expired()
        return self._by_poll_token.get(poll_token)

    def mark_claimed(self, code: str, device_id: str, token: str) -> None:
        entry = self._by_code.pop(code, None)
        if entry is not None:
            entry.claimed = True
            entry.device_id = device_id
            entry.token = token

    def _sweep_expired(self) -> None:
        now = time.monotonic()
        for code, entry in list(self._by_code.items()):
            if entry.expires_at < now:
                self._by_code.pop(code, None)
        for token, entry in list(self._by_poll_token.items()):
            if entry.expires_at < now:
                self._by_poll_token.pop(token, None)


pending_pairings = PendingPairingRegistry()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_pairing_registry.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/auth/pairing.py tests/unit/test_pairing_registry.py
git commit -m "feat(auth): add in-memory pending device-pairing registry"
```

---

## Task 9: Devices routes (pairing + mine + admin overview)

**Files:**
- Create: `apps/api_gateway/app/api/routes/devices.py`
- Modify: `apps/api_gateway/app/core/errors.py`
- Modify: `apps/api_gateway/app/main.py` (register the router)
- Test: `tests/unit/test_devices_routes.py`

**Interfaces:**
- Consumes: `device_store` (Task 7), `pending_pairings` (Task 8).
- Produces: `POST /v1/devices/pair/init {serial}`, `GET /v1/devices/pair/status?poll_token=`, `POST /v1/devices/pair/claim {code, name}`, `GET /v1/devices/mine`, `POST /v1/devices/mine/{id}/revoke`, `GET /v1/devices` (admin — each device dict includes an extra `owner_username`, joined against `user_store` for display), `POST /v1/devices/{id}/revoke` (admin). `PairingCodeInvalidError` (400), `DeviceSerialConflictError` (409).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_devices_routes.py
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _logged_in_user(client):
    client.post("/api/auth/signup", json={"username": "toan", "password": "pw"})
    client.post("/api/auth/login", json={"username": "toan", "password": "pw"})
    return "toan"


def test_pair_init_returns_code_and_poll_token(client):
    resp = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["code"]) == 6
    assert data["poll_token"]


def test_pair_status_unclaimed_then_claimed(client, _logged_in_user):
    init = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"}).json()["data"]

    status_before = client.get(f"/v1/devices/pair/status?poll_token={init['poll_token']}")
    assert status_before.json()["data"]["claimed"] is False

    claim = client.post("/v1/devices/pair/claim", json={"code": init["code"], "name": "ESP32 desk"})
    assert claim.status_code == 200

    status_after = client.get(f"/v1/devices/pair/status?poll_token={init['poll_token']}")
    body = status_after.json()["data"]
    assert body["claimed"] is True
    assert body["device_id"]
    assert body["token"]


def test_pair_status_unknown_poll_token_404(client):
    resp = client.get("/v1/devices/pair/status?poll_token=nonexistent")
    assert resp.status_code == 404


def test_pair_claim_invalid_code_400(client, _logged_in_user):
    resp = client.post("/v1/devices/pair/claim", json={"code": "000000", "name": "x"})
    assert resp.status_code == 400


def test_pair_claim_serial_conflict_requires_revoke_first(client, _logged_in_user):
    init1 = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"}).json()["data"]
    client.post("/v1/devices/pair/claim", json={"code": init1["code"], "name": "first"})

    init2 = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"}).json()["data"]
    resp = client.post("/v1/devices/pair/claim", json={"code": init2["code"], "name": "second"})
    assert resp.status_code == 409


def test_mine_lists_only_own_devices_and_revoke_is_scoped(client):
    client.post("/api/auth/signup", json={"username": "a", "password": "pw"})
    client.post("/api/auth/signup", json={"username": "b", "password": "pw"})

    client.post("/api/auth/login", json={"username": "a", "password": "pw"})
    init = client.post("/v1/devices/pair/init", json={"serial": "S1"}).json()["data"]
    client.post("/v1/devices/pair/claim", json={"code": init["code"], "name": "dev-a"})

    mine_a = client.get("/v1/devices/mine").json()["data"]
    assert len(mine_a) == 1
    device_id = mine_a[0]["id"]

    client.post("/api/auth/login", json={"username": "b", "password": "pw"})
    mine_b = client.get("/v1/devices/mine").json()["data"]
    assert mine_b == []

    # b cannot revoke a's device
    resp = client.post(f"/v1/devices/mine/{device_id}/revoke")
    assert resp.status_code == 404

    client.post("/api/auth/login", json={"username": "a", "password": "pw"})
    resp = client.post(f"/v1/devices/mine/{device_id}/revoke")
    assert resp.status_code == 200


def test_admin_lists_and_revokes_any_device(client, _logged_in_user):
    init = client.post("/v1/devices/pair/init", json={"serial": "S1"}).json()["data"]
    client.post("/v1/devices/pair/claim", json={"code": init["code"], "name": "dev"})

    resp = client.get("/v1/devices")
    assert resp.status_code == 200
    devices = resp.json()["data"]
    assert len(devices) == 1
    assert devices[0]["owner_username"] == "toan"

    resp = client.post(f"/v1/devices/{devices[0]['id']}/revoke")
    assert resp.status_code == 200

    resp = client.post("/v1/devices/does-not-exist/revoke")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_devices_routes.py -v`
Expected: FAIL — router doesn't exist yet (404s across the board)

- [ ] **Step 3: Write the implementation**

Add to `apps/api_gateway/app/core/errors.py` (after `UsernameTakenError`):

```python
class PairingCodeInvalidError(AppError):
    """Raised when pair/claim is given an unknown or expired code."""

    status_code = 400


class DeviceSerialConflictError(AppError):
    """Raised when pair/claim's serial already has a non-revoked device."""

    status_code = 409
```

```python
# apps/api_gateway/app/api/routes/devices.py
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.errors import DeviceSerialConflictError, PairingCodeInvalidError
from app.services.auth.devices import device_store
from app.services.auth.pairing import pending_pairings

router = APIRouter(prefix="/v1/devices", tags=["devices"])


class PairInitRequest(BaseModel):
    serial: str


@router.post("/pair/init")
async def pair_init(payload: PairInitRequest) -> dict:
    entry = pending_pairings.create(payload.serial)
    return {"success": True, "data": {"code": entry.code, "poll_token": entry.poll_token}}


@router.get("/pair/status")
async def pair_status(poll_token: str) -> dict:
    entry = pending_pairings.get_by_poll_token(poll_token)
    if entry is None:
        raise HTTPException(status_code=404, detail="pairing session not found or expired")
    if not entry.claimed:
        return {"success": True, "data": {"claimed": False}}
    return {
        "success": True,
        "data": {"claimed": True, "device_id": entry.device_id, "token": entry.token},
    }


class PairClaimRequest(BaseModel):
    code: str
    name: str


@router.post("/pair/claim")
async def pair_claim(payload: PairClaimRequest, request: Request) -> dict:
    entry = pending_pairings.get_by_code(payload.code)
    if entry is None:
        raise PairingCodeInvalidError("pairing code is invalid or expired")
    existing = await device_store.find_active_by_serial(entry.serial)
    if existing is not None:
        raise DeviceSerialConflictError(
            "a device with this hardware is already paired; revoke it first"
        )
    user_id = request.session["user_id"]  # guaranteed by AuthGuardMiddleware on this path
    device, raw_token = await device_store.create(user_id, payload.name, entry.serial)
    pending_pairings.mark_claimed(payload.code, device["id"], raw_token)
    return {"success": True, "data": device}


@router.get("/mine")
async def list_my_devices(request: Request) -> dict:
    user_id = request.session["user_id"]
    return {"success": True, "data": await device_store.list_for_user(user_id)}


@router.post("/mine/{device_id}/revoke")
async def revoke_my_device(device_id: str, request: Request) -> dict:
    user_id = request.session["user_id"]
    ok = await device_store.revoke(device_id, owner_user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    return {"success": True}


@router.get("")
async def list_all_devices() -> dict:
    from app.services.auth.users import user_store

    devices = await device_store.list_all()
    for device in devices:
        owner = await user_store.get_by_id(device["user_id"])
        device["owner_username"] = owner.username if owner else "(deleted)"
    return {"success": True, "data": devices}


@router.post("/{device_id}/revoke")
async def revoke_any_device(device_id: str) -> dict:
    ok = await device_store.revoke(device_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    return {"success": True}
```

In `apps/api_gateway/app/main.py`, add the import alongside the others:

```python
from app.api.routes.devices import router as devices_router
```

And register it:

```python
app.include_router(devices_router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_devices_routes.py tests/unit/test_auth_guard.py -v -k "not ws_auth"`
Expected: all pass, including the two `pair/init`/`pair/claim` guard tests from Task 5 that were pending on this task's routes existing.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/devices.py apps/api_gateway/app/core/errors.py apps/api_gateway/app/main.py tests/unit/test_devices_routes.py
git commit -m "feat(auth): add device pairing + mine/admin device routes"
```

---

## Task 10: `WsIdentity` + `resolve_ws_identity()`

**Files:**
- Modify: `apps/api_gateway/app/core/auth_guard.py`
- Modify: `tests/unit/test_auth_guard.py` (append new tests; the old `ws_authenticated` tests stay for now — `ws_authenticated` itself is untouched in this task, still used by the WS routes until Task 11)

**Interfaces:**
- Consumes: `user_store.get_by_id` (Task 2), `device_store.get_by_token`/`touch_last_seen` (Task 7).
- Produces: `WsIdentity` dataclass (`user_id: str | None, device_id: str | None`); `async resolve_ws_identity(websocket) -> WsIdentity | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_auth_guard.py`, after the existing `_FakeWebSocket` class and `ws_authenticated` tests:

```python
import pytest

from app.services.auth.devices import device_store
from app.services.auth.users import user_store


@pytest.mark.asyncio
async def test_resolve_identity_noop_when_admin_password_unset():
    from app.core.auth_guard import resolve_ws_identity

    assert settings.admin_password == ""
    identity = await resolve_ws_identity(_FakeWebSocket())
    assert identity is not None
    assert identity.user_id is None and identity.device_id is None


@pytest.mark.asyncio
async def test_resolve_identity_from_browser_cookie_session(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    identity = await resolve_ws_identity(_FakeWebSocket(session={"user_id": user["id"]}))
    assert identity is not None
    assert identity.user_id == user["id"]
    assert identity.device_id is None


@pytest.mark.asyncio
async def test_resolve_identity_rejects_disabled_user_cookie(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    await user_store.set_fields(user["id"], disabled=True)
    identity = await resolve_ws_identity(_FakeWebSocket(session={"user_id": user["id"]}))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_rejects_missing_cookie_and_token(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    assert await resolve_ws_identity(_FakeWebSocket()) is None


@pytest.mark.asyncio
async def test_resolve_identity_from_paired_device_token(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    device, raw_token = await device_store.create(user["id"], "ESP32", "AA:BB:CC")
    identity = await resolve_ws_identity(_FakeWebSocket(query_params={"device_token": raw_token}))
    assert identity is not None
    assert identity.user_id == user["id"]
    assert identity.device_id == device["id"]


@pytest.mark.asyncio
async def test_resolve_identity_rejects_revoked_device(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    device, raw_token = await device_store.create(user["id"], "ESP32", "AA:BB:CC")
    await device_store.revoke(device["id"])
    identity = await resolve_ws_identity(_FakeWebSocket(query_params={"device_token": raw_token}))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_rejects_device_of_disabled_owner(_with_password):
    from app.core.auth_guard import resolve_ws_identity

    user = await user_store.create("toan", "pw")
    device, raw_token = await device_store.create(user["id"], "ESP32", "AA:BB:CC")
    await user_store.set_fields(user["id"], disabled=True)
    identity = await resolve_ws_identity(_FakeWebSocket(query_params={"device_token": raw_token}))
    assert identity is None


@pytest.mark.asyncio
async def test_resolve_identity_accepts_legacy_shared_device_token(_with_password, monkeypatch):
    from app.core.auth_guard import resolve_ws_identity

    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    identity = await resolve_ws_identity(
        _FakeWebSocket(query_params={"device_token": "d3vice-secret"})
    )
    assert identity is not None
    assert identity.user_id is None and identity.device_id is None


@pytest.mark.asyncio
async def test_resolve_identity_rejects_wrong_token(_with_password, monkeypatch):
    from app.core.auth_guard import resolve_ws_identity

    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    identity = await resolve_ws_identity(_FakeWebSocket(query_params={"device_token": "wrong"}))
    assert identity is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_auth_guard.py -v -k resolve_identity`
Expected: FAIL with `ImportError: cannot import name 'resolve_ws_identity'`

- [ ] **Step 3: Write the implementation**

Add to `apps/api_gateway/app/core/auth_guard.py` (add a `dataclass` import alongside the existing `import hmac`, and append the new dataclass + function at the end of the file):

```python
import hmac
from dataclasses import dataclass
```

```python
@dataclass
class WsIdentity:
    user_id: str | None
    device_id: str | None


async def resolve_ws_identity(websocket: WebSocket) -> "WsIdentity | None":
    """Identity-resolving replacement for ws_authenticated(). Checks the browser
    cookie session first (and re-verifies the user isn't disabled -- a stale
    cookie from before a disable shouldn't grant a fresh connection), then a
    paired-device token (services.auth.devices), then the legacy shared
    device_auth_token as a temporary fallback for un-paired fleets."""
    from app.services.auth.devices import device_store
    from app.services.auth.users import user_store

    if not settings.admin_password:
        return WsIdentity(user_id=None, device_id=None)

    session_user_id = websocket.session.get("user_id")
    if session_user_id:
        user = await user_store.get_by_id(session_user_id)
        if user is None or user.disabled:
            return None
        return WsIdentity(user_id=user.id, device_id=None)

    token = websocket.query_params.get("device_token")
    if not token:
        return None

    device = await device_store.get_by_token(token)
    if device is not None:
        if device.revoked:
            return None
        owner = await user_store.get_by_id(device.user_id)
        if owner is None or owner.disabled:
            return None
        await device_store.touch_last_seen(device.id)
        return WsIdentity(user_id=device.user_id, device_id=device.id)

    if settings.device_auth_token and hmac.compare_digest(token, settings.device_auth_token):
        return WsIdentity(user_id=None, device_id=None)
    return None
```

(No direct `hashlib` use here — `device_store.get_by_token` does its own token hashing internally, Task 7.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_auth_guard.py -v`
Expected: all pass (old `ws_authenticated` tests + new `resolve_ws_identity` tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/core/auth_guard.py tests/unit/test_auth_guard.py
git commit -m "feat(auth): add resolve_ws_identity, resolving WS connections to a user/device"
```

---

## Task 11: wire `resolve_ws_identity` into conversation/stt/livehost; retire `ws_authenticated`

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py`
- Modify: `apps/api_gateway/app/api/routes/stt.py`
- Modify: `apps/api_gateway/app/api/routes/livehost.py`
- Modify: `apps/api_gateway/app/core/auth_guard.py` (delete `ws_authenticated`, now unused)
- Modify: `tests/unit/test_auth_guard.py` (delete the now-dead `ws_authenticated` tests)
- Test: `tests/integration/test_ws_auth.py` (update to exercise the new rejection path)

**Interfaces:**
- Consumes: `resolve_ws_identity` (Task 10).
- Produces: each of the three WS routes now closes with `4401` when `resolve_ws_identity(...)` returns `None`, and holds the resolved `WsIdentity` in a local `identity` variable in scope for Tasks 14/15's watchdog integration.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_ws_auth.py` already parametrizes its 3 rejection/acceptance tests over `ROUTES = [conversation, stt, livehost]` (Task 3 fixed its one old-shape login call). Add one more parametrized case confirming a *disabled* user's cookie session is rejected at connect time, not just a missing/wrong token — append after `test_ws_accepts_valid_browser_cookie`:

```python
@pytest.mark.parametrize("path,query", ROUTES)
def test_ws_rejects_disabled_user_cookie(client, _with_password, path, query, monkeypatch):
    import asyncio

    from app.services.auth.users import user_store

    client.post("/api/auth/signup", json={"username": "toan", "password": "pw"})
    login = client.post("/api/auth/login", json={"username": "toan", "password": "pw"})
    assert login.status_code == 200
    user = asyncio.run(user_store.get_by_username("toan"))
    asyncio.run(user_store.set_fields(user.id, disabled=True))

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"{path}?{query}"):
            pass
    assert exc_info.value.code == 4401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_ws_auth.py -v`
Expected: the new disabled-cookie cases FAIL (routes still use the old boolean `ws_authenticated`, which has no concept of "disabled")

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/api/routes/conversation.py`:

```python
from app.core.auth_guard import resolve_ws_identity
```

(replaces `from app.core.auth_guard import ws_authenticated`)

```python
@router.websocket("/stream")
async def conversation_stream(websocket: WebSocket) -> None:
    identity = await resolve_ws_identity(websocket)
    if identity is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
```

(replaces the old `if not ws_authenticated(websocket): ...` block; `identity` is now in scope for the rest of the function, used starting Task 14)

Apply the identical change to `apps/api_gateway/app/api/routes/stt.py`:

```python
from app.core.auth_guard import resolve_ws_identity
```

```python
@router.websocket("/stream")
async def stt_stream(websocket: WebSocket) -> None:
    identity = await resolve_ws_identity(websocket)
    if identity is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
```

And to `apps/api_gateway/app/api/routes/livehost.py`:

```python
from app.core.auth_guard import resolve_ws_identity
```

```python
@router.websocket("/stream")
async def livehost_stream(websocket: WebSocket) -> None:
    identity = await resolve_ws_identity(websocket)
    if identity is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
```

Delete `ws_authenticated` entirely from `apps/api_gateway/app/core/auth_guard.py` (its only callers were the three routes just updated).

Delete its tests from `tests/unit/test_auth_guard.py`: remove the `_FakeWebSocket`-based `test_ws_auth_*` functions (`test_ws_auth_noop_when_admin_password_unset`, `test_ws_auth_accepts_valid_browser_cookie_session`, `test_ws_auth_rejects_missing_cookie_and_missing_token`, `test_ws_auth_accepts_valid_device_token`, `test_ws_auth_rejects_wrong_device_token`, `test_ws_auth_rejects_device_token_when_none_configured`) — the `test_resolve_identity_*` tests added in Task 10 already cover this behavior on the new function. Keep the `_FakeWebSocket` class itself (still used by those tests).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_ws_auth.py tests/unit/test_auth_guard.py -v`
Expected: all pass; no references to `ws_authenticated` remain anywhere (`grep -rn ws_authenticated apps/ tests/` returns nothing)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py apps/api_gateway/app/api/routes/stt.py apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/core/auth_guard.py tests/unit/test_auth_guard.py tests/integration/test_ws_auth.py
git commit -m "feat(auth): resolve WS connections to a user/device identity, not just a bool"
```

---

## Task 12: add auth to `/v1/lugo/stream` (closes the gap found during investigation)

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py`
- Test: `tests/integration/test_lugo_auth.py` (new — this route had no auth test coverage at all before)

**Interfaces:**
- Consumes: `resolve_ws_identity` (Task 10).
- Produces: `/v1/lugo/stream` now closes with `4401` when unauthenticated, matching the other three WS routes. `identity` is in scope in `lugo_stream` for Task 16's watchdog extension.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_lugo_auth.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def test_lugo_stream_rejects_unauthenticated_connect(client, _with_password):
    with pytest.raises(Exception):  # noqa: B017 -- TestClient raises on the 4401 close
        with client.websocket_connect("/v1/lugo/stream"):
            pass


def test_lugo_stream_accepts_valid_device_token(client, _with_password, monkeypatch):
    monkeypatch.setattr(settings, "device_auth_token", "d3vice-secret")
    with client.websocket_connect("/v1/lugo/stream?device_token=d3vice-secret") as ws:
        ws.send_json({"type": "wakeup"})
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_lugo_auth.py -v`
Expected: `test_lugo_stream_rejects_unauthenticated_connect` FAILS (the route currently accepts any connection — this is exactly the gap being closed)

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/api/routes/lugo.py`, add the import:

```python
from app.core.auth_guard import resolve_ws_identity
```

Change the top of `lugo_stream`:

```python
@router.websocket("/stream")
async def lugo_stream(websocket: WebSocket) -> None:
    identity = await resolve_ws_identity(websocket)
    if identity is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
    # Handshake: first frame must be a `wakeup`.
    message = await websocket.receive()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_lugo_auth.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/lugo.py tests/integration/test_lugo_auth.py
git commit -m "fix(auth): require auth on /v1/lugo/stream (previously unguarded)"
```

---

## Task 13: shared `IdentityWatchdog` + `receive_with_watchdog` primitive

**Files:**
- Create: `apps/api_gateway/app/core/identity_watch.py`
- Test: `tests/unit/test_identity_watch.py`

**Interfaces:**
- Consumes: `WsIdentity` (Task 10); `user_store.get_by_id` (Task 2); `device_store.get_by_id` (Task 7).
- Produces: `IdentityWatchdog(still_valid: Callable[[], Awaitable[bool]], interval_s: float = 30.0)` with `.start()`, `.cancel()`, `.invalid: bool`, `.task: asyncio.Task | None`; `async def receive_with_watchdog(websocket, watchdog: IdentityWatchdog | None)` — an async generator yielding `websocket.receive()` results, or `None` once (then stopping) when the watchdog fires; `async def identity_still_valid(identity: WsIdentity) -> bool` (Task 16 calls this directly); `build_identity_watchdog(identity: WsIdentity, interval_s: float = 30.0) -> IdentityWatchdog | None` — `None` when `identity` has no `user_id`/`device_id` to check (the legacy shared-token case), otherwise a not-yet-started `IdentityWatchdog` wrapping `identity_still_valid`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_identity_watch.py
import asyncio

import pytest

from app.core.identity_watch import IdentityWatchdog, receive_with_watchdog


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False


@pytest.mark.asyncio
async def test_watchdog_stays_valid_when_still_valid_returns_true():
    watchdog = IdentityWatchdog(still_valid=_true, interval_s=0.01)
    watchdog.start()
    await asyncio.sleep(0.03)
    assert watchdog.invalid is False
    watchdog.cancel()


@pytest.mark.asyncio
async def test_watchdog_flags_invalid_when_still_valid_returns_false():
    watchdog = IdentityWatchdog(still_valid=_false, interval_s=0.01)
    watchdog.start()
    await asyncio.sleep(0.03)
    assert watchdog.invalid is True


class _FakeWebSocket:
    def __init__(self, messages):
        self._messages = list(messages)

    async def receive(self):
        if not self._messages:
            await asyncio.sleep(3600)  # simulate "no more messages, block forever"
        return self._messages.pop(0)


@pytest.mark.asyncio
async def test_receive_with_watchdog_yields_messages_when_valid():
    ws = _FakeWebSocket([{"text": "one"}, {"text": "two"}])
    watchdog = IdentityWatchdog(still_valid=_true, interval_s=10)
    watchdog.start()
    received = []
    async for message in receive_with_watchdog(ws, watchdog):
        received.append(message)
        if len(received) == 2:
            break
    watchdog.cancel()
    assert received == [{"text": "one"}, {"text": "two"}]


@pytest.mark.asyncio
async def test_receive_with_watchdog_yields_none_when_watchdog_fires():
    ws = _FakeWebSocket([])  # receive() blocks forever -- only the watchdog can end this
    watchdog = IdentityWatchdog(still_valid=_false, interval_s=0.01)
    watchdog.start()
    result = "unset"
    async for message in receive_with_watchdog(ws, watchdog):
        result = message
        break
    assert result is None


@pytest.mark.asyncio
async def test_receive_with_watchdog_works_with_no_watchdog():
    ws = _FakeWebSocket([{"text": "one"}])
    received = []
    async for message in receive_with_watchdog(ws, None):
        received.append(message)
        break
    assert received == [{"text": "one"}]


@pytest.mark.asyncio
async def test_build_identity_watchdog_none_for_unowned_identity():
    from app.core.auth_guard import WsIdentity
    from app.core.identity_watch import build_identity_watchdog

    assert build_identity_watchdog(WsIdentity(user_id=None, device_id=None)) is None


@pytest.mark.asyncio
async def test_build_identity_watchdog_fires_when_user_disabled():
    from app.core.auth_guard import WsIdentity
    from app.core.identity_watch import build_identity_watchdog
    from app.services.auth.users import user_store

    user = await user_store.create("toan", "pw")
    watchdog = build_identity_watchdog(WsIdentity(user_id=user["id"], device_id=None), interval_s=0.01)
    assert watchdog is not None
    watchdog.start()
    await asyncio.sleep(0.03)
    assert watchdog.invalid is False
    await user_store.set_fields(user["id"], disabled=True)
    await asyncio.sleep(0.03)
    assert watchdog.invalid is True


@pytest.mark.asyncio
async def test_build_identity_watchdog_fires_when_device_revoked():
    from app.core.auth_guard import WsIdentity
    from app.core.identity_watch import build_identity_watchdog
    from app.services.auth.devices import device_store
    from app.services.auth.users import user_store

    user = await user_store.create("toan", "pw")
    device, _ = await device_store.create(user["id"], "ESP32", "AA:BB:CC")
    identity = WsIdentity(user_id=user["id"], device_id=device["id"])
    watchdog = build_identity_watchdog(identity, interval_s=0.01)
    watchdog.start()
    await device_store.revoke(device["id"])
    await asyncio.sleep(0.03)
    assert watchdog.invalid is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_identity_watch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.identity_watch'`

- [ ] **Step 3: Write the implementation**

```python
# apps/api_gateway/app/core/identity_watch.py
"""Periodic disabled/revoked re-check for long-lived WS sessions, generalizing
the idle-timeout watchdog pattern already used by app.api.routes.lugo
(lugo_stream's `_watchdog()`): a background asyncio task that wakes on a fixed
interval and checks a condition, closing the connection if it fails."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable


class IdentityWatchdog:
    def __init__(
        self, still_valid: Callable[[], Awaitable[bool]], interval_s: float = 30.0
    ) -> None:
        self._still_valid = still_valid
        self._interval_s = interval_s
        self.invalid = False
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            if not await self._still_valid():
                self.invalid = True
                return

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()


async def receive_with_watchdog(websocket, watchdog: IdentityWatchdog | None):
    """Async generator yielding websocket.receive() results. If `watchdog` fires
    while waiting, yields None exactly once and stops -- the caller should close
    the connection and break out of its loop."""
    while True:
        recv = asyncio.create_task(websocket.receive())
        waitables = {recv, watchdog.task} if watchdog is not None and watchdog.task is not None else {recv}
        await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
        if watchdog is not None and watchdog.invalid:
            recv.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recv
            yield None
            return
        yield recv.result()


async def identity_still_valid(identity) -> bool:
    """identity: app.core.auth_guard.WsIdentity. Standalone so lugo.py's own
    idle-timeout watchdog (Task 16) can call the same check inline, instead of
    running a second concurrent IdentityWatchdog task alongside its existing one."""
    from app.services.auth.devices import device_store
    from app.services.auth.users import user_store

    if identity.user_id is not None:
        user = await user_store.get_by_id(identity.user_id)
        if user is None or user.disabled:
            return False
    if identity.device_id is not None:
        device = await device_store.get_by_id(identity.device_id)
        if device is None or device.revoked:
            return False
    return True


def build_identity_watchdog(identity, interval_s: float = 30.0) -> IdentityWatchdog | None:
    """Returns None when there's nothing to check (the legacy shared
    device_auth_token case has neither user_id nor device_id)."""
    if identity.user_id is None and identity.device_id is None:
        return None
    return IdentityWatchdog(
        still_valid=lambda: identity_still_valid(identity), interval_s=interval_s
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_identity_watch.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/core/identity_watch.py tests/unit/test_identity_watch.py
git commit -m "feat(auth): add IdentityWatchdog + receive_with_watchdog primitive"
```

---

## Task 14: integrate the watchdog into `conversation.py`'s main loop

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py`
- Test: `tests/integration/test_conversation_disabled_cutoff.py`

**Interfaces:**
- Consumes: `build_identity_watchdog`, `receive_with_watchdog` (Task 13).
- Produces: a module-level `_IDENTITY_RECHECK_INTERVAL_S = 30.0` (test-tunable, mirroring `lugo.py`'s existing `_IDLE_TICK_S` pattern); the WS loop now closes with `4401` shortly after the connected user is disabled.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_conversation_disabled_cutoff.py
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.auth.users import user_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service


class _StubSTT(STTProvider):
    name = "stub-cutoff-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr("app.api.routes.conversation._IDENTITY_RECHECK_INTERVAL_S", 0.05, raising=False)
    stt_service.providers["stub-cutoff-stt"] = _StubSTT()
    yield
    stt_service.providers.pop("stub-cutoff-stt", None)
    monkeypatch.setattr(settings, "admin_password", "")


def test_disabled_user_connection_is_closed_within_recheck_interval():
    client = TestClient(app)
    client.post("/api/auth/signup", json={"username": "toan", "password": "pw"})
    client.post("/api/auth/login", json={"username": "toan", "password": "pw"})
    user = asyncio.run(user_store.get_by_username("toan"))

    with client.websocket_connect("/v1/conversation/stream?stt_engine=stub-cutoff-stt") as ws:
        asyncio.run(user_store.set_fields(user.id, disabled=True))
        with pytest.raises(Exception):  # noqa: B017 -- server closes with 4401
            for _ in range(50):
                ws.receive()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_conversation_disabled_cutoff.py -v`
Expected: FAIL — no watchdog exists yet, the loop never notices the user was disabled mid-connection; the test times out consuming 50 receives without ever closing

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/api/routes/conversation.py`, add the import and module constant:

```python
from app.core.identity_watch import build_identity_watchdog, receive_with_watchdog
```

```python
# How often the disabled/revoked re-check wakes (test-tunable, same pattern as
# lugo.py's _IDLE_TICK_S).
_IDENTITY_RECHECK_INTERVAL_S = 30.0
```

Change the session loop:

```python
    session = ConversationSession(cfg, emit, emit_audio)
    watchdog = build_identity_watchdog(identity, interval_s=_IDENTITY_RECHECK_INTERVAL_S)
    if watchdog is not None:
        watchdog.start()
    try:
        await session.start()
        async for message in receive_with_watchdog(websocket, watchdog):
            if message is None:
                await websocket.close(code=4401, reason="account disabled")
                break
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await session.feed_audio(message["bytes"])
            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "text":
                    await session.feed_text(control.get("text") or "")
                elif ctype == "abort":
                    await session.abort("user")
                elif ctype == "reset":
                    await session.reset()
                elif ctype in {"flush", "end"}:
                    await session.flush()
                    if ctype == "end":
                        await emit("done")
                        break
    except WebSocketDisconnect:
        pass
    finally:
        if watchdog is not None:
            watchdog.cancel()
        await session.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_conversation_disabled_cutoff.py -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py tests/integration/test_conversation_disabled_cutoff.py
git commit -m "feat(auth): disable a user cuts their live conversation WS within ~30s"
```

---

## Task 15: integrate the watchdog into `livehost.py` and `stt.py`'s main loops

**Files:**
- Modify: `apps/api_gateway/app/api/routes/livehost.py`
- Modify: `apps/api_gateway/app/api/routes/stt.py`
- Test: `tests/integration/test_livehost_disabled_cutoff.py`
- Test: `tests/integration/test_stt_disabled_cutoff.py`

**Interfaces:**
- Consumes: `build_identity_watchdog`, `receive_with_watchdog` (Task 13).
- Produces: same behavior as Task 14, applied to `/v1/livehost/stream` and `/v1/stt/stream`; each route gets its own `_IDENTITY_RECHECK_INTERVAL_S` module constant.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_livehost_disabled_cutoff.py
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.auth.users import user_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-lh-cutoff-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-lh-cutoff-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
                          duration_seconds=0.1, text=payload.text)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr("app.api.routes.livehost._IDENTITY_RECHECK_INTERVAL_S", 0.05, raising=False)
    stt_service.providers["stub-lh-cutoff-stt"] = _StubSTT()
    tts_service.providers["stub-lh-cutoff-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-lh-cutoff-stt", None)
    tts_service.providers.pop("stub-lh-cutoff-tts", None)
    monkeypatch.setattr(settings, "admin_password", "")


def test_disabled_user_connection_is_closed_within_recheck_interval():
    client = TestClient(app)
    client.post("/api/auth/signup", json={"username": "toan", "password": "pw"})
    client.post("/api/auth/login", json={"username": "toan", "password": "pw"})
    user = asyncio.run(user_store.get_by_username("toan"))

    url = "/v1/livehost/stream?stt_engine=stub-lh-cutoff-stt&tts_engine=stub-lh-cutoff-tts"
    with client.websocket_connect(url) as ws:
        asyncio.run(user_store.set_fields(user.id, disabled=True))
        with pytest.raises(Exception):  # noqa: B017 -- server closes with 4401
            for _ in range(50):
                ws.receive()
```

```python
# tests/integration/test_stt_disabled_cutoff.py
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.auth.users import user_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service


class _StubSTT(STTProvider):
    name = "stub-stt-cutoff"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="batch", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr("app.api.routes.stt._IDENTITY_RECHECK_INTERVAL_S", 0.05, raising=False)
    stt_service.providers["stub-stt-cutoff"] = _StubSTT()
    yield
    stt_service.providers.pop("stub-stt-cutoff", None)
    monkeypatch.setattr(settings, "admin_password", "")


def test_disabled_user_connection_is_closed_within_recheck_interval():
    client = TestClient(app)
    client.post("/api/auth/signup", json={"username": "toan", "password": "pw"})
    client.post("/api/auth/login", json={"username": "toan", "password": "pw"})
    user = asyncio.run(user_store.get_by_username("toan"))

    with client.websocket_connect("/v1/stt/stream?engine=stub-stt-cutoff&sample_rate=16000") as ws:
        ws.receive_json()  # session_started
        asyncio.run(user_store.set_fields(user.id, disabled=True))
        with pytest.raises(Exception):  # noqa: B017 -- server closes with 4401
            for _ in range(50):
                ws.receive()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_livehost_disabled_cutoff.py tests/integration/test_stt_disabled_cutoff.py -v`
Expected: FAIL — both loops still use plain `websocket.receive()`, never notice the disable

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/api/routes/livehost.py`, add the import and constant:

```python
from app.core.identity_watch import build_identity_watchdog, receive_with_watchdog
```

```python
_IDENTITY_RECHECK_INTERVAL_S = 30.0
```

Replace the loop (around the existing `while True: message = await websocket.receive() ...` shown above, after `drain_task`/`poll_task` are created):

```python
        watchdog = build_identity_watchdog(identity, interval_s=_IDENTITY_RECHECK_INTERVAL_S)
        if watchdog is not None:
            watchdog.start()

        async for message in receive_with_watchdog(websocket, watchdog):
            if message is None:
                await websocket.close(code=4401, reason="account disabled")
                break
            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                frame = message["bytes"]
                if opus_decoder is not None:
                    try:
                        frame = opus_decoder.decode(frame)
                    except Exception as exc:  # noqa: BLE001 - skip a bad packet, keep going
                        logger.warning("livehost opus decode failed: %s", exc)
                        continue
                event = endpointer.accept(frame)
                if not event:
                    continue
                if event["event"] == "speech_start":
                    await abort_turn("barge-in")
                    await send("speech_start")
                elif event["event"] == "endpoint":
                    async with turn_lock:
                        await _abort_turn_locked("superseded")
                        await send("speech_end", speech_ms=round(event["speech_ms"]))
                        current_turn = asyncio.create_task(run_voice_turn(event["audio"]))

            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "abort":
                    await abort_turn("user")
                elif ctype == "reset":
                    await abort_turn("reset")
                    history.clear()
                    endpointer.reset()
                    await send("reset")
                elif ctype in {"flush", "end"}:
                    audio = endpointer.flush()
                    if audio:
                        async with turn_lock:
                            await _abort_turn_locked("superseded")
                            await send("speech_end", speech_ms=0)
                            current_turn = asyncio.create_task(run_voice_turn(audio))
                    if ctype == "end":
                        await abort_turn("end")
                        await send("done")
                        break
    except WebSocketDisconnect:
        pass
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if current_turn and not current_turn.done():
            current_turn.cancel()
        if drain_task:
            drain_task.cancel()
        if poll_task:
            poll_task.cancel()
```

(`identity` here is the value already resolved at the top of `livehost_stream` in Task 11.)

In `apps/api_gateway/app/api/routes/stt.py`, add the same import and constant:

```python
from app.core.identity_watch import build_identity_watchdog, receive_with_watchdog
```

```python
_IDENTITY_RECHECK_INTERVAL_S = 30.0
```

Replace its loop:

```python
    watchdog = build_identity_watchdog(identity, interval_s=_IDENTITY_RECHECK_INTERVAL_S)
    if watchdog is not None:
        watchdog.start()

    try:
        async for message in receive_with_watchdog(websocket, watchdog):
            if message is None:
                await websocket.close(code=4401, reason="account disabled")
                break

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                frame = message["bytes"]
                if denoise or vad:
                    frame = preprocess_pcm16(
                        frame, sample_rate, denoise=denoise, vad=vad,
                        amount=settings.stt_noise_reduce_amount,
                    )
                try:
                    results = await stream.accept(frame)
                except RuntimeError as exc:
                    sequence += 1
                    await _emit(
                        websocket, channel,
                        StreamEvent(event_type="error", session_id=session_id,
                                    sequence=sequence, payload={"message": str(exc)}),
                    )
                    continue
                for result in results:
                    await _emit_result(result)

            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {"type": "unknown"}

                control_type = control.get("type")
                if control_type in {"flush", "end"}:
                    try:
                        final = await stream.finalize()
                    except RuntimeError as exc:
                        sequence += 1
                        await _emit(
                            websocket, channel,
                            StreamEvent(event_type="error", session_id=session_id,
                                        sequence=sequence, payload={"message": str(exc)}),
                        )
                        final = None
                    if final is not None:
                        await _emit_result(final)

                if control_type == "end":
                    sequence += 1
                    await _emit(
                        websocket, channel,
                        StreamEvent(event_type="done", session_id=session_id,
                                    sequence=sequence, payload={"message": "stream ended"}),
                    )
                    break

    except WebSocketDisconnect:
        pass
    finally:
        if watchdog is not None:
            watchdog.cancel()
        event_bus.close(channel)
```

(`identity` here is the value resolved at the top of `stt_stream` in Task 11.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_livehost_disabled_cutoff.py tests/integration/test_stt_disabled_cutoff.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/livehost.py apps/api_gateway/app/api/routes/stt.py tests/integration/test_livehost_disabled_cutoff.py tests/integration/test_stt_disabled_cutoff.py
git commit -m "feat(auth): disable a user cuts their live livehost/stt WS within ~30s"
```

---

## Task 16: extend `lugo.py`'s existing watchdog with the identity re-check

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py`
- Test: `tests/unit/test_lugo_disabled_cutoff.py`

**Interfaces:**
- Consumes: `identity_still_valid` (Task 13, revised); `identity` (resolved in Task 12).
- Produces: `lugo_stream`'s existing `_watchdog()` coroutine also cuts the connection (with a `{"type": "goodbye", "reason": "account_disabled"}` frame) when the connected user/device becomes invalid, independent of and not gated by idle/turn-active state.

Lugo already has its own idle-timeout watchdog (unlike the other three routes, which had none before Task 13-15 added a generic one) — this task extends that *existing* loop rather than layering a second concurrent watchdog task on top of it, to avoid two tasks racing to close the same socket.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lugo_disabled_cutoff.py
import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service


class _StubSTT(STTProvider):
    name = "stub-lugo-cutoff-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-lugo-cutoff-stt")
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    monkeypatch.setattr(settings, "conversation_goodbye_text", "")
    stt_service.providers["stub-lugo-cutoff-stt"] = _StubSTT()
    # idle_timeout_s huge so only the identity re-check can fire in this test.
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="fast", session=SessionConfig(idle_timeout_s=3600)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    monkeypatch.setattr("app.api.routes.lugo._IDLE_TICK_S", 0.05, raising=False)
    monkeypatch.setattr("app.api.routes.lugo._IDENTITY_RECHECK_INTERVAL_S", 0.05, raising=False)
    yield
    stt_service.providers.pop("stub-lugo-cutoff-stt", None)
    monkeypatch.setattr(settings, "admin_password", "")


def test_disabled_owner_cuts_off_paired_device():
    import asyncio

    user = asyncio.run(user_store.create("toan", "pw"))
    device, raw_token = asyncio.run(device_store.create(user["id"], "ESP32", "AA:BB:CC"))

    client = TestClient(app)
    with client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        ws.send_json({"type": "wakeup", "profile": "fast",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        asyncio.run(user_store.set_fields(user["id"], disabled=True))
        msg = ws.receive_json()
        assert msg["type"] == "goodbye"
        assert msg["reason"] == "account_disabled"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_lugo_disabled_cutoff.py -v`
Expected: FAIL — the goodbye never arrives (idle_timeout_s is set to 3600s specifically to rule that path out), so the test times out waiting on `ws.receive_json()`

- [ ] **Step 3: Write the implementation**

In `apps/api_gateway/app/api/routes/lugo.py`, add the import and a module constant near `_IDLE_TICK_S`:

```python
from app.core.identity_watch import identity_still_valid
```

```python
# How often the disabled/revoked re-check wakes (test-tunable, same idea as
# _IDLE_TICK_S). Independent of idle/turn-active state -- a disabled account
# is cut off even mid-turn.
_IDENTITY_RECHECK_INTERVAL_S = 30.0
```

Change the watchdog:

```python
    closing = False
    last_identity_check = time.monotonic()
    identity_owned = identity.user_id is not None or identity.device_id is not None

    async def _watchdog() -> None:
        nonlocal closing, last_identity_check
        while True:
            await asyncio.sleep(_IDLE_TICK_S)
            now = time.monotonic()
            if identity_owned and now - last_identity_check >= _IDENTITY_RECHECK_INTERVAL_S:
                last_identity_check = now
                if not await identity_still_valid(identity):
                    closing = True
                    try:
                        await websocket.send_json({"type": "goodbye", "reason": "account_disabled"})
                    except RuntimeError:
                        pass
                    return
            if session.is_turn_active():
                continue
            if now - last_activity >= idle:
                # Commit to closing synchronously, before the await below, so
                # the main loop cannot process/emit a message that raced in
                # concurrently with this goodbye send (ASGI sends aren't
                # mutually exclusive across concurrent awaits).
                closing = True
                try:
                    # Say a short farewell (in the bot's voice) before disconnecting.
                    # Paced in real time; give the device a moment to finish playing
                    # it out of its jitter buffer before the goodbye/close.
                    if settings.conversation_goodbye_text:
                        await session.speak(settings.conversation_goodbye_text)
                        await asyncio.sleep(0.5)
                    await websocket.send_json({"type": "goodbye", "reason": "idle_timeout"})
                except RuntimeError:
                    pass
                return

    # idle <= 0 means "never disconnect": skip scheduling the watchdog task
    # entirely rather than having it return immediately, since a completed
    # task would make `wd.done()` true on the very next loop check below and
    # tear down the connection mid-turn.
    wd = asyncio.create_task(_watchdog()) if (idle > 0 or identity_owned) else None
```

(Only the watchdog body and the final `wd = ...` line change; everything else in `lugo_stream` — the receive loop, `_discover`, device MCP wiring — is untouched. `last_activity`, `idle`, `session`, `websocket` are the same pre-existing local variables the current watchdog already closes over.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_lugo_disabled_cutoff.py tests/unit/test_lugo_idle_timeout.py -v`
Expected: all pass (the new test, plus the pre-existing idle-timeout tests still green — confirming the two checks coexist without one breaking the other)

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/lugo.py tests/unit/test_lugo_disabled_cutoff.py
git commit -m "feat(auth): disabling a user/revoking a device cuts their live Lugo WS"
```

---

## Task 17: UI — Lugo branding rename + login/signup toggle

**Files:**
- Modify: `apps/api_gateway/app/static/login.html`
- Modify: `apps/api_gateway/app/static/js/auth.js`
- Modify: `apps/api_gateway/app/static/index.html` (title + `.app-title` text only)
- Modify: `apps/api_gateway/app/static/styles.css` (small structural addition, no new colors)

**Interfaces:**
- Consumes: `POST /api/auth/signup`, `POST /api/auth/login` (Task 3, now username+password).
- Produces: one login page with a Login/Create-account toggle; every "Speech Text Transformer" string in the app shell becomes "Lugo" (chrome) / "Lugo BOT" (browser tab titles).

This repo has no JS test tooling (confirmed: no `package.json`, no `*.test.js`/`*.spec.js` anywhere) — this task is verified manually, per the Global Constraints note, not with an automated red/green cycle.

- [ ] **Step 1: Write the implementation**

Replace `apps/api_gateway/app/static/login.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Lugo BOT — Log in</title>
  <link rel="stylesheet" href="/static/styles.css" />
</head>
<body>
  <div class="login-page">
    <div class="login-card">
      <h1>Lugo</h1>
      <form id="login-form" class="login-form active">
        <input type="text" id="login-username" placeholder="Username" autocomplete="username" autofocus />
        <input type="password" id="login-password" placeholder="Password" autocomplete="current-password" />
        <button type="submit">Log in</button>
        <p class="hint">New here? <a href="#" id="show-signup">Create an account</a></p>
      </form>
      <form id="signup-form" class="login-form">
        <input type="text" id="signup-username" placeholder="Username" autocomplete="username" />
        <input type="password" id="signup-password" placeholder="Password" autocomplete="new-password" />
        <button type="submit">Create account</button>
        <p class="hint">Already have an account? <a href="#" id="show-login">Log in</a></p>
      </form>
      <div id="login-status" class="login-status"></div>
    </div>
  </div>
  <script type="module" src="/static/js/auth.js"></script>
</body>
</html>
```

Replace `apps/api_gateway/app/static/js/auth.js`:

```js
const ORIGINAL_FETCH = window.fetch.bind(window);

function installUnauthorizedRedirect() {
  window.fetch = async (...args) => {
    const resp = await ORIGINAL_FETCH(...args);
    if (resp.status === 401 && !window.location.pathname.endsWith("/login.html")) {
      window.location.href = "/static/login.html";
    }
    return resp;
  };
}

function showForm(name) {
  document.getElementById("login-form").classList.toggle("active", name === "login");
  document.getElementById("signup-form").classList.toggle("active", name === "signup");
  document.getElementById("login-status").textContent = "";
}

async function handleLoginSubmit(e) {
  e.preventDefault();
  const username = document.getElementById("login-username").value;
  const password = document.getElementById("login-password").value;
  const status = document.getElementById("login-status");
  status.textContent = "";
  const resp = await ORIGINAL_FETCH("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (resp.ok) {
    window.location.href = "/ui";
  } else {
    status.textContent = "Invalid username or password";
  }
}

async function handleSignupSubmit(e) {
  e.preventDefault();
  const username = document.getElementById("signup-username").value;
  const password = document.getElementById("signup-password").value;
  const status = document.getElementById("login-status");
  status.textContent = "";
  const signupResp = await ORIGINAL_FETCH("/api/auth/signup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!signupResp.ok) {
    status.textContent = signupResp.status === 409
      ? "That username is already taken"
      : "Could not create account";
    return;
  }
  const loginResp = await ORIGINAL_FETCH("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (loginResp.ok) {
    window.location.href = "/ui";
  } else {
    status.textContent = "Account created — please log in";
    showForm("login");
  }
}

async function handleLogout() {
  await ORIGINAL_FETCH("/api/auth/logout", { method: "POST" });
  window.location.href = "/static/login.html";
}

const loginForm = document.getElementById("login-form");
if (loginForm) {
  loginForm.addEventListener("submit", handleLoginSubmit);
  document.getElementById("signup-form").addEventListener("submit", handleSignupSubmit);
  document.getElementById("show-signup").addEventListener("click", (e) => {
    e.preventDefault();
    showForm("signup");
  });
  document.getElementById("show-login").addEventListener("click", (e) => {
    e.preventDefault();
    showForm("login");
  });
} else {
  installUnauthorizedRedirect();
  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) logoutBtn.addEventListener("click", handleLogout);
}
```

Add to `apps/api_gateway/app/static/styles.css`, right after the existing `.login-status` rule:

```css
.login-form {
  display: none;
  flex-direction: column;
  gap: 12px;
}
.login-form.active {
  display: flex;
}
```

In `apps/api_gateway/app/static/index.html`, change two lines: the `<title>` (line 6) from `Speech Text Transformer` to `Lugo BOT`, and the `.app-title` span (line 22) from `Speech&nbsp;Text&nbsp;Transformer` to `Lugo BOT`.

- [ ] **Step 2: Manually verify**

Run: `uvicorn app.main:app --app-dir apps/api_gateway --reload` (or however this project's dev server is normally started — check `docs/superpowers/specs/2026-07-05-frontend-modules-and-login-design.md` if unsure) with `ADMIN_PASSWORD=s3cret` set, then in a browser:
1. Visit `/static/login.html` — page title/tab shows "Lugo BOT", card shows "Lugo", the login form is visible by default.
2. Click "Create an account" — the form swaps to username/password/"Create account", the login form hides.
3. Submit a new account — redirected to `/ui`, header shows "Lugo BOT".
4. Log out, log back in with the same credentials on the login form — succeeds.
5. Try logging in with a wrong password — "Invalid username or password" shows, page does not navigate away.

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/login.html apps/api_gateway/app/static/js/auth.js apps/api_gateway/app/static/index.html apps/api_gateway/app/static/styles.css
git commit -m "feat(ui): rebrand to Lugo/Lugo BOT + add signup toggle to the login page"
```

---

## Task 18: UI — role-gated sidebar (Users tab + hide now-admin-only tabs)

**Files:**
- Create: `apps/api_gateway/app/static/js/session.js`
- Create: `apps/api_gateway/app/static/js/users.js`
- Modify: `apps/api_gateway/app/static/index.html` (nav list + new `#section-users`)
- Modify: `apps/api_gateway/app/static/js/sidebar-nav.js`
- Modify: `apps/api_gateway/app/static/js/main.js`
- Modify: `apps/api_gateway/app/static/styles.css` (one class, no new colors)

**Interfaces:**
- Consumes: `GET /api/auth/status` (Task 3); `GET/POST/PATCH /v1/users*` (Task 6).
- Produces: `fetchAuthStatus()` (cached single fetch, `app.static.js.session`); a new "Users" nav tab, admin-only; the pre-existing "Models" and "System" tabs are now *also* admin-only client-side, matching Task 5's guard change (they were reachable by any logged-in user before this plan; `/v1/models` and `/v1/system` are now in `_ADMIN_PREFIXES`, so a regular user hitting them would 403 — hiding the tabs client-side is a UX nicety on top of that real server-side boundary, not a substitute for it).

No JS test tooling in this repo (see Task 17) — verified manually.

- [ ] **Step 1: Write the implementation**

```js
// apps/api_gateway/app/static/js/session.js
let _statusPromise = null;

export function fetchAuthStatus() {
  if (!_statusPromise) {
    _statusPromise = fetch("/api/auth/status").then((r) => r.json());
  }
  return _statusPromise;
}
```

```js
// apps/api_gateway/app/static/js/users.js
import { el, print } from "./helpers.js";

export let userData = [];

export async function loadUsers() {
  try {
    const body = await (await fetch("/v1/users")).json();
    userData = body.data || [];
    renderUserList();
  } catch {
    /* ignore */
  }
}

function _escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function renderUserList() {
  const host = el("user-list");
  if (!host) return;
  if (!userData.length) {
    host.innerHTML = '<p class="hint">No users yet.</p>';
    return;
  }
  host.innerHTML = userData.map((u) => `
    <div class="model-row ${u.disabled ? "dim" : ""}">
      <div class="model-info">
        <strong>${_escapeHtml(u.username)}</strong>
        <select data-user-role="${u.id}">
          <option value="user" ${u.role === "user" ? "selected" : ""}>user</option>
          <option value="admin" ${u.role === "admin" ? "selected" : ""}>admin</option>
        </select>
        <label><input type="checkbox" data-user-testing="${u.id}" ${u.can_use_testing ? "checked" : ""} /> Testing</label>
        <span class="hint">${u.disabled ? "Disabled" : "Active"}</span>
      </div>
      <div class="model-action">
        <button class="mini" data-user-toggle-disabled="${u.id}">${u.disabled ? "Enable" : "Disable"}</button>
        <button class="mini" data-user-reset="${u.id}">Reset password</button>
      </div>
    </div>
  `).join("");

  document.querySelectorAll("[data-user-role]").forEach((sel) =>
    sel.addEventListener("change", () => updateUser(sel.getAttribute("data-user-role"), { role: sel.value }))
  );
  document.querySelectorAll("[data-user-testing]").forEach((cb) =>
    cb.addEventListener("change", () =>
      updateUser(cb.getAttribute("data-user-testing"), { can_use_testing: cb.checked })
    )
  );
  document.querySelectorAll("[data-user-toggle-disabled]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-user-toggle-disabled");
      const user = userData.find((u) => u.id === id);
      updateUser(id, { disabled: !user.disabled });
    })
  );
  document.querySelectorAll("[data-user-reset]").forEach((btn) =>
    btn.addEventListener("click", () => resetUserPassword(btn.getAttribute("data-user-reset")))
  );
}

async function updateUser(id, fields) {
  try {
    const resp = await fetch(`/v1/users/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("user-status"), body.detail || "Update failed", true);
      return;
    }
    await loadUsers();
  } catch (error) {
    print(el("user-status"), String(error), true);
  }
}

async function resetUserPassword(id) {
  const newPassword = prompt("New password for this user:");
  if (!newPassword) return;
  try {
    const resp = await fetch(`/v1/users/${encodeURIComponent(id)}/reset_password`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_password: newPassword }),
    });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("user-status"), body.detail || "Reset failed", true);
      return;
    }
    print(el("user-status"), "Password reset");
  } catch (error) {
    print(el("user-status"), String(error), true);
  }
}

export async function createUser() {
  const username = el("user-add-username").value.trim();
  const password = el("user-add-password").value;
  const role = el("user-add-role").value;
  const status = el("user-status");
  if (!username || !password) {
    print(status, "Enter both username and password", true);
    return;
  }
  try {
    const resp = await fetch("/v1/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || JSON.stringify(body), true);
      return;
    }
    status.textContent = `Created "${username}"`;
    el("user-add-username").value = "";
    el("user-add-password").value = "";
    await loadUsers();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("user-add-btn")) el("user-add-btn").addEventListener("click", createUser);
if (el("user-refresh")) el("user-refresh").addEventListener("click", loadUsers);
```

In `apps/api_gateway/app/static/index.html`, in the `<nav class="sidebar">` list: add `class="admin-only"` to the existing "Models" and "System" `<li>` elements (lines ~61 and ~73), and add two new `<li>`s — a always-visible "Devices" item right after "Livehost", and an admin-only "Users" item right after "MCP" (before "System"):

```html
            <li>
              <button class="nav-item" data-section="devices">
                <span class="nav-icon">&#9635;</span>
                <span class="nav-label">Devices</span>
              </button>
            </li>
```

(inserted after the existing "Livehost" `<li>`, before "STT")

```html
            <li class="admin-only">
              <button class="nav-item" data-section="users">
                <span class="nav-icon">&#9689;</span>
                <span class="nav-label">Users</span>
              </button>
            </li>
```

(inserted after the existing "MCP" `<li>`, before "System")

Add the new section markup right before the existing `<div class="section" id="section-system">`:

```html
          <!-- ============================== USERS ============================== -->
          <div class="section" id="section-users">
            <section class="card">
              <div class="card-head">
                <h2>Users</h2>
                <button id="user-refresh" class="ghost mini">Refresh</button>
              </div>
              <p class="hint">Manage accounts, roles, and testing-model access.</p>
              <div id="user-list" class="model-list">
                <p class="hint">Loading&#8230;</p>
              </div>
              <h3 class="sub">Create User</h3>
              <div class="row tight">
                <label>
                  Username
                  <input id="user-add-username" type="text" placeholder="username" />
                </label>
                <label>
                  Password
                  <input id="user-add-password" type="password" placeholder="password" />
                </label>
                <label>
                  Role
                  <select id="user-add-role">
                    <option value="user" selected>user</option>
                    <option value="admin">admin</option>
                  </select>
                </label>
                <div class="actions end">
                  <button id="user-add-btn">Create</button>
                </div>
              </div>
              <p id="user-status" class="meta"></p>
            </section>
          </div>
```

Replace `apps/api_gateway/app/static/js/sidebar-nav.js`:

```js
import { el } from "./helpers.js";
import { loadRecommend } from "./model-recommender.js";
import { loadMcpServers } from "./mcp-servers.js";
import { loadUsers } from "./users.js";
import { fetchAuthStatus } from "./session.js";

function activateSection(section) {
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-section") === section);
  });
  document.querySelectorAll(".section").forEach((s) => {
    s.classList.toggle("active", s.id === `section-${section}`);
  });
  if (section === "models") loadRecommend();
  if (section === "mcp") loadMcpServers();
  if (section === "users") loadUsers();
}

export async function initSidebar() {
  const status = await fetchAuthStatus();
  if (status.authenticated && status.role === "admin") {
    document.querySelectorAll(".admin-only").forEach((li) => {
      li.classList.remove("admin-only");
    });
  }

  const validSections = Array.from(document.querySelectorAll(".nav-item")).map((b) =>
    b.getAttribute("data-section")
  );

  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const section = btn.getAttribute("data-section");
      activateSection(section);
      const url = new URL(window.location.href);
      url.searchParams.set("tab", section);
      window.history.replaceState(null, "", url);
    });
  });

  const requestedTab = new URLSearchParams(window.location.search).get("tab");
  if (requestedTab && validSections.includes(requestedTab)) {
    activateSection(requestedTab);
  }

  const toggle = el("sidebar-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      el("sidebar").classList.toggle("collapsed");
    });
  }
}
```

In `apps/api_gateway/app/static/js/main.js`, add `import "./users.js";` and `import "./devices.js";` (Task 19) to the side-effect-only imports block (alongside `"./tts-batch.js"`/`"./tts-stream.js"`/`"./sessions.js"`). `loadUsers`/`loadMyDevices` are called on tab-activate by `sidebar-nav.js`, not eagerly at boot — unlike `loadModels`/`loadMcpServers`, a non-admin's page load shouldn't fire an admin-only request that will just 403.

Add to `apps/api_gateway/app/static/styles.css`, right after the `:root` block's closing brace (or anywhere with the other simple utility rules):

```css
.admin-only {
  display: none;
}
```

- [ ] **Step 2: Manually verify**

With `ADMIN_PASSWORD` set and a running server:
1. Log in as the bootstrap admin — sidebar shows Devices, Users, Models, System (all visible).
2. Sign up a second, regular-role account, log in as it — sidebar shows Chat/Livehost/STT/TTS/MCP/Devices only; Models/System/Users are absent.
3. As the regular user, manually visit `/ui?tab=system` — the tab list still doesn't show System as active/populated (guarded server-side; at minimum confirm no crash/blank white page).
4. As admin, click "Users" — the table loads, shows both accounts. Toggle the regular user's "Disable" — status flips to Disabled; toggle back.
5. Use "Create User" to add a third account with role `admin`; log in as it — Models/System/Users are visible immediately.

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/js/session.js apps/api_gateway/app/static/js/users.js apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/sidebar-nav.js apps/api_gateway/app/static/js/main.js apps/api_gateway/app/static/styles.css
git commit -m "feat(ui): add role-gated Users tab; hide Models/System from non-admins"
```

---

## Task 19: UI — Devices tab (own devices, admin all-devices, pairing claim)

**Files:**
- Create: `apps/api_gateway/app/static/js/devices.js`
- Modify: `apps/api_gateway/app/static/index.html` (new `#section-devices`)

**Interfaces:**
- Consumes: `GET /v1/devices/mine`, `POST /v1/devices/mine/{id}/revoke`, `GET /v1/devices`, `POST /v1/devices/{id}/revoke`, `POST /v1/devices/pair/claim` (Task 9); `fetchAuthStatus` (Task 18).
- Produces: a "Devices" tab visible to every logged-in user, showing their own devices plus a claim form; admins additionally see an all-devices table with an owner column.

No JS test tooling in this repo (see Task 17) — verified manually.

- [ ] **Step 1: Write the implementation**

```js
// apps/api_gateway/app/static/js/devices.js
import { el, print } from "./helpers.js";
import { fetchAuthStatus } from "./session.js";

export let myDeviceData = [];
export let allDeviceData = [];

function _escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function _deviceRow(d, ownerLabel, revokeAttr) {
  return `
    <div class="model-row ${d.revoked ? "dim" : ""}">
      <div class="model-info">
        <strong>${_escapeHtml(d.name)}</strong>
        ${ownerLabel}
        <code>${_escapeHtml(d.serial)}</code>
        <span class="hint">${d.last_seen_at ? "last seen " + _escapeHtml(d.last_seen_at) : "never connected"}</span>
      </div>
      <div class="model-action">
        <button class="mini danger" ${revokeAttr}="${d.id}" ${d.revoked ? "disabled" : ""}>Revoke</button>
      </div>
    </div>
  `;
}

export async function loadMyDevices() {
  try {
    const body = await (await fetch("/v1/devices/mine")).json();
    myDeviceData = body.data || [];
    renderMyDeviceList();
  } catch {
    /* ignore */
  }
  await maybeLoadAllDevices();
}

function renderMyDeviceList() {
  const host = el("device-mine-list");
  if (!host) return;
  if (!myDeviceData.length) {
    host.innerHTML = '<p class="hint">No devices paired yet.</p>';
    return;
  }
  host.innerHTML = myDeviceData.map((d) => _deviceRow(d, "", "data-device-revoke-mine")).join("");
  document.querySelectorAll("[data-device-revoke-mine]").forEach((btn) =>
    btn.addEventListener("click", () => revokeMyDevice(btn.getAttribute("data-device-revoke-mine")))
  );
}

async function maybeLoadAllDevices() {
  const status = await fetchAuthStatus();
  const section = el("device-all-section");
  if (!(status.authenticated && status.role === "admin")) {
    if (section) section.classList.add("hidden");
    return;
  }
  if (section) section.classList.remove("hidden");
  try {
    const body = await (await fetch("/v1/devices")).json();
    allDeviceData = body.data || [];
    renderAllDeviceList();
  } catch {
    /* ignore */
  }
}

function renderAllDeviceList() {
  const host = el("device-all-list");
  if (!host) return;
  if (!allDeviceData.length) {
    host.innerHTML = '<p class="hint">No devices paired yet.</p>';
    return;
  }
  host.innerHTML = allDeviceData
    .map((d) => _deviceRow(d, `<span class="hint">owner: ${_escapeHtml(d.owner_username)}</span>`, "data-device-revoke-any"))
    .join("");
  document.querySelectorAll("[data-device-revoke-any]").forEach((btn) =>
    btn.addEventListener("click", () => revokeAnyDevice(btn.getAttribute("data-device-revoke-any")))
  );
}

async function revokeMyDevice(id) {
  if (!confirm("Revoke this device? It will need to be paired again.")) return;
  try {
    const resp = await fetch(`/v1/devices/mine/${encodeURIComponent(id)}/revoke`, { method: "POST" });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("device-status"), body.detail || "Revoke failed", true);
      return;
    }
    await loadMyDevices();
  } catch (error) {
    print(el("device-status"), String(error), true);
  }
}

async function revokeAnyDevice(id) {
  if (!confirm("Revoke this device? It will need to be paired again.")) return;
  try {
    const resp = await fetch(`/v1/devices/${encodeURIComponent(id)}/revoke`, { method: "POST" });
    if (!resp.ok) {
      const body = await resp.json();
      print(el("device-status"), body.detail || "Revoke failed", true);
      return;
    }
    await maybeLoadAllDevices();
  } catch (error) {
    print(el("device-status"), String(error), true);
  }
}

export async function claimDevice() {
  const status = el("device-status");
  const name = el("device-pair-name").value.trim();
  const code = el("device-pair-code").value.trim();
  if (!name || !code) {
    print(status, "Enter both the code shown on the device and a name for it", true);
    return;
  }
  try {
    const resp = await fetch("/v1/devices/pair/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Pairing failed", true);
      return;
    }
    status.textContent = `Paired "${name}"`;
    el("device-pair-name").value = "";
    el("device-pair-code").value = "";
    await loadMyDevices();
  } catch (error) {
    print(status, String(error), true);
  }
}

if (el("device-pair-btn")) el("device-pair-btn").addEventListener("click", claimDevice);
if (el("device-refresh")) el("device-refresh").addEventListener("click", loadMyDevices);
```

Add to `apps/api_gateway/app/static/index.html`, right before `<div class="section" id="section-tts">` (i.e. between "STT" and "TTS" sections is fine, or anywhere among the other sections — nav order, not markup order, drives which tab shows first):

```html
          <!-- ============================== DEVICES ============================== -->
          <div class="section" id="section-devices">
            <section class="card">
              <div class="card-head">
                <h2>My Devices</h2>
                <button id="device-refresh" class="ghost mini">Refresh</button>
              </div>
              <p class="hint">Pair an ESP32 or RPi client to your account. Enter the code shown on the device.</p>
              <div id="device-mine-list" class="model-list">
                <p class="hint">Loading&#8230;</p>
              </div>
              <h3 class="sub">Add Device</h3>
              <div class="row tight">
                <label>
                  Code shown on device
                  <input id="device-pair-code" type="text" placeholder="123456" />
                </label>
                <label>
                  Name
                  <input id="device-pair-name" type="text" placeholder="ESP32 desk" />
                </label>
                <div class="actions end">
                  <button id="device-pair-btn">Pair</button>
                </div>
              </div>
              <p id="device-status" class="meta"></p>
            </section>
            <section class="card hidden" id="device-all-section">
              <div class="card-head">
                <h2>All Devices</h2>
              </div>
              <p class="hint">Every paired device, across all accounts.</p>
              <div id="device-all-list" class="model-list"></div>
            </section>
          </div>
```

And in the sidebar `<nav>` list (already added `data-section="devices"` in Task 18) — no further change needed there.

In `apps/api_gateway/app/static/js/sidebar-nav.js`, add the load call for the new tab (alongside the existing `if (section === "mcp") ...` line):

```js
if (section === "devices") loadMyDevices();
```

(and the corresponding import: `import { loadMyDevices } from "./devices.js";`)

In `apps/api_gateway/app/static/js/main.js`, the `import "./devices.js";` side-effect import was already added in Task 18's diff — confirm it's present (it registers `device-pair-btn`/`device-refresh` listeners at module load, same pattern as `users.js`).

- [ ] **Step 2: Manually verify**

1. As a regular logged-in user, open "Devices" — "My Devices" is empty, "All Devices" card is absent (`hidden` class).
2. In a second terminal, drive the pairing handshake directly against the API to simulate a device (no real ESP32 needed for this check):
   `curl -s -X POST localhost:8000/v1/devices/pair/init -H 'Content-Type: application/json' -d '{"serial":"AA:BB:CC"}'` — note the returned `code`.
3. Back in the browser, enter that `code` and a name, click "Pair" — status shows "Paired…", the device appears in "My Devices".
4. Poll pairing status to confirm the "device" side got its token: `curl -s "localhost:8000/v1/devices/pair/status?poll_token=<poll_token from step 2>"` returns `claimed: true` with a `token`.
5. Click "Revoke" on the paired device — confirm dialog, then it disappears/greys out; a repeat pairing with the same serial (`pair/init` + `pair/claim` again) now succeeds (since the prior device row is revoked).
6. Log in as an admin — "All Devices" card appears, listing every account's devices with an owner column.

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/js/devices.js apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/sidebar-nav.js
git commit -m "feat(ui): add Devices tab with pairing claim + admin all-devices view"
```

---

## Final Verification

- [ ] Run the full backend test suite: `pytest -v`. Expected: all tests pass (this plan's new tests plus every pre-existing test still green — in particular, confirm nothing in `tests/unit/test_conversation_*`, `tests/unit/test_livehost_*`, `tests/unit/test_lugo_*`, `tests/integration/test_*` regressed from the WS-route loop rewrites in Tasks 14–16).
- [ ] `grep -rn "ws_authenticated\|session\[.authenticated.\]\|session.get(.authenticated.)" apps/ tests/` returns nothing — confirms the old boolean auth model was fully retired, not left as dead code alongside the new one.
- [ ] Manually walk the flow end-to-end once, in order: sign up → log in → (as admin) create a second user + bootstrap-verify role split (Models/System hidden for the regular user) → pair a device via curl + claim it in the Devices tab → disable that user from the Users tab → confirm any of that user's open WS connections (if you have a live one open in another tab) close within ~30s.

