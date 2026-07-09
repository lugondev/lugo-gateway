# ESP32 ST7789 Boot/Status Display

## Problem

`esp32-assistant` has no visual feedback at all. WiFi provisioning mode
(SoftAP + captive portal, added earlier) is invisible unless you already know
the AP name to look for; connection/session state is only visible via serial
log, which requires a real terminal (`idf.py monitor` doesn't work from a
sandboxed shell, and isn't convenient for everyday use anyway). The user has
wired a 1.54" ST7789 SPI LCD (240x240) to show this information instead.

## Hardware

- Panel: ST7789, 240x240, SPI, no MISO needed.
- Pins: SCLK=GPIO42, MOSI=GPIO41, DC=GPIO1, RST=GPIO2, BL=GPIO17.
- BL is driven as a constant-high plain GPIO output once at init — no
  PWM/brightness control (user confirmed no dedicated backlight driving
  needed).
- CS is not wired to any GPIO — assumed tied to GND on the breakout module
  (only SPI device on the bus), so the driver configures `cs_gpio_num = -1`.
- **Known pin conflict, accepted by user:** DC=GPIO1/RST=GPIO2 are the same
  pins as `CONFIG_AA_I2C_SDA`/`CONFIG_AA_I2C_SCL` (the ES8311 audio codec's
  I2C bus, confirmed wired and working on a different board earlier this
  session). This is fine for a display-only test board. If audio + display
  are ever combined on one physical board, DC/RST (or the I2C pins) must
  move to different GPIOs — this spec does not attempt to resolve that,
  it's a wiring decision for whenever that combination is actually built.

## Goals

- Show WiFi provisioning status (AP SSID + IP) while the device is in
  SoftAP/captive-portal mode.
- Show WiFi/gateway connection progress and the final connected state.
- Show a short error message on WiFi timeout or a WS error event.

## Non-goals

- No graphical UI, animations, or icons — text only.
- No Vietnamese diacritics in on-screen text (ASCII bitmap font only) —
  status strings are short fixed English/technical phrases (SSID, IP,
  host:port, "Connecting...", "Error").
- No brightness control / PWM backlight dimming.
- No touch input or interaction — display-only.

## Architecture

New component `components/display`:

- `display_init()` — configures the SPI bus (SCLK/MOSI, no MISO), brings up
  `esp_lcd_panel_st7789` (ESP-IDF managed component, same pattern as
  `78/esp-opus`/`esp_codec_dev` already used in this project via
  `idf_component.yml`) with `cs_gpio_num = -1`, `dc_gpio_num = 1`,
  `reset_gpio_num = 2`, 240x240 resolution, no offset. Drives `BL` (GPIO17)
  high once. Clears the screen to black.
- `display_show(const char *line1, const char *line2)` — clears the screen
  and draws up to two lines of text, each centered horizontally, using an
  8x16 ASCII bitmap font baked into the component. `line2` may be NULL to
  show a single line.

**Text layout is split out as pure C** (host-testable, matching this
project's `ws_protocol`/`provisioning_form` pattern):
- `display_font.h`/`.c`: the 8x16 glyph bitmap table (ASCII 0x20-0x7E) and
  `display_layout_line(const char *text, int screen_width, int *out_x)` —
  given a string and the screen width, computes the starting x pixel for
  horizontal centering (returns -1 if the text is wider than the screen,
  caller should not attempt to draw it in that case — none of this spec's
  fixed status strings are expected to hit that case at 240px width with an
  8px-wide font, i.e. up to 30 characters per line, but the function still
  reports it rather than silently overflowing).
- `display.c`: uses `display_font.h`'s glyph table + `display_layout_line`'s
  x offset to blit pixels via `esp_lcd_panel_draw_bitmap` — this part is
  ESP-IDF/SPI-only, not host-testable, verified on-device (same as
  `provisioning.c`'s SoftAP/DNS/HTTP orchestration).

## Integration points

| State | Trigger (existing code) | Text shown |
|---|---|---|
| Provisioning mode active | `provisioning_start()`, right after SoftAP comes up | `"Setup WiFi"` / `"<ssid> <ip>"` (e.g. `"Lugo-48D0 192.168.9.1"`) |
| WiFi connecting | `main.c`, before `wifi_sta_wait_connected()` | `"Connecting WiFi..."` (single line) |
| WiFi OK, gateway connecting | `main.c`, after WiFi connects, before `ws_client_start()` | `"WiFi OK"` / `"Connecting gateway..."` |
| Gateway session ready | `on_event()`'s `WSP_EV_SESSION_STARTED` case | `"Connected"` / `"<host>:<port>"` |
| WiFi timeout (pre-provisioning-fallback) | `main.c`, `!wifi_sta_wait_connected()` branch | `"WiFi failed"` / `"Starting setup AP..."` |
| Gateway/WS error | `on_event()`'s `WSP_EV_ERROR` case | `"Error"` / `<ev->text, truncated to fit>` |

`main` and `provisioning` both gain a dependency on the new `display`
component.

## Error handling

- If `display_init()` fails (e.g. panel init error), `ESP_ERROR_CHECK` aborts
  — consistent with this project's existing convention for unrecoverable
  init failures (see `wifi_sta_start`, `audio_init` callers).
- `display_show()` truncates any text longer than fits on one line (via
  `display_layout_line` reporting overflow) rather than wrapping or
  scrolling — out of scope for this spec.

## Testing

- Host-testable: `display_layout_line()` (pure centering-math function, no
  hardware) gets unit tests in `test/test_display_font.c`, following the
  existing `test/test_provisioning_ssid.c`/`test_provisioning_form.c`
  pattern (plain C11, added to `test/Makefile`).
- Not host-testable: `display_init()`/`display_show()`'s actual SPI/panel
  code — verified on-device: confirm the ST7789 shows correct, centered,
  readable text for each of the six states in the table above, and confirm
  the panel isn't mirrored/offset (240x240 assumed with no offset; fix if
  the user reports it looks wrong).
