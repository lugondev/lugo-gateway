"""The ownerless memory bucket moves from `''` to a named subject.

Why at all: memgw's `Scope.subject` rejects an empty string, so adopting it needs
a real value here. Why *two* values: a device on the legacy shared
DEVICE_AUTH_TOKEN is a real authenticated deployment that simply identified no
person, while auth-disabled dev mode is local scratch — same emptiness, very
different data, and they should not share a retention policy.

See docs/decisions.md, "The ownerless memory bucket".
"""

import asyncio

import pytest
from sqlalchemy import select

from app.core.settings import settings
from app.services.db.engine import db_session
from app.services.db.models import MemoryItem, MemoryProfileDoc
from app.services.memory.store import ANON_SUBJECT, DEV_SUBJECT, MemoryStore, _uid
from app.services.memory.subject_migration import migrate_ownerless_memory_subject


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def _auth_on(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


# ---- which sentinel ----

def test_real_user_id_passes_through(_auth_on):
    assert _uid("1363bc98-40a4-4331-8f2b-43aa95840905") == "1363bc98-40a4-4331-8f2b-43aa95840905"


def test_no_user_with_auth_enabled_is_anonymous(_auth_on):
    """The legacy shared DEVICE_AUTH_TOKEN path: real deployment, real speech,
    nobody identified."""
    assert _uid(None) == ANON_SUBJECT
    assert _uid("") == ANON_SUBJECT


def test_no_user_with_auth_disabled_is_dev(monkeypatch):
    """auth_enabled False is exactly the condition resolve_ws_identity uses to set
    `unauthenticated=True`, so the store can read it directly instead of having the
    flag threaded through every call site."""
    monkeypatch.setattr(settings, "admin_password", "")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "")
    assert settings.auth_enabled is False
    assert _uid(None) == DEV_SUBJECT


def test_sentinels_cannot_collide_with_a_real_user_id():
    """Real ids are UUIDv4; a colon never appears in one. Collision-freedom is
    structural, not luck -- so this asserts the shape, not the spelling."""
    for s in (ANON_SUBJECT, DEV_SUBJECT):
        assert ":" in s, f"{s} must contain a colon to be UUID-proof"
        assert s.startswith("lugo:"), f"{s} must be namespaced"


# ---- the migration ----

def _insert_legacy_rows() -> None:
    async def _go():
        async with db_session() as s:
            s.add(MemoryItem(id="m-old", profile_id="kitchen", content="x", user_id=""))
            s.add(MemoryItem(id="m-owned", profile_id="kitchen", content="y", user_id="u-real"))
            s.add(MemoryProfileDoc(user_id="", profile_id="kitchen", content="doc"))
            await s.commit()
    asyncio.run(_go())


def _rows():
    async def _go():
        async with db_session() as s:
            items = {r.id: r.user_id for r in (await s.execute(select(MemoryItem))).scalars()}
            docs = {(r.user_id, r.profile_id) for r in (await s.execute(select(MemoryProfileDoc))).scalars()}
            return items, docs
    return asyncio.run(_go())


def test_migration_rewrites_both_tables(_auth_on):
    """`memory_profile_docs` is the one that gets forgotten: it holds the compacted
    per-profile summary on the same key. Move the facts without it and a profile's
    memory is split in half, with nothing raising."""
    _insert_legacy_rows()
    asyncio.run(migrate_ownerless_memory_subject())

    items, docs = _rows()
    assert items["m-old"] == ANON_SUBJECT, "facts table not migrated"
    assert items["m-owned"] == "u-real", "a real user's rows must not be touched"
    assert (ANON_SUBJECT, "kitchen") in docs, "profile-doc table not migrated"
    assert ("", "kitchen") not in docs


def test_migration_is_idempotent(_auth_on):
    _insert_legacy_rows()
    asyncio.run(migrate_ownerless_memory_subject())
    asyncio.run(migrate_ownerless_memory_subject())

    items, docs = _rows()
    assert items["m-old"] == ANON_SUBJECT
    assert len([d for d in docs if d[1] == "kitchen"]) == 1, "re-running duplicated a row"


def test_migration_maps_everything_to_anonymous_not_dev(_auth_on):
    """Existing rows cannot be split between the two sentinels -- nothing recorded
    which case wrote them. They all become anonymous: treating old data as real
    speech is the conservative error, treating it as scratch is the destructive
    one."""
    _insert_legacy_rows()
    asyncio.run(migrate_ownerless_memory_subject())
    items, _ = _rows()
    assert items["m-old"] != DEV_SUBJECT


def test_migration_survives_an_already_migrated_row_colliding(_auth_on):
    """`memory_profile_docs.user_id` is part of a composite primary key, so the
    rewrite is a PK change. If a row already sits at the target key, a blind
    UPDATE would violate the constraint rather than no-op."""
    async def _seed():
        async with db_session() as s:
            s.add(MemoryProfileDoc(user_id="", profile_id="kitchen", content="legacy"))
            s.add(MemoryProfileDoc(user_id=ANON_SUBJECT, profile_id="kitchen", content="already"))
            await s.commit()
    asyncio.run(_seed())

    asyncio.run(migrate_ownerless_memory_subject())  # must not raise

    _, docs = _rows()
    assert ("", "kitchen") not in docs
    assert (ANON_SUBJECT, "kitchen") in docs
