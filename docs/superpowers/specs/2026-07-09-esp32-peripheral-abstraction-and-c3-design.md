# ESP32 Peripheral Abstraction (mic / speaker / display) + ESP32-C3 Support — Design

**Date:** 2026-07-09
**Component:** `esp32-assistant/` (ESP-IDF firmware, C)
**Status:** Approved for planning
**Builds on:** `2026-07-09-esp32-board-abstraction-design.md` (the board layer, already merged to `esp32-assistant` main @ 8cf68cd)

## Problem

The merged board-abstraction layer bundles all audio into a single
`audio_ops_t` (mic + speaker + volume) and ships one audio driver
(`i2s_std.c`, which uses **two** I2S controllers) and one display driver
(`st7789.c`). Two gaps:

1. **Mic and speaker are welded together.** A board cannot pair, say, a PDM
   mic with an I2S DAC, or an ES8311 codec's mic path with a different output
   — because there is one combined `audio_ops_t`. Mic and speaker are
   physically independent peripherals and must be independently selectable.
2. **The firmware is ESP32-S3-only.** Adding an ESP32-C3 board is a cross-SoC
   port, not just a new `board_def.c`: C3 is single-core (tasks are pinned to
   core 1), has one I2S controller (the audio driver needs two), max 160 MHz
   (config forces 240), no PSRAM (config enables SPIRAM), and GPIO 0–21 only.

## Goals

- Four independent peripheral abstractions — **mic**, **speaker**, **display**,
  **buttons** — each its own ops-struct, freely mixable per board (no fixed
  mic↔speaker or audio↔display pairing).
- Drivers are independent modules; adding a mic/speaker/display type = one new
  driver file exporting the matching ops, referenced by a board.
- The firmware **builds for `esp32c3`** (`idf.py set-target esp32c3`) with a
  registered C3 board, alongside the unchanged `esp32s3` build.
- **Behaviour-preserving for the S3 board** (`lugo-s3-st7789`): identical pins,
  identical I2S/SPI configuration, identical task core-pinning.
- The app-facing API (`audio.h`, `display.h`, `buttons.h`) is unchanged, so
  `main.c`/`voice.c` do not churn.

## Non-goals

- Verified C3 audio on hardware — no C3 board exists yet. The C3 audio path is
  written to ESP-IDF full-duplex semantics and must compile, but is explicitly
  **UNVERIFIED on hardware** until a board is available.
- Single binary spanning S3 and C3 (impossible — different SoC architectures).
- New display drivers (SSD1306 etc.) — structure supports them; not built now.
- Renaming the `display` abstraction to `lcd` — `display` is the correct
  umbrella (LCD/OLED/e-ink).

## Architecture

### 1. Four ops-structs (replaces `audio_ops_t`)

In `components/board/include/board_types.h`, **remove** `audio_ops_t` and add:

```c
typedef struct {
    esp_err_t (*init)(const void *cfg);
    int       (*read)(int16_t *pcm, int samples);   // returns frames read
} mic_ops_t;

typedef struct {
    esp_err_t (*init)(const void *cfg);
    int       (*write)(const int16_t *pcm, int samples);  // returns samples written
    void      (*reset)(void);
    void      (*set_volume)(int pct);
    int       (*get_volume)(void);
    int       (*adjust_volume)(int delta);
} speaker_ops_t;
```

`display_ops_t` and `buttons_ops_t` are unchanged.

### 2. `board_t` restructure

Replace the `audio`/`audio_cfg` fields with mic + speaker:

```c
typedef struct board {
    const char          *name;
    const mic_ops_t     *mic;
    const speaker_ops_t *speaker;
    const display_ops_t *display;
    const buttons_ops_t *buttons;
    const void          *mic_cfg;
    const void          *speaker_cfg;
    const void          *display_cfg;
    const void          *buttons_cfg;
    bool               (*match)(void);
} board_t;
```

### 3. App facade unchanged (`audio.h`)

`components/audio/audio.c` keeps the exact public functions and dispatches to
the two ops:

```c
static const mic_ops_t     *s_mic;
static const speaker_ops_t *s_spk;

esp_err_t audio_init(void) {
    s_mic = board_active()->mic;
    s_spk = board_active()->speaker;
    esp_err_t err = s_mic->init(board_active()->mic_cfg);
    if (err != ESP_OK) return err;
    return s_spk->init(board_active()->speaker_cfg);
}
int  audio_mic_read(int16_t *p, int n) { return s_mic->read(p, n); }
int  audio_spk_write(const int16_t *p, int n) { return s_spk->write(p, n); }
void audio_spk_reset(void)             { s_spk->reset(); }
void audio_set_volume(int pct)         { s_spk->set_volume(pct); }
int  audio_get_volume(void)            { return s_spk->get_volume(); }
int  audio_adjust_volume(int d)        { return s_spk->adjust_volume(d); }
```

