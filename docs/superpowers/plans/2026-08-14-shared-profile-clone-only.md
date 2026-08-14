# Shared Profiles Become Clone-Only Templates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `Profile` marked `shared` may be read and cloned, but never run in a conversation and never bound to a device; only an admin may create, edit, delete, or mark one shared.

**Architecture:** Add a `Profile.shared` boolean (stored inside the existing `config_profiles.data` JSON blob — no DDL). Split today's single `visible` rule into three: `visible` (read/clone), `usable` (run/bind), `writable` (mutate). Six consumer call sites move from `visible_profile_or_none` to a new `usable_profile_or_none`. An idempotent boot migration converts today's `owner_id is None` templates, handing the ones with a bound device to that device's owner so deployed fleets keep running.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async + SQLite, pytest, vanilla ES-module static JS.

**Spec:** `docs/superpowers/specs/2026-08-14-shared-profile-clone-only-design.md`

## Global Constraints

- Run **only the targeted test case** while working (`python -m pytest tests/unit/... -k <name>`). The full suite is a once-at-the-end gate — overlapping full runs deadlock this repo's pytest concurrency guard.
- Commit as `lugondev <lugondev@gmail.com>`: `git -c user.name=lugondev -c user.email=lugondev@gmail.com commit`.
- Branch is `feat/shared-profile-clone-only`. Do **not** push — `main` auto-deploys to prod.
- `make lint` (ruff) must pass before the final commit.
- **Never** widen the error message for a profile that is missing or owned by someone else. `app/services/profile_visibility.py`'s no-enumeration-oracle contract holds for those two cases and only stops applying to `shared` rows (which `GET /v1/profiles` already lists to everyone).
- Never `SELECT *` on `config_profiles` in a shell — it stores `llm.api_key` inline.
- Route modules keep their own module-level store bindings (`from app.services.profiles.store import profile_store` at module top). Tests monkeypatch those per-route names; never refactor a store lookup out of a route module.
- Exact new message text, used verbatim everywhere: `profile '<name>' is a shared template; clone it before using it`

---

### Task 1: `Profile.shared` field and the three predicates

**Files:**
- Modify: `apps/api_gateway/app/services/profiles/models.py:46-59`
- Modify: `apps/api_gateway/app/services/profile_visibility.py`
- Test: `tests/unit/profiles/test_profile_visibility_predicates.py` (create)

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `Profile.shared: bool = False`
  - `profile_usable(profile: Profile, caller_id: str | None) -> bool`
  - `usable_profile_or_none(profile: Profile | None, caller_id: str | None, *, bypass: bool = False) -> Profile | None`
  - `is_shared_template(profile: Profile | None) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/profiles/test_profile_visibility_predicates.py`:

```python
"""The three-way split of the old single visibility rule.

`visible` governs reads (list/get/clone-source/health): a shared template is
readable by everyone. `usable` governs RUNNING on a profile (conversation, WS,
stt warm, session resume, device bind): a shared template is usable by NOBODY,
including the admin who owns it -- that is the whole point of "clone-only".
"""

import pytest

from app.services.profile_visibility import (
    is_shared_template,
    profile_usable,
    profile_visible,
    usable_profile_or_none,
    visible_profile_or_none,
)
from app.services.profiles.models import Profile


def _p(**kw) -> Profile:
    return Profile(name="p", **kw)


OWNED = _p(owner_id="alice")
OTHERS = _p(owner_id="bob")
SHARED_OWNED = _p(owner_id="alice", shared=True)
SHARED_OWNERLESS = _p(owner_id=None, shared=True)
DEV_MODE = _p(owner_id=None)  # auth disabled: caller_id is None too


@pytest.mark.parametrize(
    "profile,caller,visible,usable",
    [
        (OWNED, "alice", True, True),
        (OWNED, "bob", False, False),
        (OTHERS, "alice", False, False),
        # Shared is readable by everyone and runnable by no one -- including
        # its own owner, who must clone it like anybody else.
        (SHARED_OWNED, "alice", True, False),
        (SHARED_OWNED, "bob", True, False),
        (SHARED_OWNERLESS, "alice", True, False),
        (SHARED_OWNERLESS, None, True, False),
        # Dev mode (settings.auth_enabled False): owner_id and caller_id are
        # both None, so the owned path matches and nothing changes.
        (DEV_MODE, None, True, True),
    ],
)
def test_predicate_table(profile, caller, visible, usable):
    assert profile_visible(profile, caller) is visible
    assert profile_usable(profile, caller) is usable


def test_default_is_not_shared():
    assert Profile(name="p").shared is False


def test_usable_or_none_collapses_shared_to_none():
    assert usable_profile_or_none(SHARED_OWNED, "alice") is None
    assert visible_profile_or_none(SHARED_OWNED, "alice") is SHARED_OWNED


def test_bypass_does_not_resurrect_a_shared_template():
    """`bypass` exists for dev mode's "no way to prove ownership of anything".
    Shared is not an ownership question -- nobody may run on it, so bypass must
    not become a back door into one."""
    assert usable_profile_or_none(SHARED_OWNED, None, bypass=True) is None
    assert usable_profile_or_none(OTHERS, None, bypass=True) is OTHERS


def test_is_shared_template_tolerates_none():
    assert is_shared_template(None) is False
    assert is_shared_template(OWNED) is False
    assert is_shared_template(SHARED_OWNED) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/profiles/test_profile_visibility_predicates.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'is_shared_template'`.

- [ ] **Step 3: Add the field**

In `apps/api_gateway/app/services/profiles/models.py`, inside `class Profile`, directly after `owner_id`:

```python
    owner_id: str | None = None
    # A shared profile is a CLONE-ONLY template: readable by everyone, runnable
    # by no one (not even its owner), bindable to no device, writable only by an
    # admin. See services/profile_visibility.py for the three predicates and
    # docs/superpowers/specs/2026-08-14-shared-profile-clone-only-design.md.
    shared: bool = False
```

- [ ] **Step 4a: Widen `profile_visible` so a shared row is visible to everyone**

A shared profile keeps the `owner_id` of the admin who made it, so the existing
`owner_id is None or owner_id == caller` rule would hide it from everyone else —
the exact opposite of what a template is for. Replace `profile_visible`'s body
(leave `tts_profile_visible` alone; `TtsProfile` has no `shared` field):

```python
def profile_visible(profile: Profile, caller_id: str | None) -> bool:
    """Read access: a shared template is readable by everyone, and your own
    rows are readable by you.

    `owner_id is None` is deliberately NOT a visibility grant any more. It used
    to be the entire definition of "template"; it now means only "nobody owns
    this row", and the boot migration (services/profiles/shared_migration.py)
    guarantees every such row is also `shared`. The one case that reaches here
    unmigrated is dev mode (settings.auth_enabled False), where caller_id is
    None too and the ownership arm matches.
    """
    return profile.shared or profile.owner_id == caller_id
```

Also update the module docstring's rule sentence — it currently reads "A
profile/tts-profile is visible to a caller iff it's a template (``owner_id is
None`` -- visible to everyone) or the caller owns it." Replace that paragraph
with:

```
A Profile is visible to a caller iff it's shared (a clone-only template --
visible to everyone) or the caller owns it. A TtsProfile keeps the older rule
(``owner_id is None`` = template) -- it has no ``shared`` flag and is out of
scope for the 2026-08-14 design.

Visibility is not permission to RUN: see profile_usable() below.
```

