"""One-time conversion of legacy ownerless profiles to the shared flag.

`owner_id is None` used to mean two things at once: "an admin made it" and
"it is a template everyone may use". Profile.shared now carries the second
meaning on its own (see
docs/superpowers/specs/2026-08-14-shared-profile-clone-only-design.md), and
shared rows are clone-only -- so a straight "every ownerless row becomes
shared" would strand every device already bound to one.

Hence the device check: a template exactly one live device owner is running
becomes that owner's private profile, and the fleet survives the upgrade
untouched. Only templates nobody runs become shared.

Idempotent, so it is safe on every boot: afterwards `owner_id is None` implies
`shared is True`, and this only rewrites rows where both are false.
"""

from __future__ import annotations

import logging

from app.services.auth.devices import device_store
from app.services.profiles.store import profile_store

logger = logging.getLogger(__name__)


async def migrate_ownerless_profiles() -> None:
    legacy = [
        p for p in profile_store.list().values() if p.owner_id is None and not p.shared
    ]
    if not legacy:
        return

    # One pass over devices, not one query per profile: this runs on every boot
    # and the device table is small but the profile table is smaller still.
    # Revoked devices are excluded deliberately -- a revoked device is not a
    # running fleet member, and letting its owner claim the profile would hand
    # a stranger the row's llm.api_key.
    devices_by_profile: dict[str, list[dict]] = {}
    owners_by_profile: dict[str, set[str]] = {}
    for d in await device_store.list_all():
        if d.get("revoked"):
            continue
        name = d.get("profile_id") or ""
        if name:
            devices_by_profile.setdefault(name, []).append(d)
            owners_by_profile.setdefault(name, set()).add(d["user_id"])

    for profile in legacy:
        owners = owners_by_profile.get(profile.name, set())
        if len(owners) == 1:
            profile.owner_id = next(iter(owners))
            profile.shared = False
            logger.info(
                "profile '%s': ownerless template adopted by its device owner", profile.name
            )
        else:
            profile.shared = True
            if owners:
                bound = devices_by_profile.get(profile.name, [])
                # The operator reading this during the riskiest part of a
                # deploy needs to find these devices without a second lookup
                # -- name/serial are already in the dict device_store hands
                # back (_device_dict), so include them instead of sending the
                # reader on a raw-id hunt.
                device_desc = ", ".join(
                    f"{d['id']} ({d.get('name') or '?'}/{d.get('serial') or '?'})" for d in bound
                )
                logger.warning(
                    "profile '%s' is now a clone-only shared template but %d device(s) "
                    "across %d different owners are still bound to it (%s); those devices "
                    "will fall back to server defaults until an admin reassigns them",
                    profile.name,
                    len(bound),
                    len(owners),
                    device_desc,
                )
        profile_store.upsert(profile)
