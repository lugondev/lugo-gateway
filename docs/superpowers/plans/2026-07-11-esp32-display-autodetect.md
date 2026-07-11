# ESP32 Display Auto-Detect (SSD1306 vs ST7789) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `AA_BOARD_AUTODETECT` actually distinguish `lugo-s3-ssd1306` from `lugo-s3-st7789` at boot, by giving each board's `match()` a real I2C-ACK probe instead of the current `return true;` stub.

**Architecture:** A new board-agnostic helper, `board_i2c_probe()`, briefly brings up a throwaway I2C bus on given pins and checks for an ACK at a given address. Each S3 board's `match()` calls it against the shared 42/41 pins with the SSD1306's known addresses (0x3C/0x3D); `lugo-s3-ssd1306` returns true on ACK, `lugo-s3-st7789` returns the logical inverse (so it's the deterministic default when nothing/an ST7789 is wired).

**Tech Stack:** ESP-IDF v5.x (`driver/i2c_master.h`, `i2c_master_probe()`), C, `esp32-assistant` nested repo (gitlink inside `speech-text-transformer`).

## Global Constraints

- Project: `/Users/lugon/code/speech-text-transformer/esp32-assistant` (nested gitlink repo — commit inside it, then bump the gitlink pointer in the parent repo as a separate final commit, matching existing history, e.g. `c6641f2 chore: bump esp32-assistant (...)`).
- Probe timeout: `50` ms for every `board_i2c_probe()` call (one fixed value across both boards — no per-call tuning).
- SSD1306 addresses to check: `0x3C` (primary) and `0x3D` (alt) — both documented in `display_ssd1306_cfg_t`'s existing comment.
- Shared physical pins: GPIO 42 / GPIO 41 on both `lugo-s3-ssd1306` (scl/sda) and `lugo-s3-st7789` (sclk/mosi) — read pin numbers from each board's existing `display_cfg` struct fields, don't re-hardcode the literals `42`/`41` in `match()`.
- `board_i2c_probe.c` is target-only (real I2C hardware) — do **not** add it to `test/Makefile`'s `SRC_BOARD_SEL`/`SRC_FACADES` lists; host tests must stay green without it.
- `lugo_c3_devkit/board_def.c` is out of scope — do not modify it.
- Building requires `source ~/esp/esp-idf/export.sh` first (once per shell) so `idf.py` is on `PATH`.
- Never run `git add -A` / `git add .` in either repo — the parent repo (`speech-text-transformer`) has unrelated pre-existing uncommitted changes (`apps/api_gateway/...`, `tests/unit/test_stt_routes.py`) that must not be touched or committed by this plan. Stage only the exact files this plan creates/modifies.

---

### Task 1: Shared I2C probe helper

**Files:**
- Create: `esp32-assistant/components/board/include/board_i2c_probe.h`
- Create: `esp32-assistant/components/board/board_i2c_probe.c`
- Modify: `esp32-assistant/components/board/CMakeLists.txt`

**Interfaces:**
- Produces: `bool board_i2c_probe(int scl, int sda, uint16_t addr, int timeout_ms)` — true iff a device ACKs at `addr` on a throwaway I2C bus using `scl`/`sda`. Tasks 2 and 3 call this.

- [ ] **Step 1: Write `board_i2c_probe.h`**

```c
#pragma once
#include <stdbool.h>
#include <stdint.h>

// Briefly brings up scl/sda as a throwaway I2C master bus and checks for an
// ACK at addr, then tears the bus back down. For use from board_def.c
// match() functions at board-selection time — before any board is chosen
// and before any peripheral driver claims these pins for real. Safe to call
// even when the pins are actually wired to a different bus (e.g. an
// ST7789's SPI clock/data): whichever board wins re-initializes its own
// hardware afterward, and ST7789 specifically gets a real RST-pin reset
// before any init command is sent, clearing whatever this probe's clocking
// left behind.
bool board_i2c_probe(int scl, int sda, uint16_t addr, int timeout_ms);
```

- [ ] **Step 2: Write `board_i2c_probe.c`**

```c
#include "board_i2c_probe.h"
#include "driver/i2c_master.h"

bool board_i2c_probe(int scl, int sda, uint16_t addr, int timeout_ms) {
    i2c_master_bus_config_t bus_config = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = sda,
        .scl_io_num = scl,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus_handle;
    if (i2c_new_master_bus(&bus_config, &bus_handle) != ESP_OK) {
        return false;
    }

    bool found = i2c_master_probe(bus_handle, addr, timeout_ms) == ESP_OK;

    i2c_del_master_bus(bus_handle);
    return found;
}
```

- [ ] **Step 3: Add the new source file and required components to `components/board/CMakeLists.txt`**

Current content:
```cmake
idf_component_register(
    SRCS "board_active.c" "board_select.c" "board.c"
    INCLUDE_DIRS "include")
```

New content:
```cmake
idf_component_register(
    SRCS "board_active.c" "board_select.c" "board.c" "board_i2c_probe.c"
    INCLUDE_DIRS "include"
    REQUIRES driver esp_driver_i2c)
```

(`REQUIRES driver esp_driver_i2c` matches the existing pattern in `components/display/CMakeLists.txt`, which uses the same `driver/i2c_master.h` API.)

- [ ] **Step 4: Build to confirm the new file compiles**

```bash
source ~/esp/esp-idf/export.sh
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
idf.py build
```
Expected: build succeeds (`Project build complete.`). Nothing calls `board_i2c_probe()` yet, so this only proves the new file compiles and links cleanly into `libboard.a`.

- [ ] **Step 5: Confirm host tests are unaffected**

```bash
cd /Users/lugon/code/speech-text-transformer/esp32-assistant/test
make test
```
Expected: all tests pass, including `test_board_select` and `test_board_facades` (these don't reference `board_i2c_probe.c`, since it's not in `SRC_BOARD_SEL`/`SRC_FACADES`).

- [ ] **Step 6: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
git add components/board/include/board_i2c_probe.h components/board/board_i2c_probe.c components/board/CMakeLists.txt
git commit -m "feat(board): add shared I2C ACK-probe helper for match()"
```

---

### Task 2: Real `match()` for `lugo-s3-ssd1306`

**Files:**
- Modify: `esp32-assistant/components/boards/lugo_s3_ssd1306/board_def.c`

**Interfaces:**
- Consumes: `bool board_i2c_probe(int scl, int sda, uint16_t addr, int timeout_ms)` (Task 1).

- [ ] **Step 1: Add the include**

In `esp32-assistant/components/boards/lugo_s3_ssd1306/board_def.c`, change:
```c
#include "board.h"
#include "i2s_mic.h"
#include "i2s_speaker.h"
#include "display_ssd1306.h"
#include "buttons_gpio.h"
#include "sdkconfig.h"
```
to:
```c
#include "board.h"
#include "board_i2c_probe.h"
#include "i2s_mic.h"
#include "i2s_speaker.h"
#include "display_ssd1306.h"
#include "buttons_gpio.h"
#include "sdkconfig.h"
```

- [ ] **Step 2: Replace the stub `match()` and its comment**

Change:
```c
// Kconfig-forced like lugo_s3_st7789 — match() is only consulted under
// AA_BOARD_AUTODETECT, where two ESP32-S3 boards both unconditionally
// matching would be ambiguous; pick one explicitly via AA_BOARD_FORCE
// instead (the Kconfig default) when both are compiled into the same build.
static bool match(void) { return true; }
```
to:
```c
// Real probe for AA_BOARD_AUTODETECT: an SSD1306 ACKs I2C at 0x3C or 0x3D
// on the same scl/sda pins display_cfg uses below. lugo-s3-st7789's
// match() is the logical inverse of this same check (see that file), so
// exactly one of the two S3 boards matches regardless of link/registration
// order.
static bool match(void) {
    return board_i2c_probe(display_cfg.scl, display_cfg.sda, display_cfg.i2c_addr, 50) ||
           board_i2c_probe(display_cfg.scl, display_cfg.sda, 0x3D, 50);
}
```

(`display_cfg` is the `static const display_ssd1306_cfg_t` declared earlier in this same file — `.scl = 42, .sda = 41, .i2c_addr = 0x3C`.)

- [ ] **Step 3: Build to confirm it compiles**

```bash
source ~/esp/esp-idf/export.sh
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
idf.py build
```
Expected: build succeeds. The current `sdkconfig` still has `CONFIG_AA_BOARD_FORCE=y` / `CONFIG_AA_BOARD_NAME="lugo-s3-ssd1306"`, so `match()` isn't called yet at runtime (Task 4 exercises `AA_BOARD_AUTODETECT`) — this step only checks the code compiles.

- [ ] **Step 4: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
git add components/boards/lugo_s3_ssd1306/board_def.c
git commit -m "feat(board): real I2C-ACK match() for lugo-s3-ssd1306"
```

---

### Task 3: Real `match()` for `lugo-s3-st7789`

**Files:**
- Modify: `esp32-assistant/components/boards/lugo_s3_st7789/board_def.c`

**Interfaces:**
- Consumes: `bool board_i2c_probe(int scl, int sda, uint16_t addr, int timeout_ms)` (Task 1).

- [ ] **Step 1: Add the include**

In `esp32-assistant/components/boards/lugo_s3_st7789/board_def.c`, change:
```c
#include "board.h"
#include "i2s_mic.h"
#include "i2s_speaker.h"
#include "display_st7789.h"
#include "buttons_gpio.h"
#include "tp4056_battery.h"
#include "sdkconfig.h"
```
to:
```c
#include "board.h"
#include "board_i2c_probe.h"
#include "i2s_mic.h"
#include "i2s_speaker.h"
#include "display_st7789.h"
#include "buttons_gpio.h"
#include "tp4056_battery.h"
#include "sdkconfig.h"
```

- [ ] **Step 2: Replace the stub `match()`**

Change:
```c
static bool match(void) { return true; }   // Kconfig-forced; single S3 board
```
to:
```c
// Real probe for AA_BOARD_AUTODETECT: the logical inverse of
// lugo-s3-ssd1306's match(). Its scl/sda are these same physical pins
// (used here as SPI sclk/mosi instead) — if an SSD1306 ACKs I2C at
// 0x3C/0x3D there, this board loses; if neither answers, this board wins,
// which makes ST7789 the deterministic default when nothing is wired at
// all (matching this board's original single-board behavior).
static bool match(void) {
    return !(board_i2c_probe(display_cfg.sclk, display_cfg.mosi, 0x3C, 50) ||
              board_i2c_probe(display_cfg.sclk, display_cfg.mosi, 0x3D, 50));
}
```

(`display_cfg` here is the `static const display_st7789_cfg_t` declared earlier in this file — `.sclk = 42, .mosi = 41, .dc = 1, .rst = 2, .bl = 17`.)

- [ ] **Step 3: Build to confirm it compiles**

```bash
source ~/esp/esp-idf/export.sh
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
idf.py build
```
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
git add components/boards/lugo_s3_st7789/board_def.c
git commit -m "feat(board): real I2C-ACK match() for lugo-s3-st7789 (inverse probe)"
```

---

### Task 4: Verify both Kconfig configs build, restore state, bump gitlink

**Files:**
- Modify (temporarily, then restored): `esp32-assistant/sdkconfig`
- Modify: `speech-text-transformer/esp32-assistant` (gitlink pointer, in the parent repo)

**Interfaces:**
- Consumes: nothing new — this is a build/regression pass over Tasks 1–3's combined result.

- [ ] **Step 1: Confirm the current (unaffected) `AA_BOARD_FORCE` config still builds**

```bash
source ~/esp/esp-idf/export.sh
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
grep -n "AA_BOARD" sdkconfig
```
Expected output includes:
```
CONFIG_AA_BOARD_LUGO_S3_SSD1306=y
# CONFIG_AA_BOARD_AUTODETECT is not set
CONFIG_AA_BOARD_FORCE=y
CONFIG_AA_BOARD_NAME="lugo-s3-ssd1306"
```
```bash
idf.py build
```
Expected: build succeeds (this is the config already exercised in Tasks 1–3).

- [ ] **Step 2: Switch to `AA_BOARD_AUTODETECT` and build**

Edit `esp32-assistant/sdkconfig`, changing:
```
# CONFIG_AA_BOARD_LUGO_S3_ST7789 is not set
CONFIG_AA_BOARD_LUGO_S3_SSD1306=y
# CONFIG_AA_BOARD_AUTODETECT is not set
CONFIG_AA_BOARD_FORCE=y
CONFIG_AA_BOARD_NAME="lugo-s3-ssd1306"
```
to:
```
# CONFIG_AA_BOARD_LUGO_S3_ST7789 is not set
# CONFIG_AA_BOARD_LUGO_S3_SSD1306 is not set
CONFIG_AA_BOARD_AUTODETECT=y
# CONFIG_AA_BOARD_FORCE is not set
CONFIG_AA_BOARD_NAME=""
```
Then:
```bash
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
idf.py reconfigure
idf.py build
```
Expected: `reconfigure` accepts the edited `sdkconfig` without prompting for other changes (the four lines above are the only `AA_BOARD_*` symbols this choice touches), and `idf.py build` succeeds — this is the config where both boards' `match()` functions actually run at `board_detect_and_select()` time.

- [ ] **Step 3: Restore the original `sdkconfig`**

```bash
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
git status --short sdkconfig
```
Expected: `M sdkconfig` (only this file changed).
```bash
git checkout -- sdkconfig
idf.py reconfigure
git status --short sdkconfig
```
Expected: no output (clean — back to `AA_BOARD_FORCE` / `lugo-s3-ssd1306`, matching the tree's state before this task).

- [ ] **Step 4: Re-run host tests as a final regression check**

```bash
cd /Users/lugon/code/speech-text-transformer/esp32-assistant/test
make test
```
Expected: all tests pass.

- [ ] **Step 5: Bump the gitlink pointer in the parent repo**

```bash
cd /Users/lugon/code/speech-text-transformer
git status --short
```
Confirm the only esp32-assistant-related change listed is the `esp32-assistant` gitlink itself (the pre-existing unrelated changes to `apps/api_gateway/...` and `tests/unit/test_stt_routes.py` must still be present and untouched).
```bash
git add esp32-assistant
git commit -m "chore: bump esp32-assistant (SSD1306/ST7789 I2C auto-detect match())"
```

- [ ] **Step 6: Record the manual on-target verification still owed**

This plan cannot flash real hardware. Before considering display auto-detect done, the user needs to, on real boards, with `AA_BOARD_AUTODETECT` selected:
1. Flash a board with a real SSD1306 wired to GPIO 42/41 → boot log (`idf.py monitor`) should read `board: lugo-s3-ssd1306`.
2. Flash a board with a real ST7789 wired to GPIO 42/41 (or nothing wired) → boot log should read `board: lugo-s3-st7789`.

No file changes for this step — it's a reminder to surface to the user at the end of the implementation run, not something to automate.

## Self-Review Notes

- **Spec coverage:** shared probe helper (Task 1) — covered; ssd1306 `match()` (Task 2) — covered; st7789 inverse `match()` (Task 3) — covered; pin-reuse safety comment (Tasks 1–3's code comments) — covered; C3 untouched — covered by Global Constraints explicitly excluding it; build verification for both Kconfig configs (Task 4) — covered; on-target test call-out (Task 4 Step 6) — covered.
- **Placeholder scan:** no TBD/TODO; every step has literal code or literal commands with expected output.
- **Type consistency:** `board_i2c_probe(int scl, int sda, uint16_t addr, int timeout_ms)` — signature identical in the Task 1 header, Task 1 implementation, and both call sites in Tasks 2–3.
