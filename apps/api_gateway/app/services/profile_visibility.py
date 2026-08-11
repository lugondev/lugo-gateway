"""Shared owner_id visibility rule for Profile / TtsProfile rows, applied to
every CONSUMER of a client-supplied ``?profile=`` / ``?tts_profile=`` name.

Background (docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md,
finding C2): profiles.py and tts_profiles.py already enforce owner_id
visibility on every CRUD route (see profiles.py's ``_visible`` /
tts_profiles.py's ``_visible``, both defined in terms of the predicates
below). But every consumer -- conversation.py, lugo.py, livehost.py (before
the livehost plugin left this repo; its own traffic now reaches
conversation.py's own check the same way a browser's does), stt.py,
services/health.py, services/conversation/session.py -- used to resolve the
name with no check at all, so any signed-up user could name another user's
private profile and run on that victim's ``llm.api_key``, ``system_prompt``
and private ``mcp_servers``.

A profile/tts-profile is visible to a caller iff it's a template
(``owner_id is None`` -- visible to everyone) or the caller owns it
(``owner_id == caller_id``).

Deliberately just the predicate + a tiny "collapse to None" wrapper here, NOT
a resolve-by-name-from-the-global-store helper: every consumer site keeps
calling `profile_store.get(...)` / `tts_profile_store.get(...)` through its
OWN already-imported module-level binding (several test modules
monkeypatch e.g. `app.api.routes.lugo.profile_store` with a fresh/pointed
store for isolation -- a shared helper that imported the store itself would
silently bypass those seams). Call sites do::

    profile = visible_profile_or_none(profile_store.get(name) if name else None, caller_id)

``visible_profile_or_none`` / ``visible_tts_profile_or_none`` return ``None``
both when the row doesn't exist and when it exists but belongs to someone
else -- callers MUST keep those two cases indistinguishable (same status,
same message, same close code) so this fix doesn't create a new enumeration
oracle on top of the pre-existing ones (global 409-on-create, the "profile
not found" warning itself)."""

from __future__ import annotations

from app.services.profiles.models import Profile
from app.services.tts.profile_models import TtsProfile


def profile_visible(profile: Profile, caller_id: str | None) -> bool:
    return profile.owner_id is None or profile.owner_id == caller_id


def tts_profile_visible(profile: TtsProfile, caller_id: str | None) -> bool:
    return profile.owner_id is None or profile.owner_id == caller_id


def visible_profile_or_none(
    profile: Profile | None, caller_id: str | None, *, bypass: bool = False
) -> Profile | None:
    """Collapse "belongs to someone else" into the same None as "doesn't
    exist" for an already-fetched (possibly None) Profile.

    `bypass=True` is for the ONE precedented exception in this codebase:
    `WsIdentity.unauthenticated` (auth_guard.py) -- set only when
    settings.auth_enabled is False (dev mode), where there is no way to
    prove ownership of anything and every other consumer of a WS identity
    (see ws_session_owner_denied) already treats it as fully unscoped,
    matching current_role()'s identical "no session role -> admin" dev-mode
    default on the HTTP side. Do NOT pass bypass=True for a merely-None
    caller_id from a REAL auth-enabled deployment (e.g. the legacy shared
    device_auth_token) -- that must still only ever see templates."""
    if profile is None:
        return None
    if bypass or profile_visible(profile, caller_id):
        return profile
    return None


def visible_tts_profile_or_none(
    profile: TtsProfile | None, caller_id: str | None, *, bypass: bool = False
) -> TtsProfile | None:
    """TtsProfile equivalent of visible_profile_or_none."""
    if profile is None:
        return None
    if bypass or tts_profile_visible(profile, caller_id):
        return profile
    return None
