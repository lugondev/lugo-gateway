# Device↔Profile Pairing in the Admin Console — Design

Date: 2026-08-12

## Problem

The admin static console (`apps/api_gateway/app/static/`) already has a device
pairing flow (code shown on the device → typed into "Add Device" → claimed)
and a `devices.profile_id` binding already exists in the backend
(`services/auth/devices.py`, `POST /v1/devices/mine/{id}/profile`), but the
admin console never wires a profile into either the claim step or the
existing-device list. A newly paired device ends up unassigned
(`profile_id=""`) with no UI path to bind it to a profile, and an unbound
device is currently allowed to run anyway — it falls back to whatever profile
it requests on its own, or server defaults (`services/auth/device_profile.py`).

Goal: make pairing a device to a profile a first-class, required step in the
admin console, make already-paired-but-unassigned devices visible and fixable
from the same screen, and make the backend actually refuse to run a paired
device that has no profile bound (rather than silently falling back).

## Scope

In scope:
- Admin console's existing **Devices** section (`index.html` `#section-devices`,
  `static/js/devices.js`).
- `POST /v1/devices/pair/claim` request from the admin console (backend
  already accepts `profile_id`; this is a UI-only wiring change there).
- `services/auth/device_profile.py` and its two callers (`lugo.py`,
  `conversation.py`) — new hard-deny behavior for unbound paired devices.

Out of scope (explicitly deferred, confirmed with user):
- Cross-tenant profile reassignment from the admin-only **All Devices**
  table. That table gets a read-only "Profile" column (name + "Unassigned"),
  not an editable one — editing stays confined to devices the caller owns
  (`My Devices`), reusing the existing owner-scoped
  `POST /v1/devices/mine/{id}/profile` endpoint. No new admin-scoped
  cross-user endpoint is added.
- The legacy shared `device_auth_token` fleet fallback
  (`identity.via_fleet_token`). The new hard-deny gate only applies to
  `identity.via_device` (devices paired through `device_store`).
- `stt.py`'s standalone WS STT stream — it doesn't call
  `resolve_bound_profile` today and isn't part of the paired-device
  conversation flow this change targets.
- `lugo-web-client` (the React client) — it already sends `profile_id` on
  claim per the schema's own comment; nothing to change there.

## Part 1 — Admin UI: require a Profile when pairing

`index.html`, inside `#section-devices`'s "Add Device" row: add a
`<select id="device-pair-profile">` next to the existing code/name inputs,
populated the same way `profiles.js` already populates `#profile-select`
(iterate `profileData`, sorted by name, label existing-user's-own profiles
with `(mine)` via `profileData[name]?.owner_id`). No new endpoint — this
reuses `profileData`, already fetched by `profiles.js` on page load.

`devices.js`'s `claimDevice()`:
- Reads `device-pair-profile`'s value.
- Blocks submission (same inline `print(status, ..., true)` pattern already
  used for missing code/name) if it's empty.
- Sends it as `profile_id` in the `POST /v1/devices/pair/claim` body.
  `PairClaimRequest.profile_id` already exists server-side
  (`schemas/devices.py`) — this is UI-only.

## Part 2 — Admin UI: fix up already-paired, unassigned devices

`My Devices` table (`renderMyDeviceList` in `devices.js`): add a "Profile"
column. Each row renders an inline `<select>` (same `profileData` source as
Part 1) defaulting to the device's current `profile_id`, or an "Unassigned"
placeholder option when empty. Changing it calls the existing
`POST /v1/devices/mine/{id}/profile` and refreshes the list — no new backend
endpoint.

`All Devices` table (`renderAllDeviceList`, admin-role-only): add a read-only
"Profile" column showing the profile name or "Unassigned". No select, no new
endpoint — this table stays observational for anything beyond revoke, per the
existing IDOR-avoidance posture of `_checked_profile_name`
(`api/routes/devices.py`) that scopes profile visibility to the *device
owner*, not the caller.

## Part 3 — Backend: refuse to run an unbound paired device

`services/auth/device_profile.py`'s `resolve_bound_profile(identity,
requested)` gains a fourth return value, `hard_denied: bool` (return shape
becomes `(profile_name, warning_or_None, from_binding, hard_denied)`):

- `hard_denied = True` iff `identity.via_device` is `True` and the device's
  `profile_id` is empty (paired hardware, no assigned assistant).
- Deliberately keyed on `via_device` only — `via_fleet_token` identities are
  untouched, same as today.

Both call sites (`api/routes/lugo.py`, `api/routes/conversation.py`) check
`hard_denied` immediately after calling `resolve_bound_profile`, before doing
anything else with the (possibly `None`) resolved profile:
- Send that route's own error-frame shape (`{"type": "error", "message": ...}`
  for `lugo.py`, `{"event": "error", ...}` for `conversation.py`) with a
  message telling the caller the device has no assigned profile.
- Close the socket with an **ordinary close (no 401/403/4401 code)**. This is
  the one non-obvious constraint: firmware's revoke-vs-network-drop
  classifier (`classify_disconnect` / `aa_classify_disconnect`) only wipes
  the stored device token and re-pairs on a 401/403 handshake rejection or a
  `goodbye{reason=account_disabled}`. The device's token is still valid here
  — only the profile binding is missing — so reusing that close code would
  make the device destroy a perfectly good token and loop re-pairing forever,
  never fixing the actual problem (which only admin action in the console
  can fix). An ordinary close keeps the token, and firmware's normal
  reconnect/backoff behavior applies.

### Breaking change, intentional

Any already-paired device that has never been assigned a profile (the
previous default, and the current state of any pre-existing fleet) stops
being able to run a session the moment this ships, until an admin binds it to
a profile via Part 2's UI. This is the explicit point of the change — a
device without a bound profile is not considered "usable" — and was confirmed
with the user rather than assumed.

## Testing

- Unit: `resolve_bound_profile` — new case for `via_device` + empty
  `profile_id` → `hard_denied=True`; existing cases (bound, unbound-non-device,
  `via_fleet_token`) stay `hard_denied=False`.
- Route tests: `lugo.py` and `conversation.py` WS tests for the new
  error-frame + ordinary-close path on an unbound device identity.
- Any existing fixture/test that connects as an unbound `via_device` identity
  and expects success will need updating to either bind a profile first or
  assert the new denial — audit `tests/unit` for device WS fixtures with
  `profile_id=""` before landing this.
- Admin UI: manual check (no JS test harness exists for this static console
  today, matches existing precedent for `devices.js`) — claim blocked without
  a profile selected, claim succeeds with one, My Devices reassignment works,
  All Devices shows read-only profile/"Unassigned".
