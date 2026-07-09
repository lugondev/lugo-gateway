# ESP32 Board Abstraction Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a C board-abstraction layer to the ESP32 firmware so hardware drivers are selectable per board, adding a new board is one folder, and one binary can auto-detect its board — while keeping the current board's behaviour identical.

**Architecture:** Each hardware capability (audio/display/buttons) gets an *ops struct* (vtable of function pointers). The existing public functions (`audio_init`, `display_show`, `buttons_start`, …) become thin **facades** that dispatch to the active board's ops, so `main.c` is unchanged. Concrete drivers move into `drivers/` subfolders and read their pins from a per-board config blob. Boards live in `components/boards/<name>/board_def.c`, self-register into a linker section via a macro, and are picked at boot by `board_detect_and_select()` (Kconfig-forced today; I2C-probe auto-detect ready for later boards).

**Tech Stack:** C11, ESP-IDF v5.x (esp_lcd, i2s_std, FreeRTOS), host unit tests via `cc` + `test/Makefile`.

## Global Constraints

- Language is **C** only — no C++ introduced.
- Public signatures in `audio.h`, `display.h`, `buttons.h` are **unchanged** (`main.c` must not need edits beyond one added `board_detect_and_select()` call).
- `button_id_t` enum values stay `BTN_WAKE=0, BTN_VOL_UP=1, BTN_VOL_DOWN=2`.
- **Behaviour-preserving** for board `lugo-s3-st7789`: identical GPIO numbers and identical I2S/SPI configuration to today.
- Host tests follow the existing `test/Makefile` + `CHECK(cond)` pattern; host-only shims go in `test/shims/`.
- Commit after each task. All paths are relative to `esp32-assistant/` unless noted.
- The plan's core deliverable is the framework + migrating the **one** existing board. Adding the second physical board (SSD1306 + ES8311 drivers, I2C-probe `match()`) is a follow-up — the framework makes it a one-folder add.

---

### Task 1: Board core — types, active-board holder, selection logic

Pure C, fully host-testable. No ESP-IDF dependencies in the tested files.

**Files:**
- Create: `components/board/include/board_types.h`
- Create: `components/board/include/board.h`
- Create: `components/board/board_active.c`
- Create: `components/board/board_select.c`
- Create: `components/board/CMakeLists.txt`
- Modify: `components/buttons/include/buttons.h` (move `button_id_t` to board_types.h)
- Create: `test/shims/esp_err.h`
- Test: `test/test_board_select.c`
- Modify: `test/Makefile`

**Interfaces:**
- Produces:
  - `audio_ops_t`, `display_ops_t`, `buttons_ops_t`, `board_t`, `button_id_t` (in `board_types.h`)
  - `const board_t *board_active(void);`
  - `void board_set(const board_t *b);`
  - `const board_t *board_select(const board_t *const *boards, int n, const char *forced_name);`
  - `esp_err_t board_detect_and_select(void);` (declared here, defined in Task 5)
  - macro `LUGO_BOARD_REGISTER(sym)`

- [ ] **Step 1: Create `components/board/include/board_types.h`**

```c
#pragma once
#include "esp_err.h"
#include <stdint.h>
#include <stdbool.h>

// Moved here from buttons.h so the board layer can reference it without a
// circular component dependency. Values must stay 0/1/2 (main.c relies on them).
typedef enum {
    BTN_WAKE,      // wake / conversation toggle
    BTN_VOL_UP,
    BTN_VOL_DOWN,
} button_id_t;

typedef struct {
    esp_err_t (*init)(const void *cfg);
    int  (*mic_read)(int16_t *pcm, int samples);
    int  (*spk_write)(const int16_t *pcm, int samples);
    void (*spk_reset)(void);
    void (*set_volume)(int pct);
    int  (*get_volume)(void);
    int  (*adjust_volume)(int delta);
} audio_ops_t;

typedef struct {
    esp_err_t (*init)(const void *cfg);
    void (*show)(const char *line1, const char *line2);
} display_ops_t;

typedef struct {
    void (*start)(void (*on_press)(button_id_t id));
} buttons_ops_t;

typedef struct board {
    const char          *name;
    const audio_ops_t   *audio;
    const display_ops_t *display;
    const buttons_ops_t *buttons;
    // const void *net;         // RESERVED for a future 4G/other-transport board
    const void          *audio_cfg;    // driver-specific pin/config blob
    const void          *display_cfg;
    const void          *buttons_cfg;
    bool               (*match)(void); // true if firmware is running on this board
} board_t;
```

- [ ] **Step 2: Create `components/board/include/board.h`**