- [ ] **Step 4b: Append the new predicates**

Append to `apps/api_gateway/app/services/profile_visibility.py`:

```python
def profile_usable(profile: Profile, caller_id: str | None) -> bool:
    """May this caller RUN on this profile (conversation, WS, /stt/warm,
    session resume, device binding)?

    Strictly narrower than profile_visible(): a shared template is visible to
    everyone and usable by nobody -- including the admin whose owner_id is on
    it. That asymmetry IS the feature: a shared profile exists to be cloned,
    and a clone is what you run."""
    return not profile.shared and profile.owner_id == caller_id


def usable_profile_or_none(
    profile: Profile | None, caller_id: str | None, *, bypass: bool = False
) -> Profile | None:
    """usable() counterpart of visible_profile_or_none, with the identical
    "collapse to None" contract.

    `bypass` keeps its existing single legitimate caller (dev mode's
    WsIdentity.unauthenticated -- see visible_profile_or_none). It waives the
    OWNERSHIP half only: `shared` is not an ownership question, so a bypassing
    caller still may not run on a template."""
    if profile is None or profile.shared:
        return None
    if bypass or profile_usable(profile, caller_id):
        return profile
    return None


def is_shared_template(profile: Profile | None) -> bool:
    """True for a row every caller can already see listed by GET /v1/profiles.

    Call sites use this to decide whether they may NAME the profile in a
    rejection message. The no-enumeration-oracle contract at the top of this
    module governs "missing" vs "someone else's" and is untouched -- a shared
    row's existence is public, so saying so leaks nothing."""
    return profile is not None and profile.shared
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/unit/profiles/test_profile_visibility_predicates.py -q`
Expected: PASS (12 passed).

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/profiles/models.py \
        apps/api_gateway/app/services/profile_visibility.py \
        tests/unit/profiles/test_profile_visibility_predicates.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(profiles): add Profile.shared and the usable/visible predicate split"
```

---

### Task 2: CRUD routes honor `shared`

**Files:**
- Modify: `apps/api_gateway/app/schemas/profiles.py:7-17`
- Modify: `apps/api_gateway/app/api/routes/profiles.py:130-155` (create), `:166-210` (update), `:242-269` (clone)
- Test: `tests/unit/profiles/test_profile_shared_crud.py` (create)

**Interfaces:**
- Consumes: `Profile.shared` (Task 1).
- Produces: `POST/PUT /v1/profiles` accept a `shared` boolean; every newly created profile carries `owner_id = current_user_id(request)` regardless of role. A `PUT` that flips `shared` False→True on a profile with bound devices returns 409.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/profiles/test_profile_shared_crud.py`:

```python
"""CRUD half of clone-only shared profiles.

The load-bearing change is in create: `owner_id = None if is_admin` used to
mean an admin could not own a profile at all -- every one they made was a
template. Ownership and sharing are now independent, so an admin gets a normal
owned profile unless they explicitly ask for a shared one.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.store import profile_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """tests/conftest.py's autouse `_hermetic` blanks the admin password, which
    turns auth off entirely. These tests need real roles -- same pattern as
    test_profile_idor.py."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient, role: str = "user") -> str:
    username = f"{role}-{uuid.uuid4().hex[:10]}"
    password = "s3cret-password"
    assert client.post(
        "/api/auth/signup", json={"username": username, "password": password}
    ).status_code == 200
    if role == "admin":
        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    assert client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).status_code == 200
    return asyncio.run(user_store.get_by_username(username)).id


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def test_admin_create_is_owned_not_shared_by_default(client, _with_password):
    admin_id = _as_user(client, "admin")
    name = _rand("adm")
    resp = client.post("/v1/profiles", json={"name": name})
    assert resp.status_code == 200, resp.text
    row = profile_store.get(name)
    assert row.owner_id == admin_id, "admin profiles used to be ownerless templates"
    assert row.shared is False


def test_admin_can_create_a_shared_template(client, _with_password):
    admin_id = _as_user(client, "admin")
    name = _rand("tpl")
    assert client.post("/v1/profiles", json={"name": name, "shared": True}).status_code == 200
    row = profile_store.get(name)
    assert row.shared is True
    assert row.owner_id == admin_id, "a shared row still records who made it"


def test_non_admin_cannot_create_a_shared_profile(client, _with_password):
    _as_user(client, "user")
    name = _rand("usr")
    # Silently dropped, not 403 -- same contract as mcp_servers (profiles.py),
    # so the profile editor needs no new error path.
    assert client.post("/v1/profiles", json={"name": name, "shared": True}).status_code == 200
    assert profile_store.get(name).shared is False


def test_non_admin_cannot_flip_shared_on_their_own_profile(client, _with_password):
    _as_user(client, "user")
    name = _rand("usr")
    assert client.post("/v1/profiles", json={"name": name}).status_code == 200
    assert client.put(f"/v1/profiles/{name}", json={"name": name, "shared": True}).status_code == 200
    assert profile_store.get(name).shared is False


def test_admin_update_can_flip_shared_both_ways(client, _with_password):
    _as_user(client, "admin")
    name = _rand("adm")
    assert client.post("/v1/profiles", json={"name": name}).status_code == 200
    assert client.put(f"/v1/profiles/{name}", json={"name": name, "shared": True}).status_code == 200
    assert profile_store.get(name).shared is True
    assert client.put(f"/v1/profiles/{name}", json={"name": name, "shared": False}).status_code == 200
    assert profile_store.get(name).shared is False


def test_update_preserves_shared_when_payload_omits_it(client, _with_password):
    """ProfileRequest.shared defaults to False, so an admin editing an unrelated
    field with a client that predates this feature must not silently un-share."""
    _as_user(client, "admin")
    name = _rand("adm")
    assert client.post("/v1/profiles", json={"name": name, "shared": True}).status_code == 200
    assert client.put(
        f"/v1/profiles/{name}", json={"name": name, "nickname": "renamed"}
    ).status_code == 200
    row = profile_store.get(name)
    assert row.nickname == "renamed"
    assert row.shared is True


def test_clone_of_a_shared_template_is_owned_and_not_shared(client, _with_password):
    admin = TestClient(app)
    _as_user(admin, "admin")
    tpl = _rand("tpl")
    assert admin.post("/v1/profiles", json={"name": tpl, "shared": True}).status_code == 200

    user_id = _as_user(client, "user")
    copy = _rand("copy")
    resp = client.post(f"/v1/profiles/{tpl}/clone", json={"new_name": copy})
    assert resp.status_code == 200, resp.text
    row = profile_store.get(copy)
    assert row.owner_id == user_id
    assert row.shared is False, "a clone is a working profile, never another template"


def test_sharing_a_profile_with_bound_devices_is_refused(client, _with_password):
    """Otherwise the admin creates exactly the state this feature exists to
    prevent: a device bound to a profile it is no longer allowed to run, which
    would silently degrade to server defaults on its next connect."""
    user_id = _as_user(client, "admin")
    name = _rand("bound")
    assert client.post("/v1/profiles", json={"name": name}).status_code == 200
    asyncio.run(device_store.create(user_id, "speaker", _rand("serial"), profile_id=name))
    device_id = asyncio.run(device_store.list_for_user(user_id))[0]["id"]

    resp = client.put(f"/v1/profiles/{name}", json={"name": name, "shared": True})
    assert resp.status_code == 409, resp.text
    assert device_id in resp.json()["detail"], "the admin needs to know WHICH devices to fix"
    assert profile_store.get(name).shared is False


def test_unsharing_is_never_blocked_by_devices(client, _with_password):
    """The 409 guards one direction only -- going back to a private profile
    creates no dangling binding."""
    _as_user(client, "admin")
    name = _rand("tpl")
    assert client.post("/v1/profiles", json={"name": name, "shared": True}).status_code == 200
    assert client.put(f"/v1/profiles/{name}", json={"name": name, "shared": False}).status_code == 200
    assert profile_store.get(name).shared is False
```

