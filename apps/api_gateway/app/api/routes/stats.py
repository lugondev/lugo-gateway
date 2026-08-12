"""Role-scoped counts for the admin console's Home tab.

Everything else Home shows (model registry, active models, system health,
admin usage totals) is read straight from existing admin-only endpoints by
the frontend -- this route only supplies the three totals nothing else
already exposes: profiles, devices, sessions."""

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.core.actor import current_role, current_user_id, scope_user_id
from app.services.auth.devices import device_store
from app.services.history.store import session_store
from app.services.profile_visibility import profile_visible
from app.services.profiles.store import profile_store

router = APIRouter(prefix="/v1/stats", tags=["stats"])

# `last_seen_at` is only touched once per new WS handshake (see
# auth_guard.py), never on a heartbeat during a long-lived connection -- so
# this is a "recently active" proxy, not a live-online signal. The UI must
# label it accordingly and never call it "online".
_ACTIVE_RECENT_MINUTES = 30


def _is_recently_active(last_seen_at: str | None) -> bool:
    if not last_seen_at:
        return False
    seen = datetime.fromisoformat(last_seen_at)
    return (datetime.now(timezone.utc) - seen).total_seconds() <= _ACTIVE_RECENT_MINUTES * 60


@router.get("/home")
async def home_stats(request: Request) -> dict:
    user_id = current_user_id(request)
    role = current_role(request)

    profiles = profile_store.list()
    profile_count = sum(1 for p in profiles.values() if profile_visible(p, user_id))

    devices = (
        await device_store.list_all()
        if role == "admin"
        else await device_store.list_for_user(user_id or "")
    )
    live_devices = [d for d in devices if not d["revoked"]]
    active_recent = sum(1 for d in live_devices if _is_recently_active(d["last_seen_at"]))

    session_count = await session_store.count(user_id=scope_user_id(request))

    return {
        "success": True,
        "data": {
            "profiles": {"count": profile_count},
            "devices": {"count": len(live_devices), "active_recent": active_recent},
            "sessions": {"count": session_count},
        },
    }
