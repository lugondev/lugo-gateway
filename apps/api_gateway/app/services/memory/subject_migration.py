"""Move ownerless memories off the empty-string subject.

Memory is keyed on (profile_id, user_id). Rows written with no signed-in person
used `''`, which memgw's `Scope.subject` validator rejects outright -- so adopting
it needs a real value. See docs/decisions.md, "The ownerless memory bucket".

Everything existing becomes ANON_SUBJECT rather than being split between the two
sentinels: nothing recorded which case wrote a given row, and treating old data as
real user speech is the conservative error while treating it as scratch is the
destructive one.

Idempotent, so it is safe on every boot.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update

from app.services.db.engine import db_session
from app.services.db.models import MemoryItem, MemoryProfileDoc
from app.services.memory.store import ANON_SUBJECT

logger = logging.getLogger(__name__)


async def migrate_ownerless_memory_subject() -> None:
    async with db_session() as s:
        # memory_items: user_id is a plain column, so a bulk UPDATE is enough.
        facts = await s.execute(
            update(MemoryItem).where(MemoryItem.user_id == "").values(user_id=ANON_SUBJECT)
        )

        # memory_profile_docs is the one that gets forgotten. It holds the
        # compacted per-profile summary on the SAME (user_id, profile_id) key, so
        # migrating the facts without it splits a profile's memory in half -- the
        # facts move, the summary stays behind under '' and is never read again,
        # with nothing raising anywhere.
        #
        # Row by row, not a bulk UPDATE, because user_id is part of this table's
        # composite PRIMARY KEY: if a row already sits at the target key (a
        # half-finished earlier run, or a write that landed after the sentinel
        # shipped), a blind UPDATE hits a uniqueness violation instead of
        # no-opping. The already-migrated row wins and the legacy one is dropped
        # -- it is the older of the two by construction, since '' stopped being
        # written the moment the sentinel did.
        legacy_docs = (
            await s.execute(select(MemoryProfileDoc).where(MemoryProfileDoc.user_id == ""))
        ).scalars().all()
        moved = 0
        for row in legacy_docs:
            existing = await s.get(MemoryProfileDoc, (ANON_SUBJECT, row.profile_id))
            if existing is None:
                s.add(
                    MemoryProfileDoc(
                        user_id=ANON_SUBJECT,
                        profile_id=row.profile_id,
                        content=row.content,
                        updated_at=row.updated_at,
                    )
                )
                moved += 1
            await s.delete(row)

        await s.commit()

    if facts.rowcount or moved:
        logger.info(
            "memory subject migration: %s fact rows and %s profile docs moved to %s",
            facts.rowcount, moved, ANON_SUBJECT,
        )