- [ ] **Step 2: Check `device_store.create`'s real signature**

Run: `sed -n 44,66p apps/api_gateway/app/services/auth/devices.py`

The test above calls it as `device_store.create(user_id, name, serial, profile_id=...)`. If the real signature differs (extra required argument, different order), adjust the two `device_store.create(...)` calls in this file to match — everything else in the test is independent of it.

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/unit/profiles/test_profile_shared_crud.py -x -q`
Expected: FAIL on the first test — `row.owner_id` is `None`, not the admin's id.

- [ ] **Step 4: Add `shared` to the request schema**

In `apps/api_gateway/app/schemas/profiles.py`, add to `ProfileRequest` after `voice_optimized`:

```python
    # Admin-only; silently forced to False for anyone else (see
    # api/routes/profiles.py), the same way mcp_servers is.
    shared: bool = False
```

- [ ] **Step 5: Rewrite `create_profile`'s ownership block**

In `apps/api_gateway/app/api/routes/profiles.py`, replace lines 138-151 (from `is_admin = ...` through `profile = Profile(**data, owner_id=owner_id)`):

```python
    is_admin = current_role(request) == "admin"
    # Ownership and sharing are independent now. This used to read
    # `None if is_admin else current_user_id(...)`, which meant an admin could
    # not own a profile at all -- every one they made was a template. See
    # docs/superpowers/specs/2026-08-14-shared-profile-clone-only-design.md.
    owner_id = current_user_id(request)
    data = payload.model_dump()
    if not is_admin:
        # C1: mcp_servers carries an arbitrary url+headers that
        # _build_tool_registry fetches and reflects into the LLM turn -- the
        # same SSRF-with-reflection primitive Task 6 gated on /v1/mcp/servers
        # for admins only. A non-admin setting it here would fully bypass that
        # gate. Silently drop rather than 403: doesn't need a special error path
        # in the profile editor UI -- the field just doesn't take.
        data["mcp_servers"] = []
        # Same "the field just doesn't take" treatment: publishing a template is
        # an admin act.
        data["shared"] = False
    profile = Profile(**data, owner_id=owner_id)
```

- [ ] **Step 6: Guard `shared` in `update_profile`**

In the same file, inside `update_profile`'s `if existing:` branch, after the `api_key` preservation block and alongside the existing `if not is_admin:` handling, make the non-admin branch also preserve `shared`, and add the bound-device guard before `profile_store.upsert(profile)`.

Replace the `if not is_admin:` block inside `if existing:` with:

```python
        if not is_admin:
            # C1: a non-admin PUT must not be able to add/change mcp_servers
            # (same SSRF-with-reflection gate as create_profile below). Silently
            # preserve whatever was already stored rather than 403.
            data["mcp_servers"] = [s.model_dump() for s in existing.mcp_servers]
            # Same for shared: only an admin publishes or withdraws a template.
            # Preserved, not forced False -- ProfileRequest.shared defaults to
            # False, so forcing would let any non-admin edit un-share a row.
            data["shared"] = existing.shared
```

And in the `else:` branch (PUT-as-create), after `data["owner_id"] = ...`:

```python
    else:
        data["owner_id"] = current_user_id(request)
        if not is_admin:
            data["mcp_servers"] = []
            data["shared"] = False
```

(Note the `owner_id` line changes here too — PUT-as-create must match
`create_profile`'s new rule, not the old `None if is_admin`.)

Then, immediately before `profile_store.upsert(profile)` in `update_profile`:

```python
    if profile.shared and existing is not None and not existing.shared:
        # Sharing a profile that devices still point at would leave those
        # devices bound to something they may no longer run -- they would
        # silently fall back to server defaults on the next connect. Make the
        # admin reassign them first. delete_profile has the same concern and
        # solves it by sweeping (clear_profile); sharing must not silently
        # unassign someone's speaker, so it refuses instead.
        bound = [d["id"] for d in await device_store.list_all() if d["profile_id"] == name]
        if bound:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{name}' still has {len(bound)} device(s) bound to it "
                    f"({', '.join(bound)}); reassign them before sharing it"
                ),
            )
```

- [ ] **Step 7: Force `shared=False` on clones**

In `clone_profile`, after `data["owner_id"] = user_id`:

```python
    # A clone is a working profile, never another template -- even when an admin
    # clones a shared row.
    data["shared"] = False
```

- [ ] **Step 8: Run test to verify it passes**

Run: `python -m pytest tests/unit/profiles/test_profile_shared_crud.py -q`
Expected: PASS (9 passed).

- [ ] **Step 9: Run the existing profile route + IDOR tests for regressions**

Run: `python -m pytest tests/unit/profiles/test_profiles_routes.py tests/unit/profiles/test_profile_idor.py tests/unit/profiles/test_profile_ownership.py -q`

Expected: some failures. Tests written against the old "admin creates templates" rule (e.g. ones asserting `owner_id is None` after an admin POST, or that an admin-made profile is visible to another user) now describe behavior this feature deliberately removes. For each failure, update the test to create its template with `{"shared": True}` and assert on `shared` rather than on `owner_id is None`. Do **not** weaken any assertion about a *private* profile being indistinguishable from a missing one — those are the C2 no-oracle guards and must still pass unchanged.

- [ ] **Step 10: Commit**

```bash
git add apps/api_gateway/app/schemas/profiles.py \
        apps/api_gateway/app/api/routes/profiles.py \
        tests/unit/profiles/
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(profiles): admin-only shared flag, owned admin profiles, clone-never-shared"
```

---

### Task 3: Consumers refuse to run on a shared profile

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py:46` (import), `:172`, `:354`
- Modify: `apps/api_gateway/app/api/routes/lugo.py:33` (import), `:105`
- Modify: `apps/api_gateway/app/api/routes/stt.py:143`, `:152`
- Modify: `apps/api_gateway/app/services/conversation/session.py:56` (import), `:331`
- Test: `tests/unit/profiles/test_shared_profile_not_runnable.py` (create)

**Interfaces:**
- Consumes: `usable_profile_or_none`, `is_shared_template` (Task 1); the `shared` field on create (Task 2).
- Produces: nothing new — behavior change only.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/profiles/test_shared_profile_not_runnable.py`. Copy the `client` / `_with_password` / `_as_user` / `_rand` helpers verbatim from `tests/unit/profiles/test_profile_shared_crud.py` (Task 2), then:

```python
"""A shared template is readable and clonable, but nothing may RUN on it.

Every test here also asserts the OTHER half: a private profile belonging to
someone else must still produce the old, indistinguishable "not found"
response. That is the regression guard on the C2 no-enumeration-oracle
contract (see services/profile_visibility.py) -- naming a shared profile in an
error is safe precisely because GET /v1/profiles already lists it to everyone,
and that reasoning must not leak onto private rows.
"""

