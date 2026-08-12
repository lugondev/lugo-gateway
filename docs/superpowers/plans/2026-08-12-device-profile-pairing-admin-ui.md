# Device↔Profile Pairing in the Admin Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make pairing a device to a Profile a required step in the admin console, let already-paired devices be (re)assigned a Profile from the same screen, and make the backend refuse to run a paired device with no bound Profile instead of silently falling back.

**Architecture:** Backend: `services/auth/device_profile.resolve_bound_profile` gains a 4th return value, `hard_denied`, computed purely from `identity.via_device` + `Device.profile_id`; both WS routes that already call this function (`lugo.py`, `conversation.py`) check it and refuse the connection. Frontend: the existing admin static console's Devices section (`static/js/devices.js` + `index.html`) gets a required Profile `<select>` on the pairing form and a Profile column on the My Devices / All Devices tables, reusing the `profileData` already loaded by `profiles.js` and the already-existing `POST /v1/devices/pair/claim` (`profile_id` field) and `POST /v1/devices/mine/{id}/profile` endpoints — no new backend endpoints.

**Tech Stack:** Python 3.12 / FastAPI / pytest-asyncio (backend), vanilla ES modules, no bundler (admin static console).

## Global Constraints

- `hard_denied` is keyed **only** on `identity.via_device` — the legacy shared `device_auth_token` fleet fallback (`identity.via_fleet_token`) is never touched by this change.
- The WS close on `hard_denied` uses an **ordinary close (no 401/403/4401 code)** — firmware's revoke-vs-network-drop classifier only wipes a device's token on a 401/403 handshake rejection or `goodbye{reason=account_disabled}`; reusing that code here would make a device with a perfectly valid token destroy it and loop re-pairing forever without ever fixing the actual problem.
- Each route sends its own existing error-frame shape: `{"type": "error", ...}` for `lugo.py`, `{"event": "error", ...}` for `conversation.py`. Do not invent a third shape.
- The admin-only **All Devices** table gets a **read-only** Profile column. No new admin-scoped/cross-tenant endpoint is added; cross-user profile reassignment is out of scope (confirmed with user).
- No new backend endpoints for the frontend tasks — `POST /v1/devices/pair/claim` already accepts `profile_id`, `POST /v1/devices/mine/{id}/profile` already exists, and `profile_id` is already present in every device dict returned by `/v1/devices/mine` and `/v1/devices` (`services/auth/devices.py`).
- This is a deliberate breaking change: any already-paired device with no bound profile stops being able to connect once Task 2/3 ship, until an admin binds it via Task 5's UI.
- Spec: `docs/superpowers/specs/2026-08-12-device-profile-pairing-admin-ui-design.md`.

---

### Task 1: `resolve_bound_profile` gains `hard_denied`

**Files:**
- Modify: `apps/api_gateway/app/services/auth/device_profile.py`
- Modify: `tests/unit/auth/test_device_profile_binding.py`

**Interfaces:**
- Produces: `resolve_bound_profile(identity, requested: str | None) -> tuple[str | None, str | None, bool, bool]` — `(profile_name_to_use, warning_or_None, came_from_binding, hard_denied)`. `hard_denied` is `True` iff `identity.via_device` is `True` and the resolved binding is empty (device row missing, `device_id` missing, or `device.profile_id == ""`); `False` for every non-`via_device` identity and for any bound device.