```c
#pragma once
#include "board_types.h"

// The board selected at boot. NULL until board_detect_and_select() succeeds.
const board_t *board_active(void);
// Force the active board. Boot path uses it via board_detect_and_select();
// host tests use it directly to install a mock board.
void           board_set(const board_t *b);
// Boot entry: gather registered boards, pick one (forced or auto-detected),
// and board_set() it. Defined in Task 5 (target-only).
esp_err_t      board_detect_and_select(void);

// Pure selection: forced_name (non-empty) picks by name; otherwise the first
// board whose match() returns true; otherwise boards[0]. NULL if forced name
// not found or n<=0.
const board_t *board_select(const board_t *const *boards, int n,
                            const char *forced_name);

// Define a board and auto-register it into the linker "board_desc" section:
//   LUGO_BOARD_REGISTER(board_my_name) { .name = "my-name", ... };
#define LUGO_BOARD_REGISTER(sym)                                          \
    static const board_t sym;                                             \
    static const board_t *const sym##_ref                                 \
        __attribute__((used, section("board_desc"))) = &sym;              \
    static const board_t sym =
```

- [ ] **Step 3: Create `components/board/board_active.c`**

```c
#include "board.h"

static const board_t *s_active;

const board_t *board_active(void) { return s_active; }
void           board_set(const board_t *b) { s_active = b; }
```

- [ ] **Step 4: Create `components/board/board_select.c`**

```c
#include "board.h"
#include <string.h>

const board_t *board_select(const board_t *const *boards, int n,
                            const char *forced_name) {
    if (n <= 0 || boards == NULL) return NULL;
    if (forced_name != NULL && forced_name[0] != '\0') {
        for (int i = 0; i < n; i++)
            if (boards[i] && boards[i]->name &&
                strcmp(boards[i]->name, forced_name) == 0)
                return boards[i];
        return NULL;  // configured board not present in the build
    }
    for (int i = 0; i < n; i++)
        if (boards[i] && boards[i]->match && boards[i]->match())
            return boards[i];
    return boards[0];  // default fallback (first registered board)
}
```

- [ ] **Step 5: Create `components/board/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "board.c" "board_active.c" "board_select.c"
    INCLUDE_DIRS "include")
```

Note: `board.c` is created in Task 5. Until then this build target is only exercised by the host tests below (which compile the `.c` files directly, not via CMake). If you run `idf.py build` before Task 5, temporarily omit `"board.c"` — but the normal flow reaches `idf.py build` at Task 5, so leave it listed.

- [ ] **Step 6: Move `button_id_t` out of `components/buttons/include/buttons.h`**

Replace the whole file with:

```c
#pragma once
#include "board_types.h"   // button_id_t

// Configures the buttons (active-low, internal pull-up) and starts a debounced
// polling task that calls on_press(id) once per press. The callback runs in the
// button task's context — keep it light (set flags, queue messages, adjust an
// int); do NOT call blocking SPI/I2S hardware directly from it.
void buttons_start(void (*on_press)(button_id_t id));
```

- [ ] **Step 7: Create `test/shims/esp_err.h`** (host stand-in for the ESP-IDF header)

```c
#pragma once
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NOT_FOUND 0x105
```

- [ ] **Step 8: Write the failing test `test/test_board_select.c`**

```c
#include "board.h"
#include <stdio.h>
#include <string.h>

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
  printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static bool yes(void) { return true; }
static bool no(void)  { return false; }

static const board_t A = { .name = "a", .match = no };
static const board_t B = { .name = "b", .match = yes };
static const board_t C = { .name = "c", .match = yes };
static const board_t *const REG[] = { &A, &B, &C };
#define N ((int)(sizeof(REG)/sizeof(REG[0])))

static void test_forced_by_name(void) {
    CHECK(board_select(REG, N, "c") == &C);
    CHECK(board_select(REG, N, "a") == &A);
}
static void test_forced_missing_is_null(void) {
    CHECK(board_select(REG, N, "zzz") == NULL);
}
static void test_auto_picks_first_match(void) {
    // forced_name NULL/empty → first board whose match() is true (B, not A)
    CHECK(board_select(REG, N, NULL) == &B);
    CHECK(board_select(REG, N, "")  == &B);
}
static void test_auto_no_match_falls_back_to_first(void) {
    static const board_t X = { .name = "x", .match = no };
    static const board_t Y = { .name = "y", .match = no };
    static const board_t *const reg2[] = { &X, &Y };
    CHECK(board_select(reg2, 2, NULL) == &X);
}
static void test_empty_registry(void) {
    CHECK(board_select(REG, 0, NULL) == NULL);
    CHECK(board_select(NULL, 3, NULL) == NULL);
}
static void test_active_set_get(void) {
    CHECK(board_active() == NULL);
    board_set(&B);
    CHECK(board_active() == &B);
}

int main(void) {
    test_forced_by_name();
    test_forced_missing_is_null();
    test_auto_picks_first_match();
    test_auto_no_match_falls_back_to_first();
    test_empty_registry();
    test_active_set_get();
    printf(failures ? "FAILED (%d)\n" : "OK\n", failures);
    return failures ? 1 : 0;
}
```

