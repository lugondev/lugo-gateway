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
