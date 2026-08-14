"""One-time conversion of legacy ownerless profiles to the shared flag.

`owner_id is None` used to mean two things at once: "an admin made it" and
"it is a template everyone may use". Profile.shared now carries the second
meaning on its own (see
docs/superpowers/specs/2026-08-14-shared-profile-clone-only-design.md), and
shared rows are clone-only -- so a straight "every ownerless row becomes
shared" would strand every device already bound to one.

DESIGN CHANGE (see docs/decisions.md): the first version of this migration
turned every profile with zero live bound devices into a shared, clone-only
row. Inventorying the real deployment DB showed that was wrong -- profiles
like `esp32-assistant`, `dev`, `fast`, and `host` are working assistants with
no *device* row bound to them (memory and session history are keyed by
profile NAME, not by a device link), so sharing them would have silently
made every one of them un-runnable, and cloning to "recover" one starts empty
and orphans that history. The migration must preserve the status quo, not
disable a running assistant, so a row with zero live bound devices is now
handed to the first admin instead of being shared. Sharing a profile is now
something an admin does deliberately afterwards, never something this
migration does on its own.

Three outcomes for each legacy row (`owner_id is None and not shared`):
- exactly one live device owner  -> that owner adopts it (`shared=False`), so
  the fleet survives the upgrade untouched;
- two or more distinct live owners -> `shared=True`, `owner_id` stays `None`,
  and a WARNING names the row so an admin can reassign the conflicting
  devices;
- no live bound devices -> the first ACTIVE admin (earliest-created user with
  `role == "admin"` and `disabled` false -- same condition as
  `UserStore.count_active_admins()`) adopts it (`shared=False`); if no active
  admin exists (a fresh install, or every admin account disabled), the row is
  left exactly as it is and re-evaluated on a later boot -- there is
  deliberately no shared-by-default fallback here, and no falling back to a
  disabled admin or to some other non-admin user either.

Idempotent, so it is safe on every boot. The invariant is no longer "owner_id
is None implies shared" (that stopped holding the moment a no-admin-yet row
could stay `owner_id=None, shared=False` forever). What actually holds: the
one-owner and no-devices-with-admin branches clear `owner_id is None`, and
the multi-owner branch sets `shared=True`; either way the row stops matching
the `owner_id is None and not shared` filter, so it is never touched again.
The one row shape that keeps matching that filter -- no live devices and no
admin yet -- produces the identical no-op (leave the row alone, log, retry
next boot) every time it is re-evaluated, so state stops changing after the
first run even though the filter keeps re-selecting it. That is the property
idempotency actually needs here.
"""

from __future__ import annotations

import logging

from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.profiles.store import profile_store

logger = logging.getLogger(__name__)


async def _first_admin_id() -> str | None:
    """Earliest-created ACTIVE admin, or None if there isn't one.

    "Active" matches UserStore.count_active_admins()'s condition (role ==
    "admin" and not disabled) -- a disabled account has had its access
    withdrawn, so handing it every legacy assistant would make those
    profiles owned by someone who cannot use them, and since
    profile_usable() requires owner_id == caller, nobody else could run them
    either. That reproduces the exact outage this migration exists to
    prevent, so a disabled admin must not be treated as available here.

    UserStore's only bulk read is `list()` (ordered by username, not
    created_at), so the "earliest" selection happens here rather than
    inventing a new store query.
    """
    admins = [
        u for u in await user_store.list() if u["role"] == "admin" and not u["disabled"]
    ]
    if not admins:
        return None
    return min(admins, key=lambda u: u["created_at"])["id"]


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

    admin_id = await _first_admin_id()

    for profile in legacy:
        owners = owners_by_profile.get(profile.name, set())
        if len(owners) == 1:
            updated = profile.model_copy(update={"owner_id": next(iter(owners)), "shared": False})
            logger.info(
                "profile '%s': ownerless template adopted by its device owner", profile.name
            )
        elif len(owners) >= 2:
            updated = profile.model_copy(update={"shared": True})
            bound = devices_by_profile.get(profile.name, [])
            # The operator reading this during the riskiest part of a deploy
            # needs to find these devices without a second lookup -- name/
            # serial are already in the dict device_store hands back
            # (_device_dict), so include them instead of sending the reader
            # on a raw-id hunt.
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
        else:
            # No live device is bound at all. Sharing used to be the default
            # here, but real deployments have working assistants sitting in
            # exactly this state (no *device* row bound, but still in active
            # use by name -- see the module docstring's design-change note),
            # so the safe default is to hand the row to an admin rather than
            # lock it read-only.
            if admin_id is None:
                # Fresh install, nothing configured yet: don't invent a
                # shared row where none was asked for. Leave it and let the
                # next boot (after an admin exists) resolve it.
                logger.info(
                    "profile '%s': no live device is bound to it and no admin user "
                    "exists yet; left unmigrated (owner_id=None, shared=False) -- "
                    "will be re-evaluated on the next boot",
                    profile.name,
                )
                continue
            updated = profile.model_copy(update={"owner_id": admin_id, "shared": False})
            logger.info(
                "profile '%s': no live device is bound to it; adopted by the first "
                "admin (%s)",
                profile.name,
                admin_id,
            )
        profile_store.upsert(updated)