- [ ] **Step 9: Add the target to `test/Makefile`**

Add a shim include for board tests, a source var, the target, add it to the `test:` list and `clean:` list:

```makefile
BOARD_CFLAGS = -std=c11 -Wall -Wextra -g -O0 \
               -I../components/board/include -Ishims
SRC_BOARD_SEL = ../components/board/board_select.c ../components/board/board_active.c

test_board_select: test_board_select.c $(SRC_BOARD_SEL)
	$(CC) $(BOARD_CFLAGS) -o $@ $^
```

In the `.PHONY: test` recipe append `test_board_select` to the dependency list and add `./test_board_select` to the run lines. In `clean:` add `test_board_select test_board_select.dSYM`.

- [ ] **Step 10: Run the test to verify it fails first, then passes**

```bash
cd esp32-assistant/test && make test_board_select && ./test_board_select
```
Expected before Steps 3–4 exist: compile/link error (undefined `board_select`/`board_active`).
Expected after: `OK`.

- [ ] **Step 11: Commit**

```bash
cd esp32-assistant
git add components/board components/buttons/include/buttons.h test/shims test/test_board_select.c test/Makefile
git commit -m "feat(board): board abstraction core — types, active holder, selection (host-tested)"
```

---

### Task 2: Audio facade + I2S driver extraction

**Files:**
- Create: `components/audio/include/audio_i2s.h`
- Create: `components/audio/drivers/i2s_std.c` (moved from `audio.c`)
- Modify: `components/audio/audio.c` (becomes the facade)
- Modify: `components/audio/CMakeLists.txt`
- Test: `test/test_board_facades.c` (audio portion)
- Modify: `test/Makefile`

**Interfaces:**
- Consumes: `audio_ops_t`, `board_t`, `board_active()`, `board_set()` (Task 1)
- Produces: `audio_i2s_cfg_t`, `extern const audio_ops_t audio_i2s_ops;` (in `audio_i2s.h`)

- [ ] **Step 1: Create `components/audio/include/audio_i2s.h`**

```c
#pragma once
#include "board_types.h"

typedef struct {
    int mic_ws, mic_sck, mic_sd;      // INMP441 I2S RX pins
    int spk_bclk, spk_lrc, spk_din;   // MAX98357A I2S TX pins
} audio_i2s_cfg_t;

extern const audio_ops_t audio_i2s_ops;
```

- [ ] **Step 2: Create `components/audio/drivers/i2s_std.c` from the current `audio.c`**

Move the **entire current body** of `components/audio/audio.c` into this new file, then apply exactly these changes:

1. Change the includes block to:
```c
#include "audio.h"
#include "audio_i2s.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
```
2. Rename the public functions to `static` driver functions:
   - `esp_err_t audio_init(void)` → `static esp_err_t i2s_init(const void *cfg_v)`
   - `int audio_mic_read(...)` → `static int i2s_mic_read(...)`
   - `int audio_spk_write(...)` → `static int i2s_spk_write(...)`
   - `void audio_spk_reset(void)` → `static void i2s_spk_reset(void)`
   - `void audio_set_volume(int)` → `static void i2s_set_volume(int)`
   - `int audio_get_volume(void)` → `static int i2s_get_volume(void)`
   - `int audio_adjust_volume(int)` → `static int i2s_adjust_volume(int)`
3. At the top of `i2s_init`, read pins from the cfg blob and replace the six `CONFIG_AA_*` references in the gpio_cfg structs with the cfg fields:
```c
static esp_err_t i2s_init(const void *cfg_v) {
    const audio_i2s_cfg_t *c = (const audio_i2s_cfg_t *)cfg_v;
    // ... existing rx setup, but:
    //   .bclk = c->mic_sck, .ws = c->mic_ws, .din = c->mic_sd
    // ... existing tx setup, but:
    //   .bclk = c->spk_bclk, .ws = c->spk_lrc, .dout = c->spk_din
```
   (Every other line of the two `i2s_std_config_t` blocks stays byte-for-byte the same, including the 32-bit/STEREO mic and 16-bit/MONO speaker slot configs and the comments.)
