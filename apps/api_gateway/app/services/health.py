"""Pre-flight health for a profile's resolved STT/TTS engines.

Exists so a WS connect can be refused before the user speaks, instead of the
first utterance failing mid-turn. Engine resolution here mirrors what
api/routes/conversation.py and api/routes/lugo.py do, so the HTTP endpoint and
the WS gate can never disagree about which engine a profile actually uses.
"""

from __future__ import annotations

import asyncio

from app.schemas.health import EngineHealth, ProfileHealth
from app.services.profile_visibility import visible_profile_or_none, visible_tts_profile_or_none
from app.services.profiles.store import profile_store
from app.services.stt.profile import resolve_stt
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.profile_store import tts_profile_store
from app.services.tts.service import tts_service


async def check_resolved_engines(
    stt_engine: str, stt_model: str, tts_engine: str, tts_model: str
) -> tuple[EngineHealth, EngineHealth]:
    """Check both engines concurrently -- they hit different registry rows and
    different hosts, so serializing them would double the worst-case wait a
    connecting client sits through."""
    return await asyncio.gather(
        stt_service.check_engine(stt_engine, stt_model),
        tts_service.check_engine(tts_engine, tts_model),
    )


async def check_profile_health(profile_name: str | None, caller_id: str | None = None) -> ProfileHealth:
    """`caller_id` gates the profile the same way every other consumer of a
    client-supplied ?profile= name must (C2 --
    docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md): a
    profile that exists but isn't visible to `caller_id` resolves exactly
    like a nonexistent one (falls through to server defaults), so this never
    discloses another user's resolved engine/model or base_url. The one
    caller today (api/routes/profiles.py's GET /{name}/health) already
    404s before calling in here, so passing caller_id is defense-in-depth,
    not the only gate."""
    profile = visible_profile_or_none(
        profile_store.get(profile_name) if profile_name else None, caller_id
    )
    stt_engine, _language, stt_model = resolve_stt(profile)

    tts_name = (profile.tts.profile_name if profile else "") or None
    tts_profile = visible_tts_profile_or_none(
        tts_profile_store.get(tts_name) if tts_name else None, caller_id
    )
    if tts_profile and tts_profile.engine:
        tts_engine, tts_model = tts_profile.engine, tts_profile.model_id or ""
    else:
        tts_engine = system_config_store.get().engines.default_tts_engine
        tts_model = ""

    stt_health, tts_health = await check_resolved_engines(
        stt_engine, stt_model, tts_engine, tts_model
    )
    return ProfileHealth(profile=profile_name or "", stt=stt_health, tts=tts_health)
