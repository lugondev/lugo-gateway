"""Which profile a paired device runs.

Before device↔profile binding existed, a device declared its own profile as a
query param / hello field sourced from firmware or yaml config. That made the
control panel unable to answer "what is this speaker running?", and changing a
speaker's assistant meant editing a file on the device.

Now `devices.profile_id` is the source of truth, with one deliberate asymmetry:

  * bound device      -> the binding wins, the device's own request is ignored,
                         and the caller is told so it isn't a silent override;
  * unbound device    -> the device's own request still applies, unchanged. This
                         is what keeps already-deployed fleets working: they have
                         no binding yet, so nothing about them changes;
  * anything else     -> untouched (browsers, the legacy shared fleet token, and
                         dev-mode all keep choosing per connection).

Deliberately returns a NAME plus an optional warning rather than a resolved
Profile: each WS route already resolves the name through
`visible_profile_or_none` with its own bypass rule and wraps warnings in its own
message envelope, and this must not become a second, subtly-different resolution
path around that check.
"""

from __future__ import annotations

from app.services.auth.devices import device_store


async def resolve_bound_profile(
    identity, requested: str | None
) -> tuple[str | None, str | None, bool, bool]:
    """Return (profile_name_to_use, warning_or_None, came_from_binding, hard_denied).

    `identity` is a core.auth_guard.WsIdentity; taken structurally rather than by
    import to keep this leaf module out of the auth_guard import cycle.

    The third element exists because a name the SERVER chose deserves gentler
    failure handling than one the CLIENT sent: lugo.py closes the connection on
    an unresolvable client-declared profile, which would turn a stale binding
    into a bricked speaker. Callers use it to fall back to defaults instead.

    The fourth element, `hard_denied`, is True iff this identity is a paired
    device (`via_device`) with no profile bound. Unlike the gentle from-binding
    fallback above, this is NOT recoverable by falling back to defaults --
    callers must refuse the connection outright. A paired device is meant to
    be centrally assigned; one that never was isn't "usable" and letting it
    quietly run on whatever it happened to ask for (or on server defaults)
    hides exactly the state an admin needs to go fix. Never True for any
    identity that isn't `via_device` -- browsers, the legacy shared fleet
    token, and dev-mode have no assignment to lack in the first place.
    """
    if not getattr(identity, "via_device", False):
        return requested, None, False, False
    device_id = getattr(identity, "device_id", None)
    if not device_id:
        return requested, None, False, True
    device = await device_store.get_by_id(device_id)
    bound = (device.profile_id or "") if device is not None else ""
    if not bound:
        return requested, None, False, True
    if requested and requested != bound:
        return bound, (
            f"this device is assigned to profile '{bound}'; "
            f"ignoring the profile '{requested}' it asked for"
        ), True, False
    return bound, None, True, False