4. Append the ops table at the end of the file:
```c
const audio_ops_t audio_i2s_ops = {
    .init          = i2s_init,
    .mic_read      = i2s_mic_read,
    .spk_write     = i2s_spk_write,
    .spk_reset     = i2s_spk_reset,
    .set_volume    = i2s_set_volume,
    .get_volume    = i2s_get_volume,
    .adjust_volume = i2s_adjust_volume,
};
```

- [ ] **Step 3: Replace `components/audio/audio.c` with the facade**

```c
#include "audio.h"
#include "board.h"

// Dispatch to the active board's audio driver. board_detect_and_select() must
// run (in app_main) before audio_init().
static const audio_ops_t *s_ops;

esp_err_t audio_init(void) {
    s_ops = board_active()->audio;
    return s_ops->init(board_active()->audio_cfg);
}
int  audio_mic_read(int16_t *pcm, int samples)     { return s_ops->mic_read(pcm, samples); }
int  audio_spk_write(const int16_t *pcm, int n)    { return s_ops->spk_write(pcm, n); }
void audio_spk_reset(void)                          { s_ops->spk_reset(); }
void audio_set_volume(int pct)                      { s_ops->set_volume(pct); }
int  audio_get_volume(void)                         { return s_ops->get_volume(); }
int  audio_adjust_volume(int delta)                 { return s_ops->adjust_volume(delta); }
```

- [ ] **Step 4: Update `components/audio/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "audio.c" "drivers/i2s_std.c"
    INCLUDE_DIRS "include"
    REQUIRES driver board)
```

- [ ] **Step 5: Write the failing host test `test/test_board_facades.c`** (audio dispatch)

```c
#include "board.h"
#include "audio.h"
#include <stdio.h>

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
  printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

// ---- mock audio driver ----
static int m_init_calls, m_mic_calls, m_last_vol;
static const int MOCK_MIC_SAMPLES = 7;
static esp_err_t m_init(const void *cfg) { (void)cfg; m_init_calls++; return ESP_OK; }
static int  m_mic(int16_t *p, int n) { (void)p; (void)n; m_mic_calls++; return MOCK_MIC_SAMPLES; }
static int  m_spk(const int16_t *p, int n) { (void)p; return n; }
static void m_reset(void) {}
static void m_setv(int v) { m_last_vol = v; }
static int  m_getv(void) { return m_last_vol; }
static int  m_adjv(int d) { m_last_vol += d; return m_last_vol; }
static const audio_ops_t MOCK_AUDIO = {
    .init=m_init, .mic_read=m_mic, .spk_write=m_spk, .spk_reset=m_reset,
    .set_volume=m_setv, .get_volume=m_getv, .adjust_volume=m_adjv,
};
static const board_t MOCK_BOARD = { .name="mock", .audio=&MOCK_AUDIO };

static void test_audio_facade_dispatches(void) {
    board_set(&MOCK_BOARD);
    CHECK(audio_init() == ESP_OK);
    CHECK(m_init_calls == 1);
    int16_t buf[16];
    CHECK(audio_mic_read(buf, 16) == MOCK_MIC_SAMPLES);
    CHECK(m_mic_calls == 1);
    audio_set_volume(42);
    CHECK(audio_get_volume() == 42);
    CHECK(audio_adjust_volume(-10) == 32);
}

int main(void) {
    test_audio_facade_dispatches();
    printf(failures ? "FAILED (%d)\n" : "OK\n", failures);
    return failures ? 1 : 0;
}
```

- [ ] **Step 6: Add the target to `test/Makefile`**

```makefile
FACADE_CFLAGS = -std=c11 -Wall -Wextra -g -O0 \
                -I../components/board/include -I../components/audio/include \
                -I../components/display/include -I../components/buttons/include -Ishims
SRC_FACADES = ../components/board/board_active.c ../components/audio/audio.c

test_board_facades: test_board_facades.c $(SRC_FACADES)
	$(CC) $(FACADE_CFLAGS) -o $@ $^
```
Add `test_board_facades` to the `test:` deps and `./test_board_facades` to the run lines; add `test_board_facades test_board_facades.dSYM` to `clean:`.
(`SRC_FACADES` will gain display.c/buttons.c in Tasks 3–4.)

- [ ] **Step 7: Run the facade test**

```bash
cd esp32-assistant/test && make test_board_facades && ./test_board_facades
```
Expected: `OK`.

- [ ] **Step 8: Commit**

```bash
cd esp32-assistant
git add components/audio test/test_board_facades.c test/Makefile
git commit -m "feat(audio): split into facade + i2s_std driver behind audio_ops"
```

---

### Task 3: Display facade + ST7789 driver extraction

