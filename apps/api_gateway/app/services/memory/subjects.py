"""Subjects for memories with no identified person.

Memory is keyed on (profile_id, user_id): profile is *which assistant*, user_id is
*which person*. These fill the person axis when there is no person. They are NOT
about devices -- several devices sharing one profile is the intended behaviour,
and hardware identity does not belong on this axis.

Two values rather than one because the emptiness has two causes carrying very
different data: a device on the legacy shared DEVICE_AUTH_TOKEN is a real
authenticated deployment that identified nobody, while auth-disabled dev mode is
local scratch. They should not share a retention policy. See docs/decisions.md.

Kept in its own module with no database imports so `services/db/engine.py` can use
the constant during schema migration without importing the memory store, which
imports the engine.
"""

from __future__ import annotations

from app.core.settings import settings

# The `lugo:` prefix is load-bearing, not decoration. Real subjects are UUIDv4 and
# a colon never appears in one, so these cannot collide by construction rather
# than by luck. It also has to survive leaving this database: memgw in proxy mode
# can hand a subject to an external provider (Mem0, Zep) where it sits beside
# other applications' subjects.
ANON_SUBJECT = "lugo:anonymous"
DEV_SUBJECT = "lugo:dev"


def resolve_subject(user_id: str | None) -> str:
    """The subject a memory is stored under.

    Reads `settings.auth_enabled` rather than taking a flag: that is *exactly* the
    condition `resolve_ws_identity` uses to set `WsIdentity.unauthenticated`
    (auth_guard.py), so deriving it here keeps one chokepoint instead of threading
    an auth-mode flag through session.py, the routes, the extractor, the compactor
    and the retriever.
    """
    if user_id:
        return user_id
    return ANON_SUBJECT if settings.auth_enabled else DEV_SUBJECT
