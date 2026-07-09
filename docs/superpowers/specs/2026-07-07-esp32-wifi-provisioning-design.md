# ESP32 WiFi Provisioning (SoftAP + Captive Portal)

## Problem

`esp32-assistant` currently reads WiFi SSID/password and gateway host/port from
Kconfig (`CONFIG_AA_WIFI_SSID`, `CONFIG_AA_WIFI_PASS`, `CONFIG_AA_SERVER_HOST`,
`CONFIG_AA_SERVER_PORT`) — compile-time constants baked into the binary. Moving
the device to a new WiFi network or gateway IP requires editing `sdkconfig` and
reflashing. If the configured credentials are wrong, `app_main` logs
`wifi timeout` and returns — the device is dead until reflashed.

## Goals

- Configure WiFi SSID/password and gateway host/port at runtime, no reflash.
- No physical display required (board has none; out of scope per README).
- Self-healing: wrong/changed WiFi credentials fall back to a configuration
  mode automatically, rather than requiring a button press or reflash.

## Non-goals

- STT/TTS engine, profile, language stay Kconfig compile-time (rarely change
  per-deployment; can be added to the portal later if needed).
- No OLED/LCD display output (no hardware in this project).
- No BLE or ESP-IDF `wifi_provisioning` component/companion-app flow — plain
  browser-based captive portal only.

## Flow

1. On boot, `provisioning_start()` loads saved WiFi SSID/password and gateway
   host/port from NVS (namespace `aa_cfg`).
2. If no saved SSID, or `wifi_sta_wait_connected()` doesn't succeed within
   15s, switch to provisioning mode:
   - Start SoftAP: SSID `Lugo-XXXX` (`XXXX` = last 4 hex chars of the STA MAC
     address — stable across reboots, not re-randomized), open (no password).
   - AP IP fixed at `192.168.9.1` (override ESP-IDF SoftAP default of
     `192.168.4.1` via `esp_netif_set_ip_info`).
   - Start a minimal DNS server that answers every A-record query with
     `192.168.9.1`, so phones/laptops trigger their "Sign in to network"
     captive-portal popup automatically after joining the AP.
   - Start `esp_http_server` serving:
     - `GET /` (and any other path, for captive portal detection): HTML form
       with fields WiFi SSID, WiFi password, Gateway host, Gateway port.
       Fields pre-filled with current NVS values if present, else the Kconfig
       defaults (`CONFIG_AA_SERVER_HOST`/`CONFIG_AA_SERVER_PORT`).
     - `POST /save`: validate non-empty SSID and numeric port, write all four
       values to NVS, respond with a short "Saved. Restarting…" HTML page,
       then call `esp_restart()` after flushing the HTTP response.
3. On restart, step 1 runs again with the newly saved credentials.
4. If STA connects successfully (normal case after provisioning, or on any
   boot where saved credentials are already correct), proceed with the
   existing `app_main` flow unchanged (audio init, ws_client, mic/spk tasks).

## Components

- **`components/wifi` (modified)**
  - `wifi_sta_start()` takes an `ssid`/`password` pair as parameters instead
    of reading `CONFIG_AA_WIFI_SSID`/`CONFIG_AA_WIFI_PASS` directly.
  - New: `wifi_cfg_load(wifi_cfg_t *out)` / `wifi_cfg_save(const wifi_cfg_t *)`
    — NVS read/write for `{ssid, password, server_host, server_port}` under
    namespace `aa_cfg`. `wifi_cfg_load` falls back to Kconfig defaults for any
    field not yet saved in NVS (first boot: host/port pre-filled from Kconfig,
    ssid/password empty → provisioning triggers immediately).

- **New `components/provisioning`**
  - `provisioning_start(const wifi_cfg_t *current)`: brings up SoftAP + DNS +
    HTTP server, blocks (or runs as a FreeRTOS task) until the form is
    submitted, then restarts the device. Does not return normally — the only
    exit path is `esp_restart()`.
  - Depends on `esp_http_server`, `esp_netif`, `lwip` (DNS hijack via a raw
    UDP socket on port 53, not a full `dns_server` component dependency).

- **`main.c` (modified)**
  - `app_main`: load `wifi_cfg_t` via `wifi_cfg_load`; call
    `wifi_sta_start(cfg.ssid, cfg.password)`; on
    `!wifi_sta_wait_connected(15000)`, call `provisioning_start(&cfg)` instead
    of logging an error and returning.
  - `wsp_config_t.host`/`.port` come from the loaded `wifi_cfg_t` instead of
    `CONFIG_AA_SERVER_HOST`/`CONFIG_AA_SERVER_PORT` directly.

## Error handling

- Malformed POST body (missing field, non-numeric port): re-render the form
  with an inline error message, HTTP 400, do not save/restart.
- NVS write failure: log error, re-render form with a generic "failed to
  save, try again" message, do not restart (avoid a restart loop with no
  progress).
- SoftAP or HTTP server start failure: falls through to `ESP_ERROR_CHECK`
  (existing project convention — reboots on unrecoverable init failure).

## Testing

- Host-testable: NVS load/save round-trip and Kconfig-fallback logic
  (`wifi_cfg_load`/`wifi_cfg_save`) can be unit tested similarly to
  `ws_protocol`'s existing host-tested approach, if the NVS calls are
  abstracted behind a thin interface. If not straightforward to host-test,
  cover with on-device manual verification instead (documented in README).
- Manual verification on hardware (required regardless of unit test
  coverage): erase NVS, boot, confirm `Lugo-XXXX` AP appears, join it, confirm
  captive portal prompt appears, submit real WiFi credentials, confirm device
  restarts and connects, confirm gateway connection still works end-to-end.

## Open questions resolved during brainstorming

- No physical display exists or is planned — captive portal replaces the
  "show IP on screen" idea from the initial request.
- AP IP fixed at `192.168.9.1` (user requested, overriding ESP-IDF default).
- Config scope limited to WiFi + gateway host/port; other settings stay
  Kconfig for now.