**Files:**
- Create: `components/display/include/display_st7789.h`
- Create: `components/display/drivers/st7789.c` (moved from `display.c`)
- Modify: `components/display/display.c` (becomes the facade)
- Modify: `components/display/CMakeLists.txt`
- Modify: `test/test_board_facades.c`, `test/Makefile`

**Interfaces:**
- Consumes: `display_ops_t`, `board_active()` (Task 1)
- Produces: `display_st7789_cfg_t`, `extern const display_ops_t display_st7789_ops;`

- [ ] **Step 1: Create `components/display/include/display_st7789.h`**

```c
#pragma once
#include "board_types.h"

typedef struct {
    int sclk, mosi, dc, rst, bl;   // ST7789 SPI pins + backlight
} display_st7789_cfg_t;

extern const display_ops_t display_st7789_ops;
```

- [ ] **Step 2: Create `components/display/drivers/st7789.c` from the current `display.c`**

Move the **entire current body** of `components/display/display.c` into this file, then:

1. Add `#include "display_st7789.h"` after `#include "display.h"`.
2. Delete the five pin `#define`s (`DISP_SCLK_GPIO`, `DISP_MOSI_GPIO`, `DISP_DC_GPIO`, `DISP_RST_GPIO`, `DISP_BL_GPIO`). Keep `DISP_SPI_HOST`, `DISP_WIDTH`, `DISP_HEIGHT`, `CLEAR_CHUNK_ROWS`.
3. Rename the two public functions to statics: `display_init` → `static esp_err_t st7789_init(const void *cfg_v)`, `display_show` → `static void st7789_show(const char *line1, const char *line2)`.
4. In `st7789_init`, cast cfg and replace pin references:
```c
static esp_err_t st7789_init(const void *cfg_v) {
    const display_st7789_cfg_t *c = (const display_st7789_cfg_t *)cfg_v;
    // backlight: use c->bl in place of DISP_BL_GPIO (bl_cfg pin_bit_mask + gpio_set_level)
    // buscfg:    .mosi_io_num = c->mosi, .sclk_io_num = c->sclk
    // io_config: .dc_gpio_num = c->dc
    // panel_config: .reset_gpio_num = c->rst
```
   (Everything else — SPI mode 3, 4 MHz, `invert_color(true)`, the `clear_screen`/`draw_char`/`draw_line` helpers and the mode-3 comment — stays byte-for-byte identical.)
5. Append the ops table:
```c
const display_ops_t display_st7789_ops = {
    .init = st7789_init,
    .show = st7789_show,
};
```

- [ ] **Step 3: Replace `components/display/display.c` with the facade**

```c
#include "display.h"
#include "board.h"

static const display_ops_t *s_ops;

esp_err_t display_init(void) {
    s_ops = board_active()->display;
    return s_ops->init(board_active()->display_cfg);
}
void display_show(const char *line1, const char *line2) {
    s_ops->show(line1, line2);
}
```

- [ ] **Step 4: Update `components/display/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "display_font.c" "display.c" "drivers/st7789.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_lcd driver board)
```
(`display_font.c` stays shared and unchanged.)

- [ ] **Step 5: Extend `test/test_board_facades.c` with display dispatch**

Add a mock display and a test, and extend `MOCK_BOARD` to include it:

```c
#include "display.h"
static int d_init_calls, d_show_calls;
static const char *d_last1;
static esp_err_t d_init(const void *cfg) { (void)cfg; d_init_calls++; return ESP_OK; }
static void d_show(const char *l1, const char *l2) { (void)l2; d_show_calls++; d_last1 = l1; }
static const display_ops_t MOCK_DISPLAY = { .init=d_init, .show=d_show };
// change MOCK_BOARD to: { .name="mock", .audio=&MOCK_AUDIO, .display=&MOCK_DISPLAY }

static void test_display_facade_dispatches(void) {
    board_set(&MOCK_BOARD);
    CHECK(display_init() == ESP_OK);
    CHECK(d_init_calls == 1);
    display_show("hello", NULL);
    CHECK(d_show_calls == 1);
    CHECK(d_last1 == (const char *)"hello" || (d_last1 && d_last1[0]=='h'));
}
// call test_display_facade_dispatches() from main()
```

Add `../components/display/display.c` to `SRC_FACADES` in `test/Makefile`.

- [ ] **Step 6: Run the test**

