"""Precedence rules for `services/auth/device_profile.resolve_bound_profile`.

The whole point of the binding is that the control panel is the single source of
truth for what a speaker runs. These tests pin the three cases that make that
true without breaking fleets deployed before bindings existed.
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

    name, warning, from_binding = await resolve_bound_profile(identity, "stale-yaml-profile")

    assert name == "kitchen"
    assert from_binding is True
    # Announced, not silent: a config file on the device that no longer has any
    # effect should be visible to whoever is looking at the device's logs.
    assert warning is not None
    assert "kitchen" in warning and "stale-yaml-profile" in warning


@pytest.mark.asyncio
async def test_binding_agreeing_with_the_request_warns_about_nothing(store):
    device_id = await _device(store, profile_id="kitchen")
    identity = FakeIdentity(device_id=device_id, via_device=True)

    name, warning, from_binding = await resolve_bound_profile(identity, "kitchen")

    assert (name, warning, from_binding) == ("kitchen", None, True)


@pytest.mark.asyncio
async def test_unbound_device_keeps_choosing_for_itself(store):
    """This is what stops the feature from breaking already-deployed fleets:
    they have no binding, so nothing about them changes."""
    device_id = await _device(store)
    identity = FakeIdentity(device_id=device_id, via_device=True)

    assert await resolve_bound_profile(identity, "kitchen") == ("kitchen", None, False)
    assert await resolve_bound_profile(identity, None) == (None, None, False)


@pytest.mark.asyncio
async def test_non_device_identities_are_untouched(store):
    """Browsers, the legacy shared fleet token and dev-mode all keep picking a
    profile per connection -- only a paired device has an assignment to obey."""
    device_id = await _device(store, profile_id="kitchen")

    browser = FakeIdentity(user_id="u1")
    assert await resolve_bound_profile(browser, "study") == ("study", None, False)

    # via_device without a device_id can't be looked up; treat as unbound
    # rather than guessing.
    headless = FakeIdentity(via_device=True)
    assert await resolve_bound_profile(headless, "study") == ("study", None, False)

    # The bound device exists and is ignored by both identities above.
    bound = FakeIdentity(device_id=device_id, via_device=True)
    assert (await resolve_bound_profile(bound, "study"))[0] == "kitchen"


@pytest.mark.asyncio
async def test_deleted_device_row_falls_back_to_the_request(store):
    identity = FakeIdentity(device_id="no-such-device", via_device=True)
    assert await resolve_bound_profile(identity, "study") == ("study", None, False)
