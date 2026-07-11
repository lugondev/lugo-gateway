# ESP32 Display Auto-Detect (SSD1306 vs ST7789) — Design

## Context

`esp32-assistant` (nested repo, ESP-IDF) has a board-abstraction layer
(`components/board`) with a `match()`-based auto-detect mechanism and a
`AA_BOARD_AUTODETECT` Kconfig option — both already implemented and wired
into `board_detect_and_select()` (`components/board/board.c`). What's
missing: every board's `match()` is currently a stub, `static bool
match(void) { return true; }`, so `AA_BOARD_AUTODETECT` can't actually
distinguish between boards today — only the Kconfig-forced path
(`AA_BOARD_FORCE` / `AA_BOARD_NAME`) works.

Two boards make this concretely solvable right now: `lugo-s3-ssd1306`
and `lugo-s3-st7789`. Both target `CONFIG_IDF_TARGET_ESP32S3`, both are
unconditionally compiled into the same S3 build (`components/boards`
globs every `*/board_def.c`, gated internally by `#if
CONFIG_IDF_TARGET_ESP32S3`), and both wire their panel to the **same two
GPIOs** — 42/41 — just used differently (SPI sclk/mosi for ST7789, I2C
scl/sda for SSD1306). That shared wiring is what makes a real probe
possible: check for an I2C ACK on 42/41, and the answer tells you which
panel is physically present.

`lugo-c3-devkit` is out of scope: it's a different IDF target
(`CONFIG_IDF_TARGET_ESP32C3`, RISC-V vs the S3's Xtensa) built via a
separate `idf.py set-target` invocation, and it's the only board on that
target today, so `match()` has nothing to distinguish against. It stays
`return true;`. The mechanism built here (a generic I2C-probe utility) is
reusable if a second C3 board shows up later.

## Mechanism

### 1. Shared probe helper — `components/board/board_i2c_probe.c`

A small, target-only, board-agnostic utility (not SSD1306-specific —
lives in `components/board` alongside `board_select.c`/`board.c`, not in
`components/display`):

```c
// board_i2c_probe.h
#pragma once
#include <stdbool.h>
#include <stdint.h>

// Briefly brings up scl/sda as a throwaway I2C master bus and checks for
// an ACK at addr. Intended for use from board_def.c match() functions,
// called before any board is selected and before any peripheral driver
// claims these pins for real.
bool board_i2c_probe(int scl, int sda, uint16_t addr, int timeout_ms);
```

Implementation: `i2c_new_master_bus()` → `i2c_master_probe()` →
`i2c_del_master_bus()`. Returns true iff `i2c_master_probe()` returns
`ESP_OK`. `i2c_master_probe` already exists in the project's ESP-IDF
(`driver/i2c_master.h`) and is exactly an ACK-based presence check — no
new IDF dependency.

This file is **not** added to `test/Makefile`'s source lists (which
enumerate exact files per test target rather than globbing), so it can't
break the host-test build the way a glob-based inclusion could.

### 2. `lugo-s3-ssd1306/board_def.c` — real `match()`

```c
static bool match(void) {
    return board_i2c_probe(42, 41, 0x3C, 50) ||
           board_i2c_probe(42, 41, 0x3D, 50);
}
```

Checks both common SSD1306 addresses (0x3C and 0x3D — already documented
as the two options in `display_ssd1306_cfg_t`'s comment).

### 3. `lugo-s3-st7789/board_def.c` — real `match()`

```c
static bool match(void) {
    return !(board_i2c_probe(42, 41, 0x3C, 50) ||
             board_i2c_probe(42, 41, 0x3D, 50));
}
```

Deliberately the logical inverse of the ssd1306 check, not a bare
`return true` relying on `board_select()`'s `boards[0]` fallback. The
existing fallback is link-order dependent (already flagged as a footgun:
the original single-board `match()` "always wins by link order") — an
explicit inverse check makes ST7789 a deterministic default when no I2C
OLED answers, matching current behavior (ST7789 is the original/primary
board) without depending on registration order.

### 4. Pin-reuse safety (SPI vs I2C on GPIO 42/41)

Running an I2C probe on pins that may be physically wired to an ST7789's
SPI bus means clocking unstructured pulses into the panel before real
init. This is safe because `st7789_init()` — which only runs *after* a
board is selected — calls `esp_lcd_panel_reset(s_panel)`, which drives
the panel's hardware `RST` GPIO (`c->rst`) before any real init command
is sent. The hardware reset clears whatever the probe's clock/data
toggling did to the panel's internal shift register/state. This will be
recorded as a code comment next to `board_i2c_probe()`'s declaration and
at both `match()` call sites, since it's a non-obvious cross-file
invariant (the safety of `board_def.c`'s match() depends on a reset call
inside `st7789.c`'s init — not visible from either file alone).

### 5. C3 — no changes

`lugo-c3-devkit/board_def.c` keeps `static bool match(void) { return
true; }`. `board_i2c_probe()` is available to it (or a future second C3
board) without any changes to the helper itself.

## Testing

`board_i2c_probe.c` exercises real I2C hardware (bus init + ACK poll),
so — consistent with `ssd1306.c`/`st7789.c` already being target-only,
non-host-tested drivers — it is not host-tested. `board_select.c`'s pure
selection logic (already covered by `test/test_board_select.c`) is
unchanged by this work.

Verification is on-target only:
1. Build with `AA_BOARD_AUTODETECT` selected (`idf.py menuconfig` →
   Target board → Auto-detect), flash to hardware with a real SSD1306
   wired on 42/41 → boot log should read `board: lugo-s3-ssd1306`.
2. Same build, flash to hardware with a real ST7789 wired on 42/41 (or
   nothing wired) → boot log should read `board: lugo-s3-st7789`.
3. `idf.py build` must succeed for both `AA_BOARD_FORCE` (existing,
   unaffected) and `AA_BOARD_AUTODETECT` configs.

This session has no attached hardware, so steps 1–2 are the user's to
run after flashing; the plan will call this out explicitly rather than
claim on-target success.

## Out of scope

- C3 auto-detect (no second C3 board exists yet).
- Detecting anything beyond SSD1306-vs-ST7789 (e.g. panel size/variant
  auto-detect, mic/speaker auto-detect) — not requested, no shared-pin
  ambiguity to resolve for those today.
- Changing the default Kconfig choice away from `AA_BOARD_FORCE` — users
  opt into autodetect explicitly; forced-name builds are untouched.