```bash
cd esp32-assistant/test && make test_board_facades && ./test_board_facades
```
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
cd esp32-assistant
git add components/display test/test_board_facades.c test/Makefile
git commit -m "feat(display): split into facade + st7789 driver behind display_ops"
```

---

### Task 4: Buttons facade + GPIO driver extraction

**Files:**
- Create: `components/buttons/include/buttons_gpio.h`
- Create: `components/buttons/drivers/gpio_buttons.c` (moved from `buttons.c`)
- Modify: `components/buttons/buttons.c` (becomes the facade)
- Modify: `components/buttons/CMakeLists.txt`
- Modify: `test/test_board_facades.c`, `test/Makefile`

**Interfaces:**
- Consumes: `buttons_ops_t`, `button_id_t`, `board_active()` (Task 1)
- Produces: `buttons_gpio_cfg_t`, `extern const buttons_ops_t buttons_gpio_ops;`

- [ ] **Step 1: Create `components/buttons/include/buttons_gpio.h`**

```c
#pragma once
#include "board_types.h"

typedef struct {
    int wake, vol_up, vol_down;   // active-low GPIOs, one per button_id_t
} buttons_gpio_cfg_t;

extern const buttons_ops_t buttons_gpio_ops;
```

- [ ] **Step 2: Create `components/buttons/drivers/gpio_buttons.c` from the current `buttons.c`**

Move the **entire current body** of `components/buttons/buttons.c` into this file, then:

1. Change includes to:
```c
#include "buttons.h"
#include "buttons_gpio.h"
#include "board.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
```
2. Delete the three `#define BTN_*_GPIO` lines. Replace the `s_gpios` initializer so it is filled from the board's cfg at start time (it can no longer be a compile-time const):
```c
static int s_gpios[NBTN];  // filled from board cfg in gpio_buttons_start
static const button_id_t s_ids[NBTN] = { BTN_WAKE, BTN_VOL_UP, BTN_VOL_DOWN };
```
3. Rename `void buttons_start(void (*on_press)(button_id_t))` → `static void gpio_buttons_start(void (*on_press)(button_id_t))` and populate `s_gpios` at its top:
```c
static void gpio_buttons_start(void (*on_press)(button_id_t)) {
    const buttons_gpio_cfg_t *c = (const buttons_gpio_cfg_t *)board_active()->buttons_cfg;
    s_gpios[0] = c->wake; s_gpios[1] = c->vol_up; s_gpios[2] = c->vol_down;
    s_cb = on_press;
    // ... existing gpio_config loop + xTaskCreatePinnedToCore(buttons_task, ...) unchanged
}
```
   (`buttons_task`, debounce timing, and pull-up config stay identical.)
4. Append the ops table:
```c
const buttons_ops_t buttons_gpio_ops = {
    .start = gpio_buttons_start,
};
```

- [ ] **Step 3: Replace `components/buttons/buttons.c` with the facade**

```c
#include "buttons.h"
#include "board.h"

void buttons_start(void (*on_press)(button_id_t id)) {
    board_active()->buttons->start(on_press);
}
```

- [ ] **Step 4: Update `components/buttons/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "buttons.c" "drivers/gpio_buttons.c"
    INCLUDE_DIRS "include"
    REQUIRES driver board)
```

- [ ] **Step 5: Extend `test/test_board_facades.c` with buttons dispatch**

```c
#include "buttons.h"
static int b_start_calls;
static void b_start(void (*cb)(button_id_t)) { (void)cb; b_start_calls++; }
static const buttons_ops_t MOCK_BUTTONS = { .start = b_start };
// extend MOCK_BOARD: add .buttons = &MOCK_BUTTONS

static void noop_cb(button_id_t id) { (void)id; }
static void test_buttons_facade_dispatches(void) {
    board_set(&MOCK_BOARD);
    buttons_start(noop_cb);
    CHECK(b_start_calls == 1);
}
// call test_buttons_facade_dispatches() from main()
```

Add `../components/buttons/buttons.c` to `SRC_FACADES` in `test/Makefile`.

- [ ] **Step 6: Run the test**

```bash
cd esp32-assistant/test && make test_board_facades && ./test_board_facades
```
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
cd esp32-assistant
git add components/buttons test/test_board_facades.c test/Makefile
git commit -m "feat(buttons): split into facade + gpio driver behind buttons_ops"
```

---

### Task 5: Board registry, first board definition, boot wiring (on-target integration)

Brings the framework to life on hardware. Verified by `idf.py build` + an on-target parity smoke test.

**Files:**
- Create: `components/board/board.c`
- Create: `components/boards/lugo_s3_st7789/board_def.c`
- Create: `components/boards/CMakeLists.txt`
- Create: `components/boards/linker.lf`
- Modify: `main/Kconfig.projbuild` (board choice)
- Modify: `main/main.c` (one added call)
- Modify: `main/CMakeLists.txt` (REQUIRES board boards)

**Interfaces:**
- Consumes: `board_select()`, `board_set()`, `LUGO_BOARD_REGISTER` (Task 1); `audio_i2s_ops`/`audio_i2s_cfg_t` (Task 2); `display_st7789_ops`/`display_st7789_cfg_t` (Task 3); `buttons_gpio_ops`/`buttons_gpio_cfg_t` (Task 4)
- Produces: `board_detect_and_select()` definition; registered board `"lugo-s3-st7789"`

- [ ] **Step 1: Create `components/board/board.c`** (target-only registry gather + selection)

```c
#include "board.h"
#include "esp_log.h"
#include "sdkconfig.h"