`main.c` and `voice.c` (which call `audio_*`) are untouched.

### 4. Driver modules

Split the current `drivers/i2s_std.c` (which does both) into two independent
drivers, and add the C3 full-duplex driver:

- `components/audio/drivers/i2s_mic.c` → `i2s_mic_ops`, cfg `i2s_mic_cfg_t
  { int port, ws, sck, sd; }`. INMP441 RX code (32-bit STEREO slot, keep left
  slot, `>>11` gain) lifted verbatim from `i2s_std.c`'s mic half.
- `components/audio/drivers/i2s_speaker.c` → `i2s_speaker_ops`, cfg
  `i2s_speaker_cfg_t { int port, bclk, lrc, din; }`. MAX98357A TX code (16-bit
  MONO slot, `s_volume` scaling, TX mutex, `spk_reset` disable/enable) lifted
  verbatim from `i2s_std.c`'s speaker half. Volume state lives here.
- `components/audio/drivers/i2s_fd.c` → exports **both** `i2s_fd_mic_ops` and
  `i2s_fd_speaker_ops`, cfg `i2s_fd_cfg_t { int bclk, ws, mic_data, spk_data; }`
  (BCLK+WS shared between RX and TX). One shared full-duplex allocation on
  `I2S_NUM_0`; both ops' `init` call an idempotent internal
  `fd_ensure_init(cfg)` (guarded so the second call is a no-op). See §6.
- `components/display/drivers/st7789.c` → `display_st7789_ops` (unchanged).

Per-driver headers: `include/i2s_mic.h`, `include/i2s_speaker.h`,
`include/i2s_fd.h` (each declares its cfg struct + `extern` ops). The old
`include/audio_i2s.h` is removed.

`i2s_std.c` is deleted (its logic is split into `i2s_mic.c` + `i2s_speaker.c`).

Driver-file compile guards so each target only compiles what it can:
- `i2s_mic.c` / `i2s_speaker.c` bodies guarded `#if SOC_I2S_NUM > 1` (they
  assume separate controllers; on C3 they compile to nothing).
- `i2s_fd.c` body guarded `#if SOC_I2S_NUM == 1` (only meaningful / needed on
  single-I2S targets).

### 5. Boards mix drivers freely; each board_def is target-guarded

Because every `board_def.c` is globbed + `WHOLE_ARCHIVE`-linked on **every**
target, and a board references target-specific driver symbols, each board_def's
body is wrapped in a target guard (a physical board is inherently one SoC):

`components/boards/lugo_s3_st7789/board_def.c` — wrap existing content in
`#if CONFIG_IDF_TARGET_ESP32S3 … #endif`; change the audio wiring to the split
ops:

```c
static const i2s_mic_cfg_t     mic_cfg = { .port = 0, .ws = CONFIG_AA_MIC_WS,
    .sck = CONFIG_AA_MIC_SCK, .sd = CONFIG_AA_MIC_SD };            // I2S_NUM_0
static const i2s_speaker_cfg_t spk_cfg = { .port = 1, .bclk = CONFIG_AA_SPK_BCLK,
    .lrc = CONFIG_AA_SPK_LRC, .din = CONFIG_AA_SPK_DIN };          // I2S_NUM_1
// display_cfg / buttons_cfg unchanged (42/41/1/2/17 ; 47/40/39)
LUGO_BOARD_REGISTER(board_lugo_s3_st7789) {
    .name = "lugo-s3-st7789",
    .mic = &i2s_mic_ops,       .speaker = &i2s_speaker_ops,
    .display = &display_st7789_ops, .buttons = &buttons_gpio_ops,
    .mic_cfg = &mic_cfg, .speaker_cfg = &spk_cfg,
    .display_cfg = &display_cfg, .buttons_cfg = &buttons_cfg, .match = match,
};
```

New `components/boards/lugo_c3_devkit/board_def.c` — `#if CONFIG_IDF_TARGET_ESP32C3`;
mic + speaker both point at the shared full-duplex driver:

```c
// PLACEHOLDER pins — C3 usable GPIO 0-10,18-21; avoid strapping (2,8,9) and
// flash (12-17). Set to real wiring when hardware exists.
static const i2s_fd_cfg_t fd_cfg = { .bclk = 4, .ws = 5, .mic_data = 6, .spk_data = 7 };
static const display_st7789_cfg_t display_cfg = { .sclk = 0, .mosi = 1, .dc = 10, .rst = 18, .bl = 19 };
static const buttons_gpio_cfg_t   buttons_cfg = { .wake = 3, .vol_up = 20, .vol_down = 21 };
static bool match(void) { return true; }   // forced by Kconfig today
LUGO_BOARD_REGISTER(board_lugo_c3_devkit) {
    .name = "lugo-c3-devkit",
    .mic = &i2s_fd_mic_ops,     .speaker = &i2s_fd_speaker_ops,
    .display = &display_st7789_ops, .buttons = &buttons_gpio_ops,
    .mic_cfg = &fd_cfg, .speaker_cfg = &fd_cfg,   // both read the shared cfg
    .display_cfg = &display_cfg, .buttons_cfg = &buttons_cfg, .match = match,
};
```

This demonstrates the goal: two boards, **different mic+speaker drivers, the
same display driver** — independent mixing, no fixed pairing.

### 6. C3 full-duplex audio driver (`i2s_fd.c`) — UNVERIFIED on hardware

- One controller (`I2S_NUM_0`) allocated full-duplex:
  `i2s_new_channel(&chan_cfg, &s_tx, &s_rx)` where `chan_cfg` uses `I2S_NUM_0`.
- RX and TX share BCLK+WS, so both are configured with a **single uniform slot
  config: 32-bit, STEREO, 16 kHz** (the INMP441's strict requirement drives it;
  differing bit widths cannot share one BCLK).
- `fd_ensure_init(cfg)`: idempotent — allocates + std-inits + enables both
  channels once (guarded by a static `bool s_ready`); both `i2s_fd_mic_ops.init`
  and `i2s_fd_speaker_ops.init` call it, so init order doesn't matter.
- `i2s_fd_mic_ops.read`: identical to `i2s_mic`'s (keep left 32-bit slot, `>>11`
  gain → int16 PCM).
- `i2s_fd_speaker_ops.write`: MAX98357A on a 32-bit stereo frame — pack each
  16-bit mono sample into the top 16 bits of both L and R 32-bit slots (`(int32_t)s << 16`,
  duplicated), applying the same volume scaling first. Shares a TX mutex.
- Every divergence from the tested S3 path is commented `// C3 full-duplex —
  UNVERIFIED on hardware`.

### 7. sdkconfig split (S3 behaviour preserved)

Current `sdkconfig.defaults` mixes S3-only settings that break C3. Split:

- `sdkconfig.defaults` (common, target-agnostic): keep `CONFIG_IDF_TARGET="esp32s3"`
  (so a bare `idf.py build` still defaults to S3), `CONFIG_FREERTOS_HZ`,
  `CONFIG_ESP_MAIN_TASK_STACK_SIZE`, `CONFIG_PARTITION_TABLE_CUSTOM*`,
  `CONFIG_COMPILER_OPTIMIZATION_PERF`, `CONFIG_HTTPD_MAX_REQ_HDR_LEN`,
  `CONFIG_HTTPD_MAX_URI_LEN`, `CONFIG_ESP_SYSTEM_EVENT_TASK_STACK_SIZE`,
  `CONFIG_ESP_WIFI_ENABLE_WPA3_SAE`.
- `sdkconfig.defaults.esp32s3`: `CONFIG_ESPTOOLPY_FLASHSIZE_8MB`, `CONFIG_SPIRAM`,
  `CONFIG_SPIRAM_MODE_OCT`, `CONFIG_SPIRAM_SPEED_80M`,
  `CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240`.
- `sdkconfig.defaults.esp32c3`: `CONFIG_ESPTOOLPY_FLASHSIZE_4MB`,
  `CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_160` (no SPIRAM).

ESP-IDF auto-applies `sdkconfig.defaults` + `sdkconfig.defaults.<target>` on
`set-target`. Switching targets requires `idf.py set-target <t>` (a
`fullclean` is implied by set-target). The S3 default build is byte-for-byte
unchanged because the same settings still apply for the s3 target.

### 8. Portable task core-pinning (`main.c`)

Add near the top of `main.c`:

```c
#if CONFIG_FREERTOS_UNICORE
#define APP_CPU_AUDIO tskNO_AFFINITY   // C3 is single-core; core 1 does not exist
#else
#define APP_CPU_AUDIO 1                // S3: keep audio tasks off core 0 (WiFi)
#endif
```

Replace the core argument `1` with `APP_CPU_AUDIO` in the four audio task
creations (`status_task`, `spk_task`, `mic_task`, `uplink_task`). The buttons
task stays pinned to core 0 (valid on both). S3 behaviour is unchanged
(`CONFIG_FREERTOS_UNICORE` is not set on S3, so `APP_CPU_AUDIO == 1`).

### 9. Kconfig board choice gated by target (`main/Kconfig.projbuild`)

```
choice AA_BOARD
    prompt "Target board"
config AA_BOARD_LUGO_S3_ST7789
    bool "Lugo S3 (ST7789 + MAX98357A/INMP441 dual-I2S)"
    depends on IDF_TARGET_ESP32S3
config AA_BOARD_LUGO_C3_DEVKIT
    bool "Lugo C3 devkit (full-duplex I2S) [pins are placeholders]"
    depends on IDF_TARGET_ESP32C3
config AA_BOARD_AUTODETECT
    bool "Auto-detect (multi-board single binary)"
endchoice

config AA_BOARD_FORCE
    bool
    default y if !AA_BOARD_AUTODETECT
config AA_BOARD_NAME
    string
    default "lugo-s3-st7789" if AA_BOARD_LUGO_S3_ST7789
    default "lugo-c3-devkit" if AA_BOARD_LUGO_C3_DEVKIT
    default ""
```

Each target sees only its own board option, so the default selection is correct
per target.

## Data flow (unchanged app path)

```
app_main()
  → board_detect_and_select()      // picks board by Kconfig-forced name
  → display_init() → active->display->init(display_cfg)
  → audio_init()   → active->mic->init(mic_cfg); active->speaker->init(speaker_cfg)
  → buttons_start()→ active->buttons->start(cb)
```

On the C3 board, `mic->init` and `speaker->init` both funnel into
`fd_ensure_init()`, which allocates the shared controller once.

## Error handling

- `audio_init()` returns the first non-OK of `mic->init` / `speaker->init`;
  `ESP_ERROR_CHECK` in `app_main` still applies.
- `fd_ensure_init()` is idempotent and returns `ESP_OK` on the second call.
- Behaviour on missing ops/cfg is unchanged from the merged board layer (static
  board tables populate all fields; a NULL-cfg contract check remains deferred
  future work, tracked from the board-layer review).

## Testing strategy

- **Host tests:** `test/test_board_facades.c` is updated — the mock board now
  provides separate `mic_ops_t` + `speaker_ops_t` (instead of one `audio_ops_t`);
  `audio_init`/`audio_mic_read`/`audio_spk_*` dispatch is re-verified through the
  split ops. `board_select` tests are unaffected. All host tests stay green,
  pristine output.
- **S3 build (parity):** `idf.py set-target esp32s3 && idf.py build` succeeds;
  the S3 driver split is behaviour-preserving (same pins, same I2S configs, same
  core-pinning) — verified by build + the existing on-target smoke (still
  pending user from the board-layer work).
- **C3 build (the key new gate):** `idf.py set-target esp32c3 && idf.py build`
  succeeds — proving the sdkconfig split, core-pinning macro, single-I2S
  full-duplex driver, target-gated board_def, and Kconfig all resolve.
- **C3 on hardware:** explicitly deferred — no board. The C3 audio path is
  unverified until then.

## Migration order (behaviour-preserving first)

1. Split `audio_ops_t` → `mic_ops_t` + `speaker_ops_t` in `board_types.h`;
   update `board_t`; update the `audio.c` facade + the `test_board_facades.c`
   mock. (Host tests green.)
2. Split `i2s_std.c` → `i2s_mic.c` + `i2s_speaker.c` (verbatim halves), new
   per-driver headers, update audio `CMakeLists.txt`; rewire the S3 board_def to
   the split ops; guard S3 board_def with `#if CONFIG_IDF_TARGET_ESP32S3`.
   (S3 build parity.)
3. sdkconfig split + core-pinning macro. (S3 build parity.)
4. Add `i2s_fd.c` (C3 full-duplex, both ops), the C3 board_def, the Kconfig C3
   option. (C3 `set-target` build passes.)

## Out of scope / deferred

- C3 hardware validation; new display/codec drivers (ES8311, SSD1306, PDM mic);
  NULL-cfg contract check; single-binary S3+C3.