- [ ] **Step 1: Update the existing tests to the new 4-tuple shape (still red — implementation hasn't changed yet)**

Rewrite `tests/unit/auth/test_device_profile_binding.py` in full:

```python
"""Precedence rules for `services/auth/device_profile.resolve_bound_profile`.

The whole point of the binding is that the control panel is the single source of
truth for what a speaker runs. These tests pin the four cases that make that
true without breaking fleets deployed before bindings existed -- and the
hard-deny case that DOES intentionally break them once a device is expected
to be assigned (see the 2026-08-12 device-profile-pairing-admin-ui design).
"""

from dataclasses import dataclass

import pytest

from app.services.auth.device_profile import resolve_bound_profile
from app.services.auth.devices import DeviceStore
from app.services.auth.users import user_store


@dataclass
class FakeIdentity:
    """Structural stand-in for core.auth_guard.WsIdentity."""

    user_id: str | None = None
    device_id: str | None = None
    via_device: bool = False
    unauthenticated: bool = False


@pytest.fixture
def store():
    return DeviceStore()


async def _device(store, *, profile_id: str = "") -> str:
    user = await user_store.create("toan", "pw")
    device, _ = await store.create(user["id"], "speaker", "AA:BB:CC", profile_id=profile_id)
    return device["id"]


@pytest.mark.asyncio
async def test_binding_overrides_what_the_device_asked_for(store):
    device_id = await _device(store, profile_id="kitchen")
    identity = FakeIdentity(device_id=device_id, via_device=True)

    name, warning, from_binding, hard_denied = await resolve_bound_profile(
        identity, "stale-yaml-profile"
    )

    assert name == "kitchen"
    assert from_binding is True
    assert hard_denied is False
    # Announced, not silent: a config file on the device that no longer has any
    # effect should be visible to whoever is looking at the device's logs.
    assert warning is not None
    assert "kitchen" in warning and "stale-yaml-profile" in warning


@pytest.mark.asyncio
async def test_binding_agreeing_with_the_request_warns_about_nothing(store):
    device_id = await _device(store, profile_id="kitchen")
    identity = FakeIdentity(device_id=device_id, via_device=True)

    result = await resolve_bound_profile(identity, "kitchen")

    assert result == ("kitchen", None, True, False)


@pytest.mark.asyncio
async def test_unbound_device_is_hard_denied(store):
    """A paired device with no assignment is hard-denied -- callers (lugo.py,
    conversation.py) must refuse the connection instead of letting it fall
    back to whatever the device itself asked for or to server defaults. The
    resolved name/warning/from_binding stay exactly as before (still whatever
    was requested) because SOME callers of this function might one day want
    them for logging even on the denied path -- only `hard_denied` is new."""
    device_id = await _device(store)
    identity = FakeIdentity(device_id=device_id, via_device=True)

    assert await resolve_bound_profile(identity, "kitchen") == ("kitchen", None, False, True)
    assert await resolve_bound_profile(identity, None) == (None, None, False, True)


@pytest.mark.asyncio
async def test_non_device_identities_are_never_hard_denied(store):
    """Browsers, the legacy shared fleet token and dev-mode all keep picking a
    profile per connection -- only a paired device has an assignment to obey,
    so only a paired device can ever be hard-denied for lacking one."""
    device_id = await _device(store, profile_id="kitchen")

    browser = FakeIdentity(user_id="u1")
    assert await resolve_bound_profile(browser, "study") == ("study", None, False, False)

    # via_device without a device_id can't be looked up; treat as unbound
    # (and therefore hard-denied) rather than guessing.
    headless = FakeIdentity(via_device=True)
    assert await resolve_bound_profile(headless, "study") == ("study", None, False, True)

    # The bound device exists and is ignored by both identities above.
    bound = FakeIdentity(device_id=device_id, via_device=True)
    result = await resolve_bound_profile(bound, "study")
    assert result[0] == "kitchen"
    assert result[3] is False


@pytest.mark.asyncio
async def test_deleted_device_row_is_hard_denied(store):
    """A device_id that no longer resolves to a row (deleted mid-connection,
    or a stale cache) has no binding to trust -- fail closed, same as any
    other unbound device."""
    identity = FakeIdentity(device_id="no-such-device", via_device=True)
    assert await resolve_bound_profile(identity, "study") == ("study", None, False, True)
```

- [ ] **Step 2: Run the tests to verify they fail on the tuple shape**

Run: `pytest tests/unit/auth/test_device_profile_binding.py -v`
Expected: FAIL — `ValueError: not enough values to unpack` / tuple-length assertion errors, since `resolve_bound_profile` still returns a 3-tuple.

- [ ] **Step 3: Implement `hard_denied` in `resolve_bound_profile`**

In `apps/api_gateway/app/services/auth/device_profile.py`, replace the function body and its docstring:

```python
async def resolve_bound_profile(
    identity, requested: str | None
) -> tuple[str | None, str | None, bool, bool]:
    """Return (profile_name_to_use, warning_or_None, came_from_binding, hard_denied).

    `identity` is a core.auth_guard.WsIdentity; taken structurally rather than by
    import to keep this leaf module out of the auth_guard import cycle.

    The third element exists because a name the SERVER chose deserves gentler
    failure handling than one the CLIENT sent: lugo.py closes the connection on
    an unresolvable client-declared profile, which would turn a stale binding
    into a bricked speaker. Callers use it to fall back to defaults instead.

    The fourth element, `hard_denied`, is True iff this identity is a paired
    device (`via_device`) with no profile bound. Unlike the gentle from-binding
    fallback above, this is NOT recoverable by falling back to defaults --
    callers must refuse the connection outright. A paired device is meant to
    be centrally assigned; one that never was isn't "usable" and letting it
    quietly run on whatever it happened to ask for (or on server defaults)
    hides exactly the state an admin needs to go fix. Never True for any
    identity that isn't `via_device` -- browsers, the legacy shared fleet
    token, and dev-mode have no assignment to lack in the first place.
    """
    if not getattr(identity, "via_device", False):
        return requested, None, False, False
    device_id = getattr(identity, "device_id", None)
    if not device_id:
        return requested, None, False, True
    device = await device_store.get_by_id(device_id)
    bound = (device.profile_id or "") if device is not None else ""
    if not bound:
        return requested, None, False, True
    if requested and requested != bound:
        return bound, (
            f"this device is assigned to profile '{bound}'; "
            f"ignoring the profile '{requested}' it asked for"
        ), True, False
    return bound, None, True, False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/auth/test_device_profile_binding.py -v`
Expected: PASS, all 5 tests.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/auth/device_profile.py tests/unit/auth/test_device_profile_binding.py
git commit -m "$(cat <<'EOF'
feat(auth): resolve_bound_profile signals hard-denial for unassigned devices

A paired device with no profile bound now gets an explicit hard_denied
signal instead of silently falling back to its own request or server
defaults -- callers wire this up in the next two commits.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `lugo.py` refuses unbound paired devices

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py:169-177`
- Modify: `tests/unit/conversation/test_lugo_authz.py` (3 call sites)
- Modify: `tests/unit/conversation/test_lugo_disabled_cutoff.py` (2 call sites)
- Create: `tests/unit/conversation/test_lugo_device_profile_gate.py`

**Interfaces:**
- Consumes: `resolve_bound_profile` from Task 1 (4-tuple).
- Produces: nothing new consumed by later tasks — this is a leaf route change.

- [ ] **Step 1: Write the new failing test file for the gate itself**

Create `tests/unit/conversation/test_lugo_device_profile_gate.py`:

```python
"""A paired device (services.auth.devices) with no profile_id bound must be
refused at wakeup rather than allowed to run on whatever it requested or on
server defaults -- see docs/superpowers/specs/2026-08-12-device-profile-pairing-admin-ui-design.md.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.models import Profile
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store


class _StubSTT(STTProvider):
    name = "stub-lugo-profile-gate-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    _real_get = system_config_store.get

    def _get_with_stub():
        cfg = _real_get()
        return cfg.model_copy(update={
            "engines": cfg.engines.model_copy(update={"default_stt_engine": "stub-lugo-profile-gate-stt"}),
        })

    monkeypatch.setattr(system_config_store, "get", _get_with_stub)
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    stt_service.providers["stub-lugo-profile-gate-stt"] = _StubSTT()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="bound-profile"))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-lugo-profile-gate-stt", None)
    monkeypatch.setattr(settings, "admin_password", "")


def test_unbound_paired_device_is_refused_at_wakeup():
    user = asyncio.run(user_store.create("gate-user-unbound", "pw"))
    _device, raw_token = asyncio.run(device_store.create(user["id"], "ESP32", "AA:BB:GATE1"))

    client = TestClient(app)
    with client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        ws.send_json({
            "type": "wakeup",
            "audio_params": {"format": "opus", "sample_rate": 16000},
        })
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "profile" in msg["message"]


def test_bound_paired_device_still_connects():
    user = asyncio.run(user_store.create("gate-user-bound", "pw"))
    _device, raw_token = asyncio.run(
        device_store.create(user["id"], "ESP32", "AA:BB:GATE2", profile_id="bound-profile")
    )

    client = TestClient(app)
    with client.websocket_connect(f"/v1/lugo/stream?device_token={raw_token}") as ws:
        ws.send_json({
            "type": "wakeup",
            "audio_params": {"format": "opus", "sample_rate": 16000},
        })
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
```

- [ ] **Step 2: Run it to verify `test_unbound_paired_device_is_refused_at_wakeup` fails**

Run: `pytest tests/unit/conversation/test_lugo_device_profile_gate.py -v`
Expected: `test_unbound_paired_device_is_refused_at_wakeup` FAILS (today's `lugo.py` lets the unbound device through to `welcome`); `test_bound_paired_device_still_connects` already PASSES (no gate yet, so no regression to prove there — that's fine, it's the control case).

- [ ] **Step 3: Wire `hard_denied` into `lugo.py`**

In `apps/api_gateway/app/api/routes/lugo.py`, replace:

```python
    profile_name, binding_warning, from_binding = await resolve_bound_profile(
        identity, profile_name
    )
    if binding_warning:
        await websocket.send_json({"type": "warning", "message": binding_warning})
```

with:

```python
    profile_name, binding_warning, from_binding, hard_denied = await resolve_bound_profile(
        identity, profile_name
    )
    if hard_denied:
        # Ordinary close, NOT 4401/403: the device's token is valid, it's
        # just never been assigned a profile -- firmware's revoke classifier
        # only wipes the token on an auth rejection, and reusing that code
        # here would make it destroy a good token and loop re-pairing
        # forever without ever fixing the actual (admin-side) problem.
        await websocket.send_json({
            "type": "error",
            "message": "this device is not assigned to a profile; assign one in the admin console",
        })
        await websocket.close()
        return
    if binding_warning:
        await websocket.send_json({"type": "warning", "message": binding_warning})
```

- [ ] **Step 4: Run the gate test file again — both should pass now**

Run: `pytest tests/unit/conversation/test_lugo_device_profile_gate.py -v`
Expected: PASS, both tests.

- [ ] **Step 5: Run the full lugo authz/cutoff test files to find what the gate just broke**

Run: `pytest tests/unit/conversation/test_lugo_authz.py tests/unit/conversation/test_lugo_disabled_cutoff.py -v`
Expected: FAIL — `test_lugo_device_paired_to_admin_cannot_resume_other_users_session`, `test_lugo_paired_device_can_still_resume_its_owners_session`, `test_a_reconnecting_device_is_told_the_conversation_it_actually_resumed` (in `test_lugo_authz.py`), and `test_disabled_owner_cuts_off_paired_device`, `test_idle_timeout_zero_never_fires_for_identity_owned_connection` (in `test_lugo_disabled_cutoff.py`) now hit the new gate instead of their intended assertions, because their devices are created unbound.

- [ ] **Step 6: Bind those devices to the profile their own tests already wake up with**

In `tests/unit/conversation/test_lugo_authz.py`, the `_local_hermetic` fixture already seeds a profile named `"dev"` and every `_wakeup()` call already requests `"profile": "dev"` (see its definition at line 122) — bind each device to that same profile so the gate never fires and every existing assertion is reached exactly as before:

```python
# line 202, inside test_lugo_device_paired_to_admin_cannot_resume_other_users_session
    _device, raw_token = asyncio.run(
        device_store.create(admin_id, "kitchen-esp32", "serial-001", profile_id="dev")
    )
```

```python
# line 220, inside test_lugo_paired_device_can_still_resume_its_owners_session
    _device, raw_token = asyncio.run(
        device_store.create(owner_id, "living-room-esp32", "serial-002", profile_id="dev")
    )
```

```python
# line 243, inside test_a_reconnecting_device_is_told_the_conversation_it_actually_resumed
    _device, raw_token = asyncio.run(
        device_store.create(owner_id, "speaker", "serial-resume", profile_id="dev")
    )
```

In `tests/unit/conversation/test_lugo_disabled_cutoff.py`, its `_hermetic` fixture seeds a profile named `"fast"` and both tests wake up with `"profile": "fast"` — same fix:

```python
# line 92, inside test_disabled_owner_cuts_off_paired_device
    device, raw_token = asyncio.run(
        device_store.create(user["id"], "ESP32", "AA:BB:CC", profile_id="fast")
    )
```

```python
# line 132, inside test_idle_timeout_zero_never_fires_for_identity_owned_connection
    device, raw_token = asyncio.run(
        device_store.create(user["id"], "ESP32", "AA:BB:DD", profile_id="fast")
    )
```

- [ ] **Step 7: Run both files again to confirm the fix**

Run: `pytest tests/unit/conversation/test_lugo_authz.py tests/unit/conversation/test_lugo_disabled_cutoff.py tests/unit/conversation/test_lugo_device_profile_gate.py -v`
Expected: PASS, all tests in all three files.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/api/routes/lugo.py \
  tests/unit/conversation/test_lugo_authz.py \
  tests/unit/conversation/test_lugo_disabled_cutoff.py \
  tests/unit/conversation/test_lugo_device_profile_gate.py
git commit -m "$(cat <<'EOF'
feat(lugo): refuse wakeup from a paired device with no bound profile

Devices without an assignment now get an explicit error frame and an
ordinary (non-revoking) close at wakeup instead of silently running on
whatever profile they asked for or on server defaults. Breaking on
purpose for any pre-existing unassigned device -- see the 2026-08-12
device-profile-pairing-admin-ui spec.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `conversation.py` refuses unbound paired devices

**Files:**
- Modify: `apps/api_gateway/app/api/routes/conversation.py:327-338`
- Modify: `tests/unit/conversation/test_conversation_authz.py:317-334`
- Create: `tests/unit/conversation/test_conversation_device_profile_gate.py`

**Interfaces:**
- Consumes: `resolve_bound_profile` from Task 1 (4-tuple).
- Produces: nothing new consumed by later tasks — this is a leaf route change, same shape as Task 2.

- [ ] **Step 1: Write the new failing test file for the gate itself**

Create `tests/unit/conversation/test_conversation_device_profile_gate.py`:

```python
"""A paired device (services.auth.devices) with no profile_id bound must be
refused on /v1/conversation/stream too, mirroring lugo.py's gate -- see
docs/superpowers/specs/2026-08-12-device-profile-pairing-admin-ui-design.md.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.models import Profile
from app.services.profiles.store import profile_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """Same rationale as test_conversation_authz.py's fixture of the same
    name: the autouse `_hermetic` fixture in conftest.py blanks the admin
    password, which makes settings.auth_enabled False and short-circuits
    resolve_ws_identity to an unscoped unauthenticated=True identity that
    can never be via_device -- these tests need a real device-token identity."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient) -> str:
    username = f"gate-conv-{__import__('uuid').uuid4().hex[:10]}"
    password = "s3cret-password"
    signup = client.post("/api/auth/signup", json={"username": username, "password": password})
    assert signup.status_code == 200, signup.text
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return asyncio.run(user_store.get_by_username(username)).id


def test_unbound_paired_device_is_refused(client, _with_password):
    user_id = _as_user(client)
    _device, raw_token = asyncio.run(
        device_store.create(user_id, "ESP32", "AA:BB:GATE3")
    )

    device_client = TestClient(app)
    with device_client.websocket_connect(
        f"/v1/conversation/stream?output=text&device_token={raw_token}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "error"
        assert "profile" in msg["message"]


def test_bound_paired_device_still_connects(client, _with_password):
    user_id = _as_user(client)
    profile_store.upsert(Profile(name="conv-bound-profile", owner_id=user_id))
    _device, raw_token = asyncio.run(
        device_store.create(
            user_id, "ESP32", "AA:BB:GATE4", profile_id="conv-bound-profile"
        )
    )

    device_client = TestClient(app)
    with device_client.websocket_connect(
        f"/v1/conversation/stream?output=text&device_token={raw_token}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["event"] == "session_started"
```

- [ ] **Step 2: Run it to verify `test_unbound_paired_device_is_refused` fails**

Run: `pytest tests/unit/conversation/test_conversation_device_profile_gate.py -v`
Expected: `test_unbound_paired_device_is_refused` FAILS (today's `conversation.py` lets it through to `session_started`); `test_bound_paired_device_still_connects` already PASSES.

- [ ] **Step 3: Wire `hard_denied` into `conversation.py`**

In `apps/api_gateway/app/api/routes/conversation.py`, replace:

```python
    profile_name, binding_warning, _from_binding = await resolve_bound_profile(
        identity, profile_name
    )
    if binding_warning:
        await websocket.send_json({"event": "warning", "message": binding_warning})
```

with:

```python
    profile_name, binding_warning, _from_binding, hard_denied = await resolve_bound_profile(
        identity, profile_name
    )
    if hard_denied:
        # Ordinary close, NOT 4401/403 -- see lugo.py's identical gate for why:
        # the token is valid, only the profile assignment is missing.
        await websocket.send_json({
            "event": "error",
            "message": "this device is not assigned to a profile; assign one in the admin console",
        })
        await websocket.close()
        return
    if binding_warning:
        await websocket.send_json({"event": "warning", "message": binding_warning})
```

- [ ] **Step 4: Run the gate test file again — both should pass now**

Run: `pytest tests/unit/conversation/test_conversation_device_profile_gate.py -v`
Expected: PASS, both tests.

- [ ] **Step 5: Run the existing conversation authz file to find what the gate just broke**

Run: `pytest tests/unit/conversation/test_conversation_authz.py -v`
Expected: FAIL — `test_ws_paired_device_can_still_resume_its_owners_session` now hits the gate instead of reaching `session_started`, because its device is created unbound and it never passes `?profile=`.
(`test_ws_device_paired_to_admin_never_gets_admin_bypass` stays green untouched: `conversation.py` checks session ownership *before* calling `resolve_bound_profile`, and that test's session-ownership denial fires first, so the new gate is never reached for it.)

- [ ] **Step 6: Bind that one device to a real, visible profile**

In `tests/unit/conversation/test_conversation_authz.py`, replace (around line 317-325):

```python
def test_ws_paired_device_can_still_resume_its_owners_session(client, _with_password):
    """The comparison is by user_id, so a device must still be able to
    resume ITS OWN owner's session -- the fix must not break that. Fresh
    client for the same cookie-vs-device-token reason as the test above."""
    owner_id = _as_user(client, "user")
    _device, raw_token = asyncio.run(device_store.create(owner_id, "esp32-owner-test", "serial-conv-002"))

    owner_sid = "owner-device-conv-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(owner_sid, user_id=owner_id))
```

with:

```python
def test_ws_paired_device_can_still_resume_its_owners_session(client, _with_password):
    """The comparison is by user_id, so a device must still be able to
    resume ITS OWN owner's session -- the fix must not break that. Fresh
    client for the same cookie-vs-device-token reason as the test above.

    Bound to a real profile because an unbound device is now hard-denied
    before it ever gets a chance to resume anything (see
    test_conversation_device_profile_gate.py) -- this test is about the
    resume ownership check, not the profile gate, so give it a profile."""
    owner_id = _as_user(client, "user")
    profile_store.upsert(Profile(name="owner-test-profile", owner_id=owner_id))
    _device, raw_token = asyncio.run(
        device_store.create(
            owner_id, "esp32-owner-test", "serial-conv-002", profile_id="owner-test-profile"
        )
    )

    owner_sid = "owner-device-conv-" + uuid.uuid4().hex[:8]
    asyncio.run(session_store.create(owner_sid, user_id=owner_id))
```

- [ ] **Step 7: Run the file again to confirm the fix**

Run: `pytest tests/unit/conversation/test_conversation_authz.py tests/unit/conversation/test_conversation_device_profile_gate.py -v`
Expected: PASS, all tests in both files.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/api/routes/conversation.py \
  tests/unit/conversation/test_conversation_authz.py \
  tests/unit/conversation/test_conversation_device_profile_gate.py
git commit -m "$(cat <<'EOF'
feat(conversation): refuse /stream from a paired device with no bound profile

Mirrors lugo.py's gate: an unassigned paired device gets an explicit
error frame and an ordinary (non-revoking) close instead of silently
falling back to server defaults.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Admin UI — require a Profile when pairing a device

**Files:**
- Modify: `apps/api_gateway/app/static/index.html:412-437`
- Modify: `apps/api_gateway/app/static/js/devices.js`

**Interfaces:**
- Consumes: `profileData` (exported `let` object, `{name: {owner_id, ...}}`) from `apps/api_gateway/app/static/js/profiles.js`, already populated by `loadProfiles()` at page load (`main.js`) before a user can reach the Devices section (`loadMyDevices()` only runs when the user navigates there, per `sidebar-nav.js`).
- Produces: nothing consumed by later tasks in this plan — Task 5/6 touch different parts of the same file and don't depend on this task's new function names.

There is no JS test harness for this static console (verified: no `*.test.js` anywhere under `apps/api_gateway/app/static/`, matching the precedent already noted for `devices.js`). Verification for this task and Tasks 5/6 is manual, via the dev server + a browser, as the plan's last step.

- [ ] **Step 1: Add the Profile field to the "Add Device" form**

In `apps/api_gateway/app/static/index.html`, replace:

```html
              <p class="hint">Pair an ESP32 or RPi client to your account. Enter the code shown on the device.</p>
              <div id="device-mine-list" class="model-list">
                <p class="hint">Loading&#8230;</p>
              </div>
              <h3 class="sub">Add Device</h3>
              <div class="row tight">
                <label>
                  Code shown on device
                  <input id="device-pair-code" type="text" placeholder="12345678" />
                </label>
                <label>
                  Name
                  <input id="device-pair-name" type="text" placeholder="ESP32 desk" />
                </label>
                <div class="actions end">
                  <button id="device-pair-btn">Pair</button>
                </div>
              </div>
```

with:

```html
              <p class="hint">Pair an ESP32 or RPi client to your account. Enter the code shown on the device and choose which profile it should run.</p>
              <div id="device-mine-list" class="model-list">
                <p class="hint">Loading&#8230;</p>
              </div>
              <h3 class="sub">Add Device</h3>
              <div class="row tight">
                <label>
                  Code shown on device
                  <input id="device-pair-code" type="text" placeholder="12345678" />
                </label>
                <label>
                  Name
                  <input id="device-pair-name" type="text" placeholder="ESP32 desk" />
                </label>
                <label>
                  Profile
                  <select id="device-pair-profile"></select>
                </label>
                <div class="actions end">
                  <button id="device-pair-btn">Pair</button>
                </div>
              </div>
```

- [ ] **Step 2: Populate and require the Profile select in `devices.js`**

In `apps/api_gateway/app/static/js/devices.js`, add the import and a populate function, then call it from `loadMyDevices()`:

```js
import { el, print, escapeHtml, runBulk, printBulkSummary } from "./helpers.js";
import { renderDataTable } from "./data-table.js";
import { fetchAuthStatus } from "./session.js";
import { confirmDialog } from "./modal.js";
import { profileData } from "./profiles.js";
```

```js
function renderDevicePairProfileSelect() {
  const sel = el("device-pair-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">Select a profile&#8230;</option>';
  Object.keys(profileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = profileData[name]?.owner_id ? `${name} (mine)` : name;
    sel.appendChild(opt);
  });
  if (profileData[prev]) sel.value = prev;
}

export async function loadMyDevices() {
  renderDevicePairProfileSelect();
  try {
    const body = await (await fetch("/v1/devices/mine")).json();
    myDeviceData = body.data || [];
    renderMyDeviceList();
  } catch {
    /* ignore */
  }
  await maybeLoadAllDevices();
}
```

(This replaces the existing top two lines of `loadMyDevices()` — the `try { ... }` block and everything after stays exactly as it is today.)

- [ ] **Step 3: Require and send the profile on claim**

In `apps/api_gateway/app/static/js/devices.js`, replace `claimDevice()`:

```js
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
```

with:

```js
export async function claimDevice() {
  const status = el("device-status");
  const name = el("device-pair-name").value.trim();
  const code = el("device-pair-code").value.trim();
  const profileId = el("device-pair-profile").value;
  if (!name || !code) {
    print(status, "Enter both the code shown on the device and a name for it", true);
    return;
  }
  if (!profileId) {
    print(status, "Choose a profile for this device to run", true);
    return;
  }
  try {
    const resp = await fetch("/v1/devices/pair/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name, profile_id: profileId }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(status, body.detail || "Pairing failed", true);
      return;
    }
    status.textContent = `Paired "${name}"`;
    el("device-pair-name").value = "";
    el("device-pair-code").value = "";
    el("device-pair-profile").value = "";
    await loadMyDevices();
  } catch (error) {
    print(status, String(error), true);
  }
}
```

- [ ] **Step 4: Manual verification (deferred to the end of Task 6)**

This task's UI isn't independently useful to click through until Task 5 shows the result (a device without a profile has nowhere to display that fact yet). Combine manual verification with Task 6's step 3.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/devices.js
git commit -m "$(cat <<'EOF'
feat(admin-ui): require a Profile when pairing a device

The "Add Device" form now has a required Profile select, populated
from the same profileData profiles.js already loads, and sends
profile_id on POST /v1/devices/pair/claim (a field the backend has
accepted since the endpoint was written).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Admin UI — reassign a Profile on an already-paired device (My Devices)

**Files:**
- Modify: `apps/api_gateway/app/static/js/devices.js`

**Interfaces:**
- Consumes: `profileData` from `profiles.js` (same as Task 4); `POST /v1/devices/mine/{id}/profile` (existing endpoint, body `{profile_id: string}`, `""` unassigns).

- [ ] **Step 1: Add a Profile column to `renderMyDeviceList`**

In `apps/api_gateway/app/static/js/devices.js`, add a helper and splice it into the columns array:

```js
function myDeviceProfileColumn() {
  return {
    key: "profile",
    label: "Profile",
    render: (d) => {
      const options = ['<option value="">Unassigned</option>']
        .concat(
          Object.keys(profileData).sort().map((name) => {
            const label = escapeHtml(profileData[name]?.owner_id ? `${name} (mine)` : name);
            const selected = d.profile_id === name ? " selected" : "";
            return `<option value="${escapeHtml(name)}"${selected}>${label}</option>`;
          })
        )
        .join("");
      return `<select data-device-profile-select="${escapeHtml(d.id)}">${options}</select>`;
    },
  };
}
```

Replace `renderMyDeviceList`'s `columns:` array:

```js
    columns: [
      ...deviceColumns(false),
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `<button class="mini danger" data-device-revoke-mine="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>`,
      },
    ],
```

with:

```js
    columns: [
      ...deviceColumns(false),
      myDeviceProfileColumn(),
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `<button class="mini danger" data-device-revoke-mine="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>`,
      },
    ],
```

- [ ] **Step 2: Wire the select's change event and the update call**

In `renderMyDeviceList`, after the existing `table.querySelectorAll("[data-device-revoke-mine]")...` wiring, add:

```js
  table.querySelectorAll("[data-device-profile-select]").forEach((sel) =>
    sel.addEventListener("change", () =>
      setMyDeviceProfile(sel.getAttribute("data-device-profile-select"), sel.value)
    )
  );
```

Add the new function near `revokeMyDevice`:

```js
async function setMyDeviceProfile(id, profileId) {
  try {
    const resp = await fetch(`/v1/devices/mine/${encodeURIComponent(id)}/profile`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: profileId }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      print(el("device-status"), body.detail || "Failed to update profile", true);
      return;
    }
    await loadMyDevices();
  } catch (error) {
    print(el("device-status"), String(error), true);
  }
}
```

- [ ] **Step 3: Commit**

```bash
git add apps/api_gateway/app/static/js/devices.js
git commit -m "$(cat <<'EOF'
feat(admin-ui): reassign a device's Profile from My Devices

Each row's new Profile select calls the existing
POST /v1/devices/mine/{id}/profile on change -- no new backend
endpoint. This is how an already-paired, unassigned device (or one
whose owner wants to move it to a different assistant) gets fixed up
after the new hard-deny gate lands.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Admin UI — read-only Profile column on All Devices, then manual verification

**Files:**
- Modify: `apps/api_gateway/app/static/js/devices.js`

**Interfaces:**
- Consumes: `profile_id` field already present on every device dict returned by `GET /v1/devices` (`services/auth/devices.py`).

- [ ] **Step 1: Add a read-only Profile column to `renderAllDeviceList`**

In `apps/api_gateway/app/static/js/devices.js`, add:

```js
function allDeviceProfileColumn() {
  return {
    key: "profile",
    label: "Profile",
    render: (d) => (d.profile_id ? escapeHtml(d.profile_id) : '<span class="hint">Unassigned</span>'),
  };
}
```

Replace `renderAllDeviceList`'s `columns:` array:

```js
    columns: [
      ...deviceColumns(true),
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `<button class="mini danger" data-device-revoke-any="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>`,
      },
    ],
```

with:

```js
    columns: [
      ...deviceColumns(true),
      allDeviceProfileColumn(),
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `<button class="mini danger" data-device-revoke-any="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>`,
      },
    ],
```

- [ ] **Step 2: Commit**

```bash
git add apps/api_gateway/app/static/js/devices.js
git commit -m "$(cat <<'EOF'
feat(admin-ui): show each device's Profile on the admin-only All Devices table

Read-only by design -- reassigning stays confined to My Devices
(owner-scoped) to avoid opening a new cross-tenant profile-visibility
surface. See the 2026-08-12 device-profile-pairing-admin-ui spec.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Manual verification of Tasks 4-6 together**

Start the dev server (whatever this repo's existing run/dev command is — check for a documented one before assuming `uvicorn app.main:app --reload`), then in a browser:

1. Log in, create (or reuse) a Profile from the Chat section's Profile bar.
2. Go to Devices. Confirm the "Add Device" form has a Profile select populated with that profile.
3. Try to pair with the code field filled but Profile left as "Select a profile…" — confirm the Pair button is refused with the "Choose a profile" message and no request is sent (check the Network tab).
4. Pair a real or manually-registered device (or, if no hardware is available, use `pair_init`/`pair_claim` directly via the API to get a valid code) with a Profile selected — confirm it appears in My Devices with that Profile pre-selected in its row's dropdown.
5. Change that row's Profile dropdown to a different profile (or to "Unassigned") — confirm the row persists the change after a refresh.
6. If logged in as admin, confirm All Devices shows the same device's Profile as plain text (or "Unassigned"), with no interactive control.

Report back what was actually exercised versus what couldn't be (e.g. no physical device available to generate a real pairing code) — do not claim end-to-end pairing was verified if only the claim-blocked/reassign/read-only pieces were.

---

## Final full-suite check

- [ ] Run: `pytest tests/unit/conversation tests/unit/auth -v`
Expected: PASS, no regressions across every file touched or reasoned about in this plan (`test_device_profile_binding.py`, `test_lugo_authz.py`, `test_lugo_disabled_cutoff.py`, `test_lugo_device_profile_gate.py`, `test_conversation_authz.py`, `test_conversation_device_profile_gate.py`).

Per this project's own guidance (`no-full-suite-while-coding`), do NOT run the entire repo's test suite mid-task — only at the very end, once, after all six tasks are committed.