// Boundary symbols of the "board_desc" section (see components/boards/linker.lf).
extern const board_t *const _board_desc_start[];
extern const board_t *const _board_desc_end[];

static const char *TAG = "board";

esp_err_t board_detect_and_select(void) {
    int n = (int)(_board_desc_end - _board_desc_start);
#ifdef CONFIG_AA_BOARD_FORCE
    const char *forced = CONFIG_AA_BOARD_NAME;   // e.g. "lugo-s3-st7789"
#else
    const char *forced = NULL;                    // auto-detect via match()
#endif
    const board_t *b = board_select(_board_desc_start, n, forced);
    if (b == NULL) {
        ESP_LOGE(TAG, "no board selected (registered=%d, forced=%s)",
                 n, forced ? forced : "auto");
        return ESP_ERR_NOT_FOUND;
    }
    board_set(b);
    ESP_LOGI(TAG, "board: %s (registered=%d)", b->name, n);
    return ESP_OK;
}
```

- [ ] **Step 2: Create `components/boards/linker.lf`** (keep the registry section under `--gc-sections`)

```
[sections]
board_desc: board_desc

[scheme]
board_desc_scheme:
    entries:
        board_desc -> flash_rodata KEEP() SURROUND(board_desc)

[mapping:board_desc_mapping]
archive: *
entries:
    * (board_desc_scheme)
```
(`SURROUND(board_desc)` generates the `_board_desc_start` / `_board_desc_end` symbols used in Step 1.)

- [ ] **Step 3: Create `components/boards/lugo_s3_st7789/board_def.c`**

```c
#include "board.h"
#include "audio_i2s.h"
#include "display_st7789.h"
#include "buttons_gpio.h"
#include "sdkconfig.h"

// Pins identical to the pre-refactor firmware.
static const audio_i2s_cfg_t audio_cfg = {
    .mic_ws  = CONFIG_AA_MIC_WS,  .mic_sck = CONFIG_AA_MIC_SCK, .mic_sd  = CONFIG_AA_MIC_SD,
    .spk_bclk = CONFIG_AA_SPK_BCLK, .spk_lrc = CONFIG_AA_SPK_LRC, .spk_din = CONFIG_AA_SPK_DIN,
};
static const display_st7789_cfg_t display_cfg = {
    .sclk = 42, .mosi = 41, .dc = 1, .rst = 2, .bl = 17,
};
static const buttons_gpio_cfg_t buttons_cfg = {
    .wake = 47, .vol_up = 40, .vol_down = 39,
};

// Only board in the build today, so it is Kconfig-forced and match() is never
// consulted. When a second board is added, replace this with an I2C probe
// (e.g. `return !i2c_probe(ES8311_ADDR);`) so a single binary can auto-detect.
static bool match(void) { return true; }