from app.services.profiles.models import LlmConfig, Profile
from app.services.profiles.store import profile_store

SHARED_MSG = "shared template"


def _make_shared_template(name: str) -> None:
    """Written straight to the store, like test_profile_idor.py does. Going
    through the admin HTTP route would couple these tests to Task 2's payload
    handling for no gain -- what is under test here is the CONSUMERS."""
    profile_store.upsert(Profile(
        name=name,
        owner_id="some-admin",
        shared=True,
        system_prompt="TEMPLATE SYSTEM PROMPT -- MUST NOT BE USED",
        llm=LlmConfig(
            base_url="https://template-llm.example/v1",
            api_key="template-secret-api-key",
            model="template-model",
        ),
    ))


def test_http_chat_never_runs_on_a_shared_template(client, _with_password, monkeypatch):
    """Spy on build_responder_ex to observe exactly what the route resolved --
    the same technique as test_profile_idor.py's
    test_chat_private_profile_never_reaches_responder_construction. The spy
    REPLACES build_responder_ex and never calls through, so the template's
    base_url can never produce a real network call.

    Note the request shape: `?profile=` is a QUERY param and the body is
    `{"messages": [...]}` -- see the existing /chat tests.
    """
    import app.api.routes.conversation as conversation_route

    tpl = _rand("tpl")
    _make_shared_template(tpl)
    _as_user(client, "user")

    captured: list[dict] = []

    class _StubResponder:
        async def reply(self, history):
            return "ok"

        async def aclose(self):
            return None

    async def _spy(**kwargs):
        captured.append(kwargs)
        return _StubResponder()

    monkeypatch.setattr(conversation_route, "build_responder_ex", _spy)

    resp = client.post(
        f"/v1/conversation/chat?profile={tpl}",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    assert captured, "the route never built a responder"
    assert captured[0]["api_key"] != "template-secret-api-key"
    assert captured[0]["system_prompt"] != "TEMPLATE SYSTEM PROMPT -- MUST NOT BE USED"
    assert "template-secret-api-key" not in resp.text


def test_ws_conversation_warns_and_uses_defaults_on_a_shared_profile(client, _with_password):
    _as_user(client, "user")
    tpl = _rand("tpl")
    _make_shared_template(tpl)
    with client.websocket_connect(f"/v1/conversation/stream?profile={tpl}") as ws:
        warnings = []
        for _ in range(4):
            msg = ws.receive_json()
            if msg.get("event") == "warning":
                warnings.append(msg["message"])
            if msg.get("event") == "session_started":
                break
    assert any(SHARED_MSG in w for w in warnings), warnings
    assert any(tpl in w for w in warnings), "a shared name is public; say which one"


def test_ws_conversation_keeps_the_old_message_for_someone_elses_private_profile(
    client, _with_password
):
    """The no-oracle half: "exists but is Alice's" must stay byte-identical to
    "never existed"."""
    alice = TestClient(app)
    _as_user(alice, "user")
    private = _rand("priv")
    assert alice.post("/v1/profiles", json={"name": private}).status_code == 200

    _as_user(client, "user")
    ghost = _rand("ghost")

    def _first_warning(name: str) -> str:
        with client.websocket_connect(f"/v1/conversation/stream?profile={name}") as ws:
            for _ in range(4):
                msg = ws.receive_json()
                if msg.get("event") == "warning":
                    return msg["message"]
        return ""

    assert _first_warning(private).replace(private, "X") == _first_warning(ghost).replace(ghost, "X")
    assert SHARED_MSG not in _first_warning(private)


def test_stt_warm_ignores_a_shared_profile(client, _with_password):
    """Falls through to the server default engine, exactly as an unknown name
    does -- the template's pinned engine/model must not be applied.

    `POST /v1/stt/warm?profile=` returns
    {"data": {"engine": ..., "model": ..., "warmed": ...}} (stt.py's
    warm_engine). Asserted against the server default read from the config
    store rather than a hardcoded engine name, so a change to the hermetic
    test config cannot turn this green by accident.
    """
    from app.services.system_config import system_config_store

    tpl = _rand("tpl")
    profile_store.upsert(Profile(
        name=tpl, owner_id="some-admin", shared=True,
        stt=SttConfig(engine="vosk", language="vi"),
    ))
    _as_user(client, "user")

    resp = client.post(f"/v1/stt/warm?profile={tpl}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["engine"] != "vosk", "the template's engine leaked into the warm"
    assert resp.json()["data"]["engine"] == system_config_store.get().engines.default_stt_engine
```

Add `SttConfig` to the `app.services.profiles.models` import at the top of the
file.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/profiles/test_shared_profile_not_runnable.py -x -q`
Expected: FAIL — no shared-specific warning; the WS test's `warnings` list has no matching entry.

- [ ] **Step 3: Switch conversation.py**

Change the import on line 46 to bring in the new names:

```python
from app.services.profile_visibility import (
    is_shared_template,
    usable_profile_or_none,
    visible_tts_profile_or_none,
)
```

At line ~172 (HTTP `/chat`), replace the `active_profile = ...` line:

```python
    # C2 fix: usable_profile_or_none() collapses "doesn't exist" and "exists
    # but belongs to someone else" to the same None -- caller must never run
    # on another user's llm.api_key/system_prompt/mcp_servers (see
    # docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md). It also
    # excludes shared templates, which nobody runs on (2026-08-14 design).
    active_profile = usable_profile_or_none(profile_store.get(profile) if profile else None, caller_id)
```

At line ~354 (WS connect), replace the resolution and its warning:

```python
    requested_row = profile_store.get(profile_name) if profile_name else None
    profile = usable_profile_or_none(
        requested_row,
        identity.user_id,
        bypass=identity.unauthenticated,
    )
    if profile_name and not profile:
        await websocket.send_json({
            "event": "warning",
            "message": (
                f"profile '{profile_name}' is a shared template; clone it before using it"
                if is_shared_template(requested_row)
                else f"profile '{profile_name}' not found, using defaults"
            ),
        })
```

- [ ] **Step 4: Switch lugo.py**

Line 33 import: swap `visible_profile_or_none` for `usable_profile_or_none`
(keep `visible_tts_profile_or_none`). In `_resolve`, line ~105:

```python
    profile = usable_profile_or_none(
        profile_store.get(profile_name) if profile_name else None, caller_id, bypass=bypass
    )
```

Add to `_resolve`'s docstring, after the existing C2 paragraph:

```
    Shared templates resolve to None here too: they are clone-only, so a
    device that names one runs on server defaults rather than on the template
    (2026-08-14 design).
```

- [ ] **Step 5: Switch stt.py**

At line 143 change the local import to `usable_profile_or_none`, and at line ~152:

```python
        # C2 fix: a profile name the caller can't see -- or a shared template,
        # which nobody runs on -- must resolve exactly like an unknown one
        # (fall through to the server default engine), not leak or apply the
        # engine/model it pins.
        prof = usable_profile_or_none(
            profile_store.get(profile) if profile else None, current_user_id(request)
        )
```

- [ ] **Step 6: Switch session.py**

Line 56 import → `usable_profile_or_none`. At line ~331:

```python
        profile = usable_profile_or_none(
            profile_store.get(cfg.profile_name) if cfg.profile_name else None,
            cfg.identity_user_id,
            bypass=cfg.identity_unauthenticated,
        )
```

Append to the long comment above it:

```
        # Shared templates are excluded here as well -- this is the choke
        # point, so a route that somehow let one through still cannot run on it.
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/unit/profiles/test_shared_profile_not_runnable.py -q`
Expected: PASS.

- [ ] **Step 8: Run the consumer regression suites**

Run: `python -m pytest tests/unit/profiles tests/unit/conversation tests/unit/stt -q`
Expected: PASS. Any failure asserting that an ownerless/template profile is runnable is describing the removed behavior — update it to use an owned profile, since running on a template is exactly what this task forbids.

- [ ] **Step 9: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py \
        apps/api_gateway/app/api/routes/lugo.py \
        apps/api_gateway/app/api/routes/stt.py \
        apps/api_gateway/app/services/conversation/session.py \
        tests/unit/
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(profiles): shared templates are not runnable at any consumer"
```

---

### Task 4: Devices cannot be bound to a shared profile

**Files:**
- Modify: `apps/api_gateway/app/api/routes/devices.py:15` (import), `:28-43`
- Test: `tests/unit/devices/test_device_shared_profile_bind.py` (create — check the real directory name first with `ls tests/unit | grep -i device`)

**Interfaces:**
- Consumes: `usable_profile_or_none`, `is_shared_template` (Task 1).
- Produces: `POST /v1/devices/pair/claim` and `POST /v1/devices/mine/{id}/profile` return **400** for a shared `profile_id`.

- [ ] **Step 1: Write the failing test**

```python
"""Binding is the one shared-profile rejection that is NOT a silent fallback.

The WS/chat paths fall back to server defaults with a warning because a bad
`?profile=` should not brick a speaker. Binding is a deliberate admin action
with a form behind it, so it fails loudly instead -- and, since a shared
profile is listed to every caller by GET /v1/profiles, it may be named.
"""

# helpers: copy client / _with_password / _as_user / _rand verbatim from
# tests/unit/profiles/test_profile_shared_crud.py


def test_reassign_to_a_shared_profile_is_rejected(client, _with_password):
    user_id = _as_user(client, "admin")
    tpl = _rand("tpl")
    assert client.post("/v1/profiles", json={"name": tpl, "shared": True}).status_code == 200
    asyncio.run(device_store.create(user_id, "speaker", _rand("serial"), profile_id=""))
    device_id = asyncio.run(device_store.list_for_user(user_id))[0]["id"]

    resp = client.post(f"/v1/devices/mine/{device_id}/profile", json={"profile_id": tpl})
    assert resp.status_code == 400, resp.text
    assert "shared template" in resp.json()["detail"]
    assert tpl in resp.json()["detail"]
    assert asyncio.run(device_store.get_by_id(device_id)).profile_id == ""


def test_reassign_to_someone_elses_private_profile_still_404s(client, _with_password):
    """Unchanged: the private-profile path must stay a 404 with no name-shaped
    information beyond the one the caller already typed."""
    alice = TestClient(app)
    _as_user(alice, "user")
    private = _rand("priv")
    assert alice.post("/v1/profiles", json={"name": private}).status_code == 200

    user_id = _as_user(client, "user")
    asyncio.run(device_store.create(user_id, "speaker", _rand("serial"), profile_id=""))
    device_id = asyncio.run(device_store.list_for_user(user_id))[0]["id"]

    resp = client.post(f"/v1/devices/mine/{device_id}/profile", json={"profile_id": private})
    assert resp.status_code == 404
    assert "shared" not in resp.json()["detail"]


def test_unassigning_still_works(client, _with_password):
    user_id = _as_user(client, "user")
    name = _rand("own")
    assert client.post("/v1/profiles", json={"name": name}).status_code == 200
    asyncio.run(device_store.create(user_id, "speaker", _rand("serial"), profile_id=name))
    device_id = asyncio.run(device_store.list_for_user(user_id))[0]["id"]

    assert client.post(
        f"/v1/devices/mine/{device_id}/profile", json={"profile_id": ""}
    ).status_code == 200
    assert asyncio.run(device_store.get_by_id(device_id)).profile_id == ""
```

Check `device_store.create`'s signature (`sed -n 44,66p apps/api_gateway/app/services/auth/devices.py`) and the exact reassign route path (`grep -n '@router.post' apps/api_gateway/app/api/routes/devices.py`) before running; adjust the calls to match.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/devices/test_device_shared_profile_bind.py -x -q`
Expected: FAIL — the bind succeeds with 200.

- [ ] **Step 3: Implement the guard**

In `apps/api_gateway/app/api/routes/devices.py`, change the import on line 15 to
`from app.services.profile_visibility import is_shared_template, usable_profile_or_none`
and rewrite `_checked_profile_name`:

```python
def _checked_profile_name(profile_id: str, user_id: str) -> str:
    """Return `profile_id` if this user may bind a device to it, else raise.

    This is THE choke point for the bind path. A device identity resolves to its
    owner's user_id (core/auth_guard.resolve_ws_identity), so a binding the owner
    couldn't otherwise see would hand them that profile's llm.api_key,
    system_prompt and private mcp_servers on the next connect -- the C2 IDOR that
    services/profile_visibility.py exists to close. "Belongs to someone else"
    collapses into the same 404 as "doesn't exist", per that module's contract:
    the pair of them must stay indistinguishable so this doesn't become a
    profile-name enumeration oracle.

    A SHARED template is the one case that gets a real message instead. It is
    clone-only (2026-08-14 design) and GET /v1/profiles already lists it to
    every caller, so naming it leaks nothing -- and a silent 404 on a name the
    admin can see in their own picker would just look broken.
    """
    if not profile_id:
        return ""  # unassigned is always allowed
    row = profile_store.get(profile_id)
    if is_shared_template(row):
        raise HTTPException(
            status_code=400,
            detail=(
                f"profile '{profile_id}' is a shared template; "
                f"clone it before using it"
            ),
        )
    if usable_profile_or_none(row, user_id) is None:
        raise HTTPException(status_code=404, detail=f"profile '{profile_id}' not found")
    return profile_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/devices/test_device_shared_profile_bind.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the device suites**

Run: `python -m pytest tests/unit/devices -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/devices.py tests/unit/devices/
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(devices): refuse to bind a device to a shared profile"
```

---

### Task 5: Boot migration for existing ownerless templates

**Files:**
- Create: `apps/api_gateway/app/services/profiles/shared_migration.py`
- Modify: `apps/api_gateway/app/main.py:144-165`
- Test: `tests/unit/profiles/test_shared_profile_migration.py` (create)

**Interfaces:**
- Consumes: `Profile.shared` (Task 1).
- Produces: `async def migrate_ownerless_profiles() -> None` in `app.services.profiles.shared_migration`.

- [ ] **Step 1: Write the failing test**

```python
"""One-time conversion of the old owner_id-is-None templates.

The rule exists to keep deployed fleets running: a template that a device is
already bound to becomes that device owner's private profile, so the speaker
keeps working across the upgrade. Only templates nobody is running become
shared.

Invariant afterwards, and the reason this is idempotent: `owner_id is None`
implies `shared is True`.
"""

import asyncio
import uuid

import pytest

from app.services.auth.devices import device_store
from app.services.profiles.models import Profile
from app.services.profiles.shared_migration import migrate_ownerless_profiles
from app.services.profiles.store import profile_store


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def test_unbound_template_becomes_shared():
    name = _rand("free")
    profile_store.upsert(Profile(name=name, owner_id=None))
    asyncio.run(migrate_ownerless_profiles())
    row = profile_store.get(name)
    assert row.shared is True
    assert row.owner_id is None


def test_template_with_one_device_owner_goes_to_that_owner():
    name = _rand("bound")
    profile_store.upsert(Profile(name=name, owner_id=None))
    asyncio.run(device_store.create("alice", "speaker", _rand("serial"), profile_id=name))

    asyncio.run(migrate_ownerless_profiles())

    row = profile_store.get(name)
    assert row.owner_id == "alice", "the fleet must keep running across the upgrade"
    assert row.shared is False


def test_template_with_two_device_owners_becomes_shared_and_warns(caplog):
    name = _rand("multi")
    profile_store.upsert(Profile(name=name, owner_id=None))
    asyncio.run(device_store.create("alice", "a", _rand("serial"), profile_id=name))
    asyncio.run(device_store.create("bob", "b", _rand("serial"), profile_id=name))

    with caplog.at_level("WARNING"):
        asyncio.run(migrate_ownerless_profiles())

    row = profile_store.get(name)
    assert row.shared is True
    assert row.owner_id is None
    assert name in caplog.text, "an admin has to be told which bindings to fix"


def test_owned_rows_are_left_alone():
    name = _rand("owned")
    profile_store.upsert(Profile(name=name, owner_id="carol"))
    asyncio.run(migrate_ownerless_profiles())
    row = profile_store.get(name)
    assert row.owner_id == "carol"
    assert row.shared is False


def test_is_idempotent():
    free = _rand("free")
    bound = _rand("bound")
    profile_store.upsert(Profile(name=free, owner_id=None))
    profile_store.upsert(Profile(name=bound, owner_id=None))
    asyncio.run(device_store.create("dave", "speaker", _rand("serial"), profile_id=bound))

    asyncio.run(migrate_ownerless_profiles())
    first = (profile_store.get(free).model_dump(), profile_store.get(bound).model_dump())
    asyncio.run(migrate_ownerless_profiles())
    second = (profile_store.get(free).model_dump(), profile_store.get(bound).model_dump())

    assert first == second


def test_a_revoked_devices_owner_does_not_claim_the_template():
    """A revoked device is not a running fleet member; handing it the profile
    would give a stranger someone else's llm.api_key."""
    name = _rand("revoked")
    profile_store.upsert(Profile(name=name, owner_id=None))
    asyncio.run(device_store.create("erin", "speaker", _rand("serial"), profile_id=name))
    device_id = asyncio.run(device_store.list_for_user("erin"))[0]["id"]
    asyncio.run(device_store.revoke(device_id))

    asyncio.run(migrate_ownerless_profiles())

    row = profile_store.get(name)
    assert row.shared is True
    assert row.owner_id is None
```

Confirm `device_store.create`'s signature and whether `revoke` takes an owner
argument before running (`sed -n 44,102p apps/api_gateway/app/services/auth/devices.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/profiles/test_shared_profile_migration.py -x -q`
Expected: FAIL — `ModuleNotFoundError: app.services.profiles.shared_migration`.

- [ ] **Step 3: Write the migration**

Create `apps/api_gateway/app/services/profiles/shared_migration.py`:

```python
"""One-time conversion of legacy ownerless profiles to the shared flag.

`owner_id is None` used to mean two things at once: "an admin made it" and
"it is a template everyone may use". Profile.shared now carries the second
meaning on its own (see
docs/superpowers/specs/2026-08-14-shared-profile-clone-only-design.md), and
shared rows are clone-only -- so a straight "every ownerless row becomes
shared" would strand every device already bound to one.

Hence the device check: a template exactly one live device owner is running
becomes that owner's private profile, and the fleet survives the upgrade
untouched. Only templates nobody runs become shared.

Idempotent, so it is safe on every boot: afterwards `owner_id is None` implies
`shared is True`, and this only rewrites rows where both are false.
"""

from __future__ import annotations

import logging

from app.services.auth.devices import device_store
from app.services.profiles.store import profile_store

logger = logging.getLogger(__name__)


async def migrate_ownerless_profiles() -> None:
    legacy = [
        p for p in profile_store.list().values() if p.owner_id is None and not p.shared
    ]
    if not legacy:
        return

    # One pass over devices, not one query per profile: this runs on every boot
    # and the device table is small but the profile table is smaller still.
    # Revoked devices are excluded deliberately -- a revoked device is not a
    # running fleet member, and letting its owner claim the profile would hand
    # a stranger the row's llm.api_key.
    devices_by_profile: dict[str, list[str]] = {}
    owners_by_profile: dict[str, set[str]] = {}
    for d in await device_store.list_all():
        if d.get("revoked"):
            continue
        name = d.get("profile_id") or ""
        if name:
            devices_by_profile.setdefault(name, []).append(d["id"])
            owners_by_profile.setdefault(name, set()).add(d["user_id"])

    for profile in legacy:
        owners = owners_by_profile.get(profile.name, set())
        if len(owners) == 1:
            profile.owner_id = next(iter(owners))
            profile.shared = False
            logger.info(
                "profile '%s': ownerless template adopted by its device owner", profile.name
            )
        else:
            profile.shared = True
            if owners:
                logger.warning(
                    "profile '%s' is now a clone-only shared template but %d device(s) "
                    "across %d different owners are still bound to it (%s); those devices "
                    "will fall back to server defaults until an admin reassigns them",
                    profile.name,
                    len(devices_by_profile.get(profile.name, [])),
                    len(owners),
                    ", ".join(devices_by_profile.get(profile.name, [])),
                )
        profile_store.upsert(profile)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/profiles/test_shared_profile_migration.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Wire it into the lifespan**

In `apps/api_gateway/app/main.py`, add to the import block around line 144:

```python
    from app.services.profiles.shared_migration import migrate_ownerless_profiles
```

and call it after `migrate_backfill_usage_model_ids()`:

```python
    # Independent of the registry migrations -- it only touches profile rows and
    # reads the device table. Must run before anything serves a turn: until it
    # does, legacy ownerless templates are visible-but-unusable to everyone, so
    # a request handled ahead of it would fall back to server defaults.
    await migrate_ownerless_profiles()
```

- [ ] **Step 6: Verify the wiring with a boot test**

Run: `python -m pytest tests/unit/profiles/test_shared_profile_migration.py -q && python -c "import ast,sys; src=open('apps/api_gateway/app/main.py').read(); sys.exit(0 if 'migrate_ownerless_profiles()' in src else 1)"`
Expected: PASS, exit 0.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/profiles/shared_migration.py \
        apps/api_gateway/app/main.py \
        tests/unit/profiles/test_shared_profile_migration.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(profiles): migrate legacy ownerless templates onto the shared flag"
```

---

### Task 6: Admin console — templates picker, filtered run/bind selectors

**Files:**
- Modify: `apps/api_gateway/app/static/index.html:218-230` (profile bar), `:318-322` (panel footer area)
- Modify: `apps/api_gateway/app/static/js/profiles.js:102-134`, `:221-280`, `:305-378`, `:515-530`
- Modify: `apps/api_gateway/app/static/js/devices.js:24-40`, `:66-80`
- Test: `tests/unit/profiles/test_static_shared_profile.py` (create)

**Interfaces:**
- Consumes: the `shared` field now present on every `GET /v1/profiles` row (Task 2).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/profiles/test_static_shared_profile.py`, following the
source-assertion pattern of `tests/unit/profiles/test_static_profile_health.py`:

```python
"""The console's shared-profile wiring.

`#profile-select` does double duty -- it is both "what does my conversation run
on" and the only route to Edit/Clone (profiles.js's profile-edit-btn handler).
Filtering shared rows out of it therefore has to come WITH a separate templates
picker, or shared profiles become unreachable and the one thing users are
supposed to do with them (clone) becomes impossible.
"""

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[3] / "apps" / "api_gateway" / "app" / "static"


@pytest.fixture(scope="module")
def profiles_js() -> str:
    return (STATIC / "js" / "profiles.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def devices_js() -> str:
    return (STATIC / "js" / "devices.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def test_templates_picker_exists(index_html: str) -> None:
    assert 'id="profile-template-select"' in index_html
    assert 'id="profile-template-clone-btn"' in index_html


def test_templates_picker_is_wired(profiles_js: str) -> None:
    assert "renderProfileTemplateSelect" in profiles_js
    assert 'el("profile-template-clone-btn")' in profiles_js


def test_run_selector_excludes_shared(profiles_js: str) -> None:
    body = profiles_js[profiles_js.index("export function renderProfileSelect"):]
    body = body[: body.index("export function renderLivehostProfileSelect")]
    assert ".shared" in body, "renderProfileSelect never looks at the shared flag"


def test_livehost_selector_excludes_shared(profiles_js: str) -> None:
    body = profiles_js[profiles_js.index("export function renderLivehostProfileSelect"):]
    body = body[: body.index("export function renderProfileTtsSelect")]
    assert ".shared" in body


def test_edit_panel_keys_readonly_off_shared_not_owner_id(profiles_js: str) -> None:
    body = profiles_js[profiles_js.index("export async function openProfilePanel"):]
    body = body[: body.index("export function closeProfilePanel")]
    assert "p.shared" in body
    assert "p.owner_id === null" not in body, "still keying read-only off the old rule"


def test_shared_checkbox_is_admin_only(profiles_js: str, index_html: str) -> None:
    assert 'id="pf-shared"' in index_html
    body = profiles_js[profiles_js.index("export async function openProfilePanel"):]
    body = body[: body.index("export function closeProfilePanel")]
    assert "isAdmin" in body and "pf-shared" in body


def test_device_binding_pickers_exclude_shared(devices_js: str) -> None:
    pair = devices_js[devices_js.index("export function renderDevicePairProfileSelect"):]
    pair = pair[: pair.index("export function renderAllDeviceFilterProfileOptions")]
    assert ".shared" in pair

    per_device = devices_js[devices_js.index("function myDeviceProfileColumn"):]
    per_device = per_device[:600]
    assert ".shared" in per_device


def test_all_devices_filter_still_lists_every_name(devices_js: str) -> None:
    """It filters a read-only table rather than writing a binding, so hiding
    shared names there would only make a legacy binding invisible."""
    body = devices_js[devices_js.index("export function renderAllDeviceFilterProfileOptions"):]
    body = body[: body.index("export async function loadMyDevices")]
    assert ".shared" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/profiles/test_static_shared_profile.py -x -q`
Expected: FAIL on `test_templates_picker_exists`.

- [ ] **Step 3: Add the markup**

In `apps/api_gateway/app/static/index.html`, immediately after the
`#profile-new-btn` button (line ~229), inside the same profile-bar row:

```html
                <select id="profile-template-select" title="Shared templates — clone one to use it">
                  <option value="">Shared templates&#8230;</option>
                </select>
                <button id="profile-template-clone-btn" class="ghost mini">Clone template</button>
```

And in the profile panel, next to the `#pf-voice-optimized` checkbox, add:

```html
                  <label class="check hidden" id="pf-shared-label">
                    <input type="checkbox" id="pf-shared" /> Shared template (clone-only, admin)
                  </label>
```

- [ ] **Step 4: Filter the run selectors and add the templates picker**

In `apps/api_gateway/app/static/js/profiles.js`, inside `renderProfileSelect`
replace the `Object.keys(profileData).sort().forEach(...)` block with:

```javascript
  // Shared templates are clone-only -- this dropdown picks what a conversation
  // RUNS on, and the server refuses to run one. They live in
  // #profile-template-select instead.
  Object.keys(profileData).sort().filter((n) => !profileData[n]?.shared).forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
```

Apply the identical replacement inside `renderLivehostProfileSelect`.

Then add, directly after `renderLivehostProfileSelect`:

```javascript
export function renderProfileTemplateSelect() {
  const sel = el("profile-template-select");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">Shared templates&#8230;</option>';
  Object.keys(profileData).sort().filter((n) => profileData[n]?.shared).forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (profileData[prev]) sel.value = prev;
}
```

Call it from `loadProfiles()` alongside the other render calls.

- [ ] **Step 5: Wire the Clone-template button**

`cloneProfile()` currently reads the name from the edit panel. Give it an
optional argument so the templates picker can reuse it unchanged otherwise —
change its signature to `export async function cloneProfile(sourceName)` and
its first line to resolve `const name = sourceName || profileEditMode;`
(check the current first lines with `sed -n 407,415p` and preserve whatever it
does today when no argument is passed).

Then near the other profile-bar listeners (line ~515):

```javascript
if (el("profile-template-clone-btn")) {
  el("profile-template-clone-btn").addEventListener("click", () => {
    const name = el("profile-template-select").value;
    if (!name) { alert("Pick a shared template to clone."); return; }
    cloneProfile(name);
  });
}
```

- [ ] **Step 6: Switch the edit panel to `shared` and add the checkbox**

In `openProfilePanel`, replace the `isTemplate` line and add the checkbox handling:

```javascript
    // Shared templates are read-only for non-admins: the server 404s
    // Save/Delete on them anyway, so hide those controls and offer Clone only.
    const isTemplate = !!p.shared;
    const status = await fetchAuthStatus();
    const isAdmin = !!(status && status.authenticated && status.role === "admin");
    const hideWriteControls = isTemplate && !isAdmin;
    el("pf-save-btn").classList.toggle("hidden", hideWriteControls);
    el("pf-delete-btn").classList.toggle("hidden", hideWriteControls);
    if (el("pf-clone-btn")) el("pf-clone-btn").classList.remove("hidden");
    // Publishing a template is an admin act; the server silently drops the
    // field for anyone else, so don't offer a control that does nothing.
    if (el("pf-shared-label")) el("pf-shared-label").classList.toggle("hidden", !isAdmin);
    if (el("pf-shared")) el("pf-shared").checked = !!p.shared;
```

In the `mode === "new"` branch of the same function, reset the checkbox
(`if (el("pf-shared")) el("pf-shared").checked = false;`) and apply the same
admin-only visibility toggle.

- [ ] **Step 7: Send `shared` on save and surface the 409**

In `saveProfile()`, add `shared: el("pf-shared")?.checked ?? false,` to the
request body object, and confirm the existing error path prints
`body.detail` (the bound-devices 409 message from Task 2 must reach the user).
If it prints only a generic string, change it to prefer `body.detail`.

- [ ] **Step 8: Filter the device binding pickers**

In `apps/api_gateway/app/static/js/devices.js`, in
`renderDevicePairProfileSelect`, replace the forEach block:

```javascript
  // Shared templates are clone-only, so the bind endpoint 400s on them --
  // offering the name would only produce an error.
  Object.keys(profileData).sort().filter((n) => !profileData[n]?.shared).forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
```

And in `myDeviceProfileColumn`'s `render`:

```javascript
          Object.keys(profileData).sort()
            .filter((name) => !profileData[name]?.shared)
            .map((name) => {
              const label = escapeHtml(name);
              const selected = d.profile_id === name ? " selected" : "";
              return `<option value="${escapeHtml(name)}"${selected}>${label}</option>`;
            })
```

Leave `renderAllDeviceFilterProfileOptions` untouched.

- [ ] **Step 9: Run test to verify it passes**

Run: `python -m pytest tests/unit/profiles/test_static_shared_profile.py -q`
Expected: PASS (8 passed).

- [ ] **Step 10: Verify the edited JS with the Read tool, not the shell**

Open each edited file with the Read tool and confirm no smart quotes or mangled
characters were introduced. This session's shell has broken encoding and
`node --check` can false-pass smart-quote corruption — a visual read is the
only reliable check.

Also run: `python -m pytest tests/unit/profiles/test_static_profile_health.py -q`
Expected: PASS (the existing static tests must not regress).

- [ ] **Step 11: Commit**

```bash
git add apps/api_gateway/app/static/index.html \
        apps/api_gateway/app/static/js/profiles.js \
        apps/api_gateway/app/static/js/devices.js \
        tests/unit/profiles/test_static_shared_profile.py
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "feat(admin-ui): templates picker, and keep shared profiles out of run/bind selectors"
```

---

### Task 7: Docs and the full-suite gate

**Files:**
- Modify: `docs/api.md` (the `/v1/profiles` section)
- Modify: `docs/decisions.md`
- Test: the whole suite, once.

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Find the profile docs**

Run: `grep -n "v1/profiles" docs/api.md | head -20` and
`grep -n "^## " docs/decisions.md | tail -10`

- [ ] **Step 2: Document the field and the rules**

In `docs/api.md`, under the `/v1/profiles` section, add:

```markdown
`shared` (boolean, admin-only) marks a profile as a **clone-only template**.
A shared profile is listed to and readable by every caller and can be cloned
by anyone, but nothing runs on it: naming it in `?profile=` on the WS or chat
routes falls back to server defaults with a warning, `/stt/warm` ignores it,
and binding a device to it returns 400. Only an admin may set, clear, or
otherwise write a shared profile. Sharing a profile that still has devices
bound to it returns 409 — reassign them first.

Cloning a shared profile yields a normal, owned, non-shared profile.
```

In `docs/decisions.md`, append a section:

```markdown
## Shared profiles are clone-only

`owner_id is None` used to mean both "an admin made it" and "everyone may use
it". Those are now separate: `owner_id` records who made a profile (admins
included), and `Profile.shared` marks a clone-only template.

A shared profile is readable and clonable by everyone and runnable by no one —
including the admin who owns it. That asymmetry is deliberate: a template is a
starting point to copy, not a live configuration that unrelated users and
devices run against, each inheriting an `llm.api_key` and `mcp_servers` they
did not configure.

Legacy ownerless rows are converted on boot
(`services/profiles/shared_migration.py`). A template exactly one live device
owner was running becomes that owner's private profile so deployed fleets keep
working; only templates nobody ran became shared.

Spec: `docs/superpowers/specs/2026-08-14-shared-profile-clone-only-design.md`
```

- [ ] **Step 3: Lint**

Run: `make lint`
Expected: clean. Fix any ruff findings in the files this plan touched.

- [ ] **Step 4: Full suite, once**

Run: `python -m pytest tests -q`
Expected: PASS. Nothing else may be running pytest at the same time — this repo's concurrency guard deadlocks on overlapping runs.

- [ ] **Step 5: Sanity-check the real database's migration**

Run: `python -c "import sqlite3,json; c=sqlite3.connect('data/app.db'); print([(n, json.loads(d).get('owner_id'), json.loads(d).get('shared')) for n,d in c.execute('select name,data from config_profiles')])"`

This prints only name/owner/shared — never dump the whole `data` column, it
holds `llm.api_key` inline. Before this branch is deployed, back up
`data/app.db` (the `data/app.db.bak-*` files are the existing convention).

- [ ] **Step 6: Commit**

```bash
git add docs/api.md docs/decisions.md
git -c user.name=lugondev -c user.email=lugondev@gmail.com commit -m "docs: shared profiles are clone-only templates"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `Profile.shared` field | 1 |
| `visible` / `usable` / `writable` table | 1 (predicates), 2 (writable — already existed via `_can_write`, extended by the shared branch) |
| Invariant `owner_id is None → shared` | 1 (dev-mode case), 5 (migration establishes it) |
| `profile_usable` / `usable_profile_or_none` / `is_shared_template` | 1 |
| Error messages: shared may be named, private may not | 3 (WS/chat), 4 (bind), both with an explicit no-oracle regression test |
| `create_profile` / `update_profile` / `clone_profile` | 2 |
| 409 on sharing a profile with bound devices | 2 |
| `ProfileRequest.shared` | 2 |
| Six consumers switch to `usable` | 3 (five) + 4 (devices bind) |
| `visible_tts_profile_or_none` untouched | 3 (explicit in the edit steps) |
| Health keeps `visible` | Not changed by any task — `profiles.py`'s `_visible` and `services/health.py` are absent from every Files list, which is the implementation. Task 3 step 8 runs `tests/unit/profiles`, which includes `test_profile_health.py`, as the guard. |
| Migration, three branches + idempotency | 5 |
| Admin console | 6 |
| Deployment note (back up `data/app.db`) | 7 step 5 |

**Placeholder scan:** The two spots that read as deferred are deliberate and
bounded — Task 2 step 2 and Task 4/5's signature checks — because
`device_store.create`'s exact parameter list is the one thing this plan did not
read in full, and guessing it would produce a test that fails for the wrong
reason. Each says exactly what to run and what to adjust.

**Type consistency:** `usable_profile_or_none(profile, caller_id, *, bypass)`
and `is_shared_template(profile)` are defined in Task 1 and used with those
exact signatures in Tasks 3 and 4. `migrate_ownerless_profiles()` is defined in
Task 5 and imported under that name in `main.py` in the same task.
`renderProfileTemplateSelect` is defined and called in Task 6 only. The message
string `profile '<name>' is a shared template; clone it before using it` is
identical in Global Constraints, Task 3 (WS), and Task 4 (bind), and the tests
assert on the substring `shared template` so a trailing-punctuation drift does
not produce a false failure.
