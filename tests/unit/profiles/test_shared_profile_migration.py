"""One-time conversion of the old owner_id-is-None templates.

The rule exists to keep deployed fleets running: a template that a device is
already bound to becomes that device owner's private profile, so the speaker
keeps working across the upgrade. A template with two or more distinct live
device owners becomes shared (clone-only) instead, since it can't correctly
belong to just one of them, and an admin is warned to reassign the
conflicting devices.

DESIGN CHANGE: a template with *no* live bound devices used to become shared
too. Real deployments have working assistants (memory/session history keyed
by profile NAME) that have no device row bound to them at all -- sharing
those would have silently made them un-runnable. So a template with no live
bound devices is now adopted by the first admin (earliest-created user with
role == "admin") instead, and is left untouched only if no admin exists yet.
Sharing is now something an admin does deliberately afterwards, not
something this migration decides on its own.

Invariant afterwards: `owner_id is None` no longer always implies `shared is
True` -- it can also mean "no admin existed yet at migration time", a state
that is retried (and produces the same no-op) on every later boot until an
admin is created.
"""

import asyncio
import uuid

from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.models import Profile
from app.services.profiles.shared_migration import migrate_ownerless_profiles
from app.services.profiles.store import profile_store


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _make_admin() -> str:
    """Create an admin user and return its id."""
    admin = asyncio.run(user_store.create(_rand("admin"), "password123", role="admin"))
    return admin["id"]


def test_unbound_template_adopted_by_first_admin():
    admin_id = _make_admin()
    name = _rand("free")
    profile_store.upsert(Profile(name=name, owner_id=None))

    asyncio.run(migrate_ownerless_profiles())

    row = profile_store.get(name)
    assert row.owner_id == admin_id, "no fleet depends on it, so an admin adopts it"
    assert row.shared is False


def test_no_admin_exists_leaves_unbound_template_unmigrated(caplog):
    name = _rand("free")
    profile_store.upsert(Profile(name=name, owner_id=None))

    with caplog.at_level("INFO"):
        asyncio.run(migrate_ownerless_profiles())

    row = profile_store.get(name)
    assert row.owner_id is None, "there's no admin to hand it to yet"
    assert row.shared is False, "must not fall back to sharing it"
    assert name in caplog.text


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
    serial_a = _rand("serial")
    serial_b = _rand("serial")
    asyncio.run(device_store.create("alice", "device-a", serial_a, profile_id=name))
    asyncio.run(device_store.create("bob", "device-b", serial_b, profile_id=name))

    with caplog.at_level("WARNING"):
        asyncio.run(migrate_ownerless_profiles())

    row = profile_store.get(name)
    assert row.shared is True
    assert row.owner_id is None
    assert name in caplog.text, "an admin has to be told which bindings to fix"
    # The operator reading this warning during a deploy needs to identify the
    # devices without a second lookup -- raw device ids alone aren't enough.
    assert "device-a" in caplog.text and serial_a in caplog.text
    assert "device-b" in caplog.text and serial_b in caplog.text


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
    would give a stranger someone else's llm.api_key.

    An admin is present here so the assertion is unambiguous: if the revoked
    device were (wrongly) still counted, erin would be the sole live owner
    and would adopt the profile via the one-owner branch instead of the
    admin adopting it via the no-live-devices branch -- so this fails loudly
    if the revoked-device exclusion is ever removed.
    """
    admin_id = _make_admin()
    name = _rand("revoked")
    profile_store.upsert(Profile(name=name, owner_id=None))
    asyncio.run(device_store.create("erin", "speaker", _rand("serial"), profile_id=name))
    device_id = asyncio.run(device_store.list_for_user("erin"))[0]["id"]
    asyncio.run(device_store.revoke(device_id))

    asyncio.run(migrate_ownerless_profiles())

    row = profile_store.get(name)
    assert row.owner_id == admin_id, "erin's revoked device must not win the claim"
    assert row.owner_id != "erin"
    assert row.shared is False