LUGO_BOARD_REGISTER(board_lugo_s3_st7789) {
    .name        = "lugo-s3-st7789",
    .audio       = &audio_i2s_ops,
    .display     = &display_st7789_ops,
    .buttons     = &buttons_gpio_ops,
    .audio_cfg   = &audio_cfg,
    .display_cfg = &display_cfg,
    .buttons_cfg = &buttons_cfg,
    .match       = match,
};
```

- [ ] **Step 4: Create `components/boards/CMakeLists.txt`**

```cmake
file(GLOB BOARD_SRCS CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/*/board_def.c")
idf_component_register(
    SRCS ${BOARD_SRCS}
    REQUIRES board audio display buttons
    LDFRAGMENTS "linker.lf")
```
(Adding a future board = drop a new `components/boards/<name>/board_def.c`; the glob picks it up on the next `idf.py build`. No edit here.)

- [ ] **Step 5: Add the board choice to `main/Kconfig.projbuild`**

Insert inside the existing `menu "Assistant configuration"` (before `endmenu`):

```
choice AA_BOARD
    prompt "Target board"
    default AA_BOARD_LUGO_S3_ST7789
    help
        Which hardware board this firmware runs on. "Auto-detect" compiles all
        boards into one binary and picks at boot via each board's match() probe.

config AA_BOARD_LUGO_S3_ST7789
    bool "Lugo S3 (ST7789 display + MAX98357A/INMP441 I2S)"
config AA_BOARD_AUTODETECT
    bool "Auto-detect (multi-board single binary)"
endchoice

config AA_BOARD_FORCE
    bool
    default y if !AA_BOARD_AUTODETECT

config AA_BOARD_NAME
    string
    default "lugo-s3-st7789" if AA_BOARD_LUGO_S3_ST7789
    default ""
```

- [ ] **Step 6: Wire `board_detect_and_select()` into `main/main.c`**

Add the include near the other component includes (top of file):
```c
#include "board.h"
```
Make it the **first** call in `app_main()`, before `display_init()`:
```c
void app_main(void) {
    ESP_LOGI(TAG, "esp32-assistant booting");
    ESP_ERROR_CHECK(board_detect_and_select());   // <-- add this line
    ESP_ERROR_CHECK(display_init());
    ESP_ERROR_CHECK(audio_init());
    // ... rest unchanged
```

- [ ] **Step 7: Update `main/CMakeLists.txt` REQUIRES**

Add `board` and `boards` to the `REQUIRES` list so the board component's API is visible and the `boards` archive (with the registered board) is linked in:
```cmake
idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES wifi lugo_protocol ws_client audio opus_codec nvs_flash provisioning display voice buttons board boards)
```

- [ ] **Step 8: Confirm all host tests still pass**

```bash
cd esp32-assistant/test && make clean && make test
```
Expected: every line prints `OK` (including `test_board_select`, `test_board_facades`).

- [ ] **Step 9: Build the firmware**

```bash
cd esp32-assistant && idf.py build
```
Expected: build succeeds. (Requires the ESP-IDF environment / `. $IDF_PATH/export.sh`.)

- [ ] **Step 10: On-target parity smoke test**

Flash and open the monitor:
```bash
cd esp32-assistant && idf.py flash monitor
```
Verify in the boot log and on hardware:
- Log line `board: lugo-s3-st7789 (registered=1)` appears before display init.
- Display shows the boot strings (`Connecting WiFi...` → `Ready` / `Press wake to talk`) exactly as before.
- Wake button connects; mic audio reaches the gateway; TTS plays on the speaker; volume buttons change loudness — i.e. behaviour is unchanged from the pre-refactor firmware.

- [ ] **Step 11: Commit**

```bash
cd esp32-assistant
git add components/board/board.c components/boards main/Kconfig.projbuild main/main.c main/CMakeLists.txt
git commit -m "feat(board): register lugo-s3-st7789, wire board_detect_and_select into boot"
```

---

## Self-Review

**1. Spec coverage:**
- Ops-struct vtables per capability → Task 1 (`board_types.h`).
- `board_t` aggregate + `board_active`/`board_set` → Task 1.
- Facade keeps `main.c` unchanged (bar one boot call) → Tasks 2–4 (facades), Task 5 Step 6.
- Drivers in `drivers/`, pins from cfg → Tasks 2–4.
- Auto-registration "add a board = one folder" → Task 1 macro + Task 5 (glob CMake, linker.lf).
- Board selection/detection (forced Kconfig + auto match) → Task 1 `board_select`, Task 5 `board.c` + Kconfig.
- Boot data flow (`board_detect_and_select` first) → Task 5 Step 6.
- Error handling (empty registry → `ESP_ERR_NOT_FOUND`, no-match → fallback) → Task 1 `board_select`, Task 5 `board.c`.
- Testing: host mock board + `board_select` contract → Tasks 1–4 host tests; on-target parity → Task 5.
- Migration order (framework + current board first; 2nd board later) → task sequencing; second board explicitly deferred.
- `net_ops` reserved, not built → `board_t` comment in Task 1. ✓ covered.

**2. Placeholder scan:** No TBD/TODO; every code step shows real code; driver-extraction steps give exact edits against named existing code, not "similar to". ✓

**3. Type consistency:** `audio_ops_t`/`display_ops_t`/`buttons_ops_t` fields defined in Task 1 match the ops tables in Tasks 2/3/4 and the facade calls. `board_t` field names (`audio_cfg`/`display_cfg`/`buttons_cfg`/`match`) match `board_def.c` (Task 5) and the drivers' cfg reads. `_board_desc_start`/`_board_desc_end` (Task 5 `board.c`) match `SURROUND(board_desc)` (linker.lf). `board_select` signature identical across `board.h`, `board_select.c`, `board.c`, and the test. `CONFIG_AA_BOARD_FORCE`/`CONFIG_AA_BOARD_NAME` defined in Kconfig (Step 5) and consumed in `board.c` (Step 1). ✓
