# Firmware Device Pairing (ESP32 + RPi) — Design

**Date:** 2026-07-24
**Status:** Approved (pending implementation plan)
**Scope:** Firmware only. Backend and web UI are unchanged.

## Problem

The device-pairing flow exists end-to-end on the server side but not on the
device side:

- **Backend:** `POST /v1/devices/pair/init`, `GET /v1/devices/pair/status`,
  `POST /v1/devices/pair/claim`, plus `resolve_ws_identity` which already
  accepts a per-device token via `?device_token=` on `/v1/lugo/stream`
  (falling back to the legacy shared `DEVICE_AUTH_TOKEN`).
- **Web UI:** `Devices.tsx` lets a logged-in user enter the 6-digit code, name
  the device, list devices, and revoke them.
- **Firmware:** ESP32 connects with a **compile-time shared secret**
  (`CONFIG_AA_DEVICE_TOKEN`, same on every unit) and RPi has no device token at
  all. Neither runs `pair/init`, shows a code, nor stores a per-device token.

The README for `esp32-assistant` states this explicitly: *"there is no
per-device pairing, revocation, or identity yet. Revisit this when the real
pairing flow lands in firmware."*

So from the end user's perspective there is no working "pair your device"
experience, even though the server is ready. This design fills exactly that gap.

## Non-Goals

- **No backend or web changes.** The server protocol is already complete.
- **No GPIO factory-reset button** — deferred to future work (see below). The
  design leaves a single hook (`clear_device_token()`) so it can be added later
  without touching the state machine.
- **No QR-code / BLE / WiFi-provisioning pairing.** Code-on-display only.
- **No device-to-profile assignment changes.** Profile selection stays as it is
  today (`CONFIG_AA_PROFILE` / `config.yaml`).

## Architecture

A boot-time **pairing state machine**, implemented natively on each device
(C on ESP32, Python on RPi), matching the existing server protocol:

```
BOOT
 └─ stored device token present?
     ├─ YES → CONNECT WS (?device_token=…)  ──(WS closed 401/403)──┐
     │                                                             │
     └─ NO  → PAIRING:                                             │
          POST /v1/devices/pair/init {serial} → {code, poll_token} │
          show code on display (if present) + always log code      │
          loop: GET /v1/devices/pair/status?poll_token every ~3s   │
             ├─ {claimed:false}         → keep polling              │
             ├─ {claimed:true, token}   → store token → CONNECT WS  │
             └─ 404 (session expired,   → POST /pair/init again,    │
                     10-min TTL)          show fresh code           │
                                                                    │
          clear stored token  ◄─────────────────────────────────────┘
          (web "Remove" → server revokes → next connect is auth-rejected
           → device wipes its token → returns to PAIRING)
```

Token resolution priority at boot:

1. **Explicit override** (`CONFIG_AA_DEVICE_TOKEN` / `config.yaml: device_token`)
   — for dev and legacy; if set, use it directly and skip pairing.
2. **Stored per-device token** (NVS / file) — the normal paired state.
3. **Pairing** — no token anywhere, run the flow above.

## Components per device

| Concern | ESP32 (C / ESP-IDF) | RPi (Python) |
|---|---|---|
| Serial (identity) | eFuse base MAC → lowercase hex `aabbccddeeff` | `/etc/machine-id` (stable across reflash) |
| Token storage | NVS namespace `lugo`, key `device_token` | file `~/.cache/agent-assistant/device_token`, mode `0600`, next to existing `session_id` |
| Show code | `display_show("Pair code", "123456")` if display present; **always** `ESP_LOGI` to serial | `oled.show("Pair code", "123456")` if OLED enabled; **always** log to journal |
| HTTP client | `esp_http_client` (new) | `urllib` / existing async pattern (cf. `_warm_stt_engine`) |
| Headless fallback | log-only (code always logged) | log-only |
| Override / bypass | `CONFIG_AA_DEVICE_TOKEN` (existing) | new optional `server.device_token` in `config.yaml` |

### Serial choice rationale

Hardware-derived serials (eFuse MAC / machine-id) are stable across firmware
reflash. A randomly generated per-boot or per-install UUID would create orphan
device rows in the user's list every time the token store is wiped. `find_active_by_serial`
on the server already dedups on serial, so a stable serial also means a re-paired
device is recognized as the same hardware.

## Data flow (paired steady state)

Unchanged from today: WS connect to `/v1/lugo/stream?device_token=<stored>`.
The only difference is the token now comes from NVS/file (obtained via pairing)
instead of a compile-time constant.

## Error handling

- **`pair/init` fails (network/DNS):** retry with backoff. RPi reuses
  `reconnect_initial_seconds` / `reconnect_max_seconds`; ESP32 uses an
  equivalent capped backoff.
- **Code expires mid-poll (`status` returns 404):** the 10-minute TTL lapsed
  before the user claimed. Re-run `pair/init`, display the fresh code.
- **WS closes with auth rejection (401/403):** treat as token revoked — clear
  the stored token and return to PAIRING.
  **Must be distinguished from an ordinary network drop:** only an
  auth-reject close code wipes the token; a transport error reconnects with the
  same token.

## Testing

- **RPi:** unit-test the state machine with a mocked HTTP layer — token
  present/absent, `claimed` transition, TTL-expire → re-init, revoke → wipe,
  network failure → backoff. Follows the existing host-side test pattern.
- **ESP32:** host-test the pure logic (token selection, `status` JSON parse,
  state decision) separated from hardware, the same way `ws_protocol` is
  host-tested today. Hardware paths (NVS, display, HTTP) are thin adapters
  exercised on-device manually.

## Future work (not in this spec)

- **GPIO factory-reset button:** hold a button N seconds → call
  `clear_device_token()` (the same hook the revoke path uses) → reboot into
  PAIRING. Adding it is "capture button event → call existing function →
  reboot"; it does not change the state machine.

## Open sub-tasks (for the implementation plan)

1. Shared: define the exact HTTP request/response shapes and poll cadence as a
   short protocol note both implementations follow.
2. RPi: serial source, token file store, HTTP calls, state machine, OLED/log
   code display, config `device_token` override, unit tests.
3. ESP32: eFuse MAC serial, NVS token store, `esp_http_client` calls, state
   machine, display/log code, keep `CONFIG_AA_DEVICE_TOKEN` override, host tests.
