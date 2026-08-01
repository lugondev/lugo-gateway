# Default device name from the AP name, plus rename

**Date:** 2026-08-01
**Status:** approved, not yet implemented
**Touches:** `apps/api_gateway` (monorepo), `lugo-web-client` (separate repo). **No firmware change.**

## Problem

Pairing asks the user to invent a device name before the device exists. `PairWizard`
puts the 8-digit code and a required free-text name in the same form, and disables
the submit button until both are filled (`PairWizard.tsx:119`). The user is holding a
speaker that already calls itself `Lugo-48D0` on its setup AP, but has to think up a
name for it anyway, at the one moment they know least about it.

## Decision

Pairing completes as soon as the code is accepted. The device gets a default name
derived from the same hardware identity its setup AP advertises, and renaming is a
separate, always-available action — offered once right after pairing, and from the
device list forever after.

## 1. Where the default name comes from

The server derives it from the `serial` it already receives at `pair/init`. No new
field on the wire, no firmware change:

```
default_device_name(serial) = "Lugo-" + serial[-4:].upper()
"2884855048d0" -> "Lugo-48D0"
```

This matches the AP SSID the device shows during provisioning, which is built as
`"Lugo-%02X%02X"` of the MAC's last two bytes (`esp32-assistant/components/provisioning/provisioning_ssid.c`).
Verified against real hardware on 2026-08-01: serial `2884855048d0`, observed AP
`Lugo-48D0`.

**Known coupling, to be commented on both sides.** The firmware builds the SSID from
the **STA interface MAC** (`provisioning.c`, `esp_wifi_get_mac(WIFI_IF_STA, ...)`) but
the pairing serial from the **efuse base MAC** (`main.c`, `esp_efuse_mac_get_default`).
On stock ESP-IDF these are the same address, which is why the derivation is exact
today. A custom MAC configuration could make them diverge, and the default name would
then not match the AP. This is cosmetic only — the name is a suggestion the user can
change — so it is documented rather than defended against.

**Degenerate serials.** If `serial` has fewer than 4 characters (never true for real
hardware, but the field is a free-form string on the wire), fall back to the literal
`"New device"` rather than emitting a truncated `Lugo-` fragment.

## 2. API changes

### a) `pair/claim` takes an optional name

`PairClaimRequest.name` becomes `name: str = ""`. When the trimmed value is empty the
route substitutes `default_device_name(entry.serial)`.

Older clients keep working untouched: the static panel (`apps/api_gateway/app/static/js/devices.js`)
always sends a name, and an explicitly supplied name still wins. That panel is **out
of scope** for this change.

### b) New rename endpoint

`POST /v1/devices/mine/{device_id}/name`, body `DeviceNameRequest{name: str}`.

It mirrors `set_my_device_profile` deliberately:

- login required (`current_user_id`, else `AuthError`);
- scoped to the caller's own devices;
- 404 for both "no such device" and "someone else's device", keeping the two
  indistinguishable per the rule stated in `_checked_profile_name`;
- **does not touch the pairing token or the profile binding** — a name is a label, not
  hardware identity, so renaming must never send the user back to read a fresh code.

Validation: trim; reject empty (400); cap at 128 characters to match the
`Device.name` column (`String(128)`).

Store: add `DeviceStore.set_name(device_id, name, owner_user_id) -> bool`, shaped like
the existing `set_profile`.

## 3. UI (`lugo-web-client`)

- `claimDevice` already returns the created `Device`, so the wizard can read the new
  device's `id` and `name` with no change to that function's return type. Its `name`
  parameter becomes optional.
- **PairWizard, `code` step:** the name input is removed. Submit enables on a
  full-length code alone.
- **PairWizard, `done` step:** a `TextInput` prefilled with the device's name, with
  `[Done]` and `[Save]`. `Save` calls the rename endpoint then `onPaired()`; `Done`
  calls `onPaired()` with no request. If the field still holds the name the server
  assigned, `Save` skips the request too — an untouched field is not an edit. Pairing
  has already succeeded by this point, so neither button can fail the pairing.
- **DeviceRow:** add `Rename device` to the existing `MenuButton` items, opening a
  small modal built from `Modal` + `TextInput`, following `MoveDeviceModal`.
- The rename modal is extracted as its own `RenameDeviceModal` component, because
  `DeviceRow` has **two** parents that must both offer the action —
  `screens/settings/AllDevices.tsx` and `screens/profiles/ProfileDevices.tsx`. They
  already share `MoveDeviceModal` the same way; a second copy would drift.

Both entry points call the same endpoint.

## 4. Error handling

| Case | Behaviour |
|---|---|
| Rename a device you don't own, or one that's gone | 404; the server's message is shown in the dialog verbatim. Not routed through `friendlyDeviceError` — that function only rewrites "invalid or expired" and "already paired", neither of which a rename can produce |
| Empty name after trim | `Save` disabled client-side; server also rejects with 400 |
| Name over 128 chars | Server 400; client caps input length |
| Rename fails in the wizard's `done` step | Modal stays open with the error; the device is already paired, so `Done` remains a clean exit |

## 5. Testing

**Gateway**
- `default_device_name`: normal serial → `Lugo-XXXX`; short/degenerate serial → `New device`.
- Claim with no name → device gets the derived name.
- Claim with an explicit name → that name wins.
- Rename happy path.
- Rename another user's device → 404.
- Rename to an empty string → 400.

**Web client**
- `PairWizard.test.tsx` updated: no name field in the code step; `done` step prefilled;
  `Save` issues the rename call; `Done` issues none.
- `DeviceRow`: the rename item appears and opens the modal.

**Firmware** — unchanged, no new tests.

## Explicitly out of scope

- The static `devices.js` panel.
- Any change to the device→profile binding flow.
- Name uniqueness. Two devices may share a name; `Device.name` has no unique
  constraint and pairing two identical boards is not an error.
