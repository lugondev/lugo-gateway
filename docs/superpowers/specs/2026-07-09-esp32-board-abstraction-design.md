# ESP32 Board Abstraction Layer — Design

**Date:** 2026-07-09
**Component:** `esp32-assistant/` (ESP-IDF firmware, C)
**Status:** Approved for planning

## Problem

The firmware currently assumes exactly one hardware layout: INMP441 I2S mic +
MAX98357A I2S amp + ST7789 SPI display, with GPIO pins declared as a flat set in
`main/Kconfig.projbuild` (`AA_MIC_WS`, `AA_SPK_BCLK`, …). There is one driver per
capability and no way to select a different display driver, audio codec, or pin
map. We need to support **several boards that differ in chips/drivers** (e.g.
board A = ST7789 + MAX98357A, board B = SSD1306 OLED + ES8311 codec) while making
it easy to add more boards later.

## Goals

- Adding a new board is **one folder, zero edits to central files** (`main.c`,
  registry, root CMake untouched).
- A **single firmware binary** can run on multiple boards, auto-detecting which
  board it is at boot.
- Core logic and drivers are **testable/mockable on the host** without hardware.
- Room reserved for a future board that swaps whole subsystems (e.g. 4G modem
  instead of WiFi) without redesigning the abstraction.
- **Non-goal:** runtime hot-swap of boards; supporting the full xiaozhi board
  catalogue; changing `main.c`'s conversation logic.

## Non-negotiable constraints

- Language stays **C** (ESP-IDF). No C++ introduced.
- The existing capability interfaces (`audio.h`, `display.h`, `buttons.h`) keep
  their current function signatures so `main.c` is not rewritten.
- Existing host tests under `esp32-assistant/test/` keep working.

## Architecture

### 1. Ops structs (vtables)

Each hardware capability is described by a struct of function pointers that
mirrors today's interface exactly:

```c
// components/board/include/board_audio.h
typedef struct {
    esp_err_t (*init)(const void *cfg);
    int  (*mic_read)(int16_t *pcm, int samples);
    int  (*spk_write)(const int16_t *pcm, int samples);
    void (*spk_reset)(void);
    void (*set_volume)(int pct);
    int  (*get_volume)(void);
    int  (*adjust_volume)(int delta);
} audio_ops_t;

// board_display.h
typedef struct {
    esp_err_t (*init)(const void *cfg);
    void (*show)(const char *line1, const char *line2);
} display_ops_t;

// board_buttons.h
typedef struct {
    void (*start)(void (*on_press)(button_id_t id));
} buttons_ops_t;
```

### 2. `board_t` aggregate

```c
// components/board/include/board.h
typedef struct board {
    const char          *name;
    const audio_ops_t   *audio;
    const display_ops_t *display;
    const buttons_ops_t *buttons;
    // const net_ops_t  *net;   // RESERVED for future 4G/other-transport board; not implemented now
    const void          *audio_cfg;    // board-specific config blob (e.g. pin struct)
    const void          *display_cfg;
    bool               (*match)(void); // returns true if running on this board (probe)
} board_t;

const board_t *board_active(void);        // the selected/detected board
void           board_set(const board_t*); // test hook: force active board (mock)
esp_err_t      board_detect_and_select(void); // called at boot before capability init
```

### 3. Facade layer (keeps `main.c` unchanged)

The capability components keep their existing public functions; internally they
dispatch to the active board's ops:

```c
// components/audio/audio.c
static const audio_ops_t *s_ops;
esp_err_t audio_init(void) {
    s_ops = board_active()->audio;
    return s_ops->init(board_active()->audio_cfg);
}
int audio_mic_read(int16_t *pcm, int n) { return s_ops->mic_read(pcm, n); }
// … spk_write / spk_reset / set_volume / get_volume / adjust_volume identical shape
```

`main.c` continues to call `audio_mic_read()`, `display_show()`, `buttons_start()`
verbatim. Only requirement: `board_detect_and_select()` runs first in `app_main()`,
before `display_init()` / `audio_init()`.

### 4. Drivers

Concrete drivers move into `drivers/` subfolders of their capability component and
each exports one ops instance:

- `components/audio/drivers/i2s_std.c`  → `extern const audio_ops_t audio_i2s_ops;`
  (today's INMP441 + MAX98357A code, unchanged behaviour, pins read from `audio_cfg`)
- `components/audio/drivers/es8311.c`   → `audio_es8311_ops` (new board)
- `components/display/drivers/st7789.c` → `display_st7789_ops` (today's code)
- `components/display/drivers/ssd1306.c`→ `display_ssd1306_ops` (new board)
- `components/display/drivers/null.c`   → `display_null_ops` (headless board)
- `components/buttons/drivers/gpio_buttons.c` → `buttons_gpio_ops`

Pins are no longer hardcoded in the driver; the driver reads them from the
`*_cfg` blob the board supplies (e.g. `audio_i2s_cfg_t { mic_ws, mic_sck, mic_sd, spk_bclk, spk_lrc, spk_din }`).

### 5. Auto-registration (add a board = one folder)

Each board lives in `boards/<name>/board_def.c` and registers itself via a macro
that hides an ESP-IDF linker-set section (same technique esp-event / console use):

```c
// boards/lugo_s3_st7789/board_def.c
#include "board.h"
static const audio_i2s_cfg_t audio_pins = {
    .mic_ws=4, .mic_sck=5, .mic_sd=6, .spk_bclk=15, .spk_lrc=16, .spk_din=7,
};
static bool match(void) { return !i2c_probe(ES8311_I2C_ADDR); } // no ES8311 → this board

LUGO_BOARD_REGISTER(lugo_s3_st7789) {
    .name      = "lugo-s3-st7789",
    .audio     = &audio_i2s_ops,
    .display   = &display_st7789_ops,
    .buttons   = &buttons_gpio_ops,
    .audio_cfg = &audio_pins,
    .match     = match,
};
```

`LUGO_BOARD_REGISTER(sym)` expands to a `const board_t` placed in a dedicated
linker section; `board.c` iterates that section as an array. Adding a board
requires **no edit** to `board.c`, `main.c`, or the root `CMakeLists.txt`. The
`boards/` component's CMake globs `boards/*/board_def.c`.

### 6. Board selection / detection

`board_detect_and_select()` behaviour, controlled by a Kconfig `choice`:

- **Forced board** (default, one board built): Kconfig selects a single board by
  name; detection is skipped; `board_set()` picks it directly. Smallest binary —
  best for flash-constrained targets like ESP32-C3.
- **Auto-detect** (multi-board single binary): iterate the registered boards,
  call each `match()`, select the first that returns true. Recommended default
  `match()` strategy is an **I2C probe** — a board with an ES8311 codec answers on
  the I2C bus, an I2S-only board does not — so no extra hardware/straps are
  needed. If no board matches, fall back to a compile-time default board and log a
  warning.

## Data flow at boot

```
app_main()
  → board_detect_and_select()   // sets active board (forced or I2C-probed)
  → display_init()   → active->display->init(display_cfg)
  → audio_init()     → active->audio->init(audio_cfg)
  → buttons_start()  → active->buttons->start(cb)
  → (unchanged conversation logic)
```

## Error handling

- `board_detect_and_select()` returns `ESP_ERR_NOT_FOUND` only if the registry is
  empty (build misconfig); a no-`match` case falls back to the default board and
  logs, never aborts.
- Facade functions assert `s_ops != NULL` (i.e. `audio_init()` ran) in debug
  builds; in release they are guarded no-ops returning error where applicable.
- A driver `init` failure propagates through the facade's `*_init()` `esp_err_t`
  exactly as today (`ESP_ERROR_CHECK` in `main.c` still applies).

## Testing strategy

- **Host unit tests** (existing `test/` harness): a `mock_board.c` provides ops
  whose `mic_read` yields canned PCM and whose `show` records calls; tests call
  `board_set(&mock_board)` then exercise `main` conversation logic and the facades
  with no hardware.
- **Driver contract test**: a shared test asserts every registered board has
  non-NULL `audio`/`display`/`buttons` and a `match` (or is the default), catching
  a malformed `board_def.c` at test time.
- **On-target smoke**: build the current board (`lugo_s3_st7789`) and confirm
  parity with pre-refactor behaviour (mic → gateway → speaker, display lines,
  buttons) — this refactor must be behaviour-preserving for the existing board.

## Migration (behaviour-preserving for the current board)

1. Introduce `components/board/` (ops typedefs, `board_t`, registry macro,
   `board_active`/`board_set`/`board_detect_and_select`).
2. Convert `audio`/`display`/`buttons` public functions into facades; move current
   implementations into `drivers/*.c` exporting ops; make drivers read pins from
   `*_cfg` instead of Kconfig `AA_*` directly.
3. Add `boards/lugo_s3_st7789/board_def.c` describing today's hardware; wire pins
   from the existing `AA_*` Kconfig values (kept as the board's defaults) so no
   physical rewiring/config change is needed.
4. Insert `board_detect_and_select()` as the first call in `app_main()`.
5. Add the Kconfig `choice` (forced vs auto-detect) with `lugo_s3_st7789` forced by
   default.
6. Only after parity is confirmed: add the second board (`ssd1306` + `es8311`)
   folder and its drivers.

## Open items deferred (explicitly out of scope)

- `net_ops` implementation (WiFi vs 4G) — slot reserved in `board_t`, not built.
- LED / backlight / battery capabilities — add as new ops structs when a board
  needs them, following the same pattern.
