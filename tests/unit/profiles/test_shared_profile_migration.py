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
