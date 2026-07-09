# ESP32 Peripheral Abstraction (mic/speaker/display) + C3 Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the combined `audio_ops_t` into independent `mic_ops_t` + `speaker_ops_t` (boards mix mic/speaker/display/buttons drivers freely), and make the firmware build for ESP32-C3 alongside ESP32-S3.

**Architecture:** Four peripheral abstractions (mic, speaker, display, buttons), each an ops-struct in `board_types.h`, selected per board. The `audio.h` app facade keeps its public API and dispatches to `board->mic`/`board->speaker`. The S3 dual-I2S driver splits into `i2s_mic.c` + `i2s_speaker.c` (behaviour identical); a new `i2s_fd.c` provides both ops on one full-duplex controller for the single-I2S C3. Cross-SoC portability via split sdkconfig defaults, a core-pinning macro, and target-gated board defs.

**Tech Stack:** C11, ESP-IDF v5.x (i2s_std, esp_lcd, FreeRTOS), host tests via `cc` + `test/Makefile`.

## Global Constraints

- Language is **C** only — no C++.
- Public signatures in `audio.h`, `display.h`, `buttons.h` are UNCHANGED (`main.c`/`voice.c` must not need edits except the core-pinning macro in `main.c`).
- `button_id_t` values stay `BTN_WAKE=0, BTN_VOL_UP=1, BTN_VOL_DOWN=2`.
- **Behaviour-preserving for `lugo-s3-st7789`:** identical GPIOs (mic `CONFIG_AA_MIC_*` on I2S_NUM_0, speaker `CONFIG_AA_SPK_*` on I2S_NUM_1; display 42/41/1/2/17; buttons 47/40/39), identical I2S/SPI config (mic 32-bit STEREO, speaker 16-bit MONO, `>>11` gain, volume scaling, TX mutex, disable/enable reset), and identical task core-pinning (audio tasks on core 1).
- `audio_ops_t` is **removed entirely** — no legacy path; S3 and C3 both use `mic_ops_t`+`speaker_ops_t`.
- Host tests follow `test/Makefile` + `CHECK` with pristine output. Commit after each task.
- The C3 audio path (`i2s_fd.c`) must **compile** for esp32c3 but is **UNVERIFIED on hardware** — comment every divergence from the tested S3 path with `// C3 full-duplex — UNVERIFIED on hardware`.
- All paths relative to `esp32-assistant/`. Work on branch `feat/peripheral-abstraction-c3` (create it; do not work on `main`).
- ESP-IDF is not on PATH; for target builds run `. ~/esp/esp-idf/export.sh` first.

---

### Task 1: Split `audio_ops_t` → `mic_ops_t` + `speaker_ops_t` (types + facade + host test)

**Files:**
- Modify: `components/board/include/board_types.h`
- Modify: `components/audio/audio.c`
- Modify: `test/test_board_facades.c`

**Interfaces:**
- Produces: `mic_ops_t`, `speaker_ops_t` (in `board_types.h`); `board_t` with `.mic`/`.speaker`/`.mic_cfg`/`.speaker_cfg` (replacing `.audio`/`.audio_cfg`).
- Note: after this task the ESP-IDF build is intentionally broken (drivers/board_def still reference the removed `audio_ops_t`); it is repaired in Task 2. Host tests (which do not compile the drivers) stay green — that is this task's gate.

- [ ] **Step 1: Rewrite the mock + test in `test/test_board_facades.c`** (RED first)

Replace the `// ---- mock audio driver ----` block (the `m_*` functions and `MOCK_AUDIO`) with split mocks, and update `MOCK_BOARD`:

```c
// ---- mock mic driver ----
static int mic_init_calls, mic_read_calls;
static const int MOCK_MIC_SAMPLES = 7;
static esp_err_t mic_init(const void *cfg) { (void)cfg; mic_init_calls++; return ESP_OK; }
static int mic_read(int16_t *p, int n) { (void)p; (void)n; mic_read_calls++; return MOCK_MIC_SAMPLES; }
static const mic_ops_t MOCK_MIC = { .init = mic_init, .read = mic_read };

// ---- mock speaker driver ----
static int spk_init_calls, spk_last_vol;
static esp_err_t spk_init(const void *cfg) { (void)cfg; spk_init_calls++; return ESP_OK; }
static int  spk_write(const int16_t *p, int n) { (void)p; return n; }
static void spk_reset(void) {}
static void spk_setv(int v) { spk_last_vol = v; }
static int  spk_getv(void) { return spk_last_vol; }
static int  spk_adjv(int d) { spk_last_vol += d; return spk_last_vol; }
static const speaker_ops_t MOCK_SPEAKER = {
    .init = spk_init, .write = spk_write, .reset = spk_reset,
    .set_volume = spk_setv, .get_volume = spk_getv, .adjust_volume = spk_adjv,
};
```

Change `MOCK_BOARD` to:

```c
static const board_t MOCK_BOARD = { .name="mock", .mic=&MOCK_MIC, .speaker=&MOCK_SPEAKER,
                                    .display=&MOCK_DISPLAY, .buttons=&MOCK_BUTTONS };
```

Update `test_audio_facade_dispatches` to assert both inits ran and mic/volume dispatch:

```c
static void test_audio_facade_dispatches(void) {
    board_set(&MOCK_BOARD);
    CHECK(audio_init() == ESP_OK);
    CHECK(mic_init_calls == 1);
    CHECK(spk_init_calls == 1);
    int16_t buf[16];
    CHECK(audio_mic_read(buf, 16) == MOCK_MIC_SAMPLES);
    CHECK(mic_read_calls == 1);
    audio_set_volume(42);
    CHECK(audio_get_volume() == 42);
    CHECK(audio_adjust_volume(-10) == 32);
}
```

- [ ] **Step 2: Run the test — verify it fails to compile**

Run: `cd esp32-assistant/test && make test_board_facades`
Expected: FAIL — `mic_ops_t`/`speaker_ops_t` undeclared, `board_t` has no `.mic`.

- [ ] **Step 3: Update `components/board/include/board_types.h`**

Remove the `audio_ops_t` struct. In its place add:

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

Change the `board_t` audio fields. Replace the `const audio_ops_t *audio;` line with:

```c
    const mic_ops_t     *mic;
    const speaker_ops_t *speaker;
```

and replace `const void *audio_cfg;` with:

```c
    const void          *mic_cfg;
    const void          *speaker_cfg;
```

(Keep `name`, `display`, `buttons`, `display_cfg`, `buttons_cfg`, `match` and the `net` reserved comment.)

- [ ] **Step 4: Rewrite `components/audio/audio.c`**

```c
#include "audio.h"
#include "board.h"

// Dispatch the combined app-facing audio API to the board's independent mic and
// speaker drivers. board_detect_and_select() must run (app_main) before audio_init().
static const mic_ops_t     *s_mic;
static const speaker_ops_t *s_spk;

esp_err_t audio_init(void) {
    s_mic = board_active()->mic;
    s_spk = board_active()->speaker;
    esp_err_t err = s_mic->init(board_active()->mic_cfg);
    if (err != ESP_OK) return err;
    return s_spk->init(board_active()->speaker_cfg);
}
int  audio_mic_read(int16_t *pcm, int samples)  { return s_mic->read(pcm, samples); }
int  audio_spk_write(const int16_t *pcm, int n) { return s_spk->write(pcm, n); }
void audio_spk_reset(void)                       { s_spk->reset(); }
void audio_set_volume(int pct)                   { s_spk->set_volume(pct); }
int  audio_get_volume(void)                      { return s_spk->get_volume(); }
int  audio_adjust_volume(int delta)              { return s_spk->adjust_volume(delta); }
```

- [ ] **Step 5: Run the test — verify it passes**

Run: `cd esp32-assistant/test && make clean && make test`
Expected: all host tests PASS, `test_board_facades` prints `OK`, no warnings.

- [ ] **Step 6: Commit**

```bash
cd esp32-assistant
git add components/board/include/board_types.h components/audio/audio.c test/test_board_facades.c
git commit -m "refactor(board): split audio_ops_t into mic_ops_t + speaker_ops_t"
```

---

### Task 2: Split the I2S driver into `i2s_mic.c` + `i2s_speaker.c`; rewire S3 board (S3 build parity)

**Files:**
- Create: `components/audio/include/i2s_mic.h`
- Create: `components/audio/include/i2s_speaker.h`
- Create: `components/audio/drivers/i2s_mic.c`
- Create: `components/audio/drivers/i2s_speaker.c`
- Delete: `components/audio/drivers/i2s_std.c`
- Delete: `components/audio/include/audio_i2s.h`
- Modify: `components/audio/CMakeLists.txt`
- Modify: `components/boards/lugo_s3_st7789/board_def.c`

**Interfaces:**
- Consumes: `mic_ops_t`, `speaker_ops_t` (Task 1); `LUGO_BOARD_REGISTER` (board layer).
- Produces: `i2s_mic_ops` + `i2s_mic_cfg_t`; `i2s_speaker_ops` + `i2s_speaker_cfg_t`.

- [ ] **Step 1: Create `components/audio/include/i2s_mic.h`**

```c
#pragma once
#include "board_types.h"

typedef struct {
    int port;          // I2S port number (INMP441 RX)
    int ws, sck, sd;   // I2S LRCK, BCLK, data-in pins
} i2s_mic_cfg_t;

extern const mic_ops_t i2s_mic_ops;
```

- [ ] **Step 2: Create `components/audio/include/i2s_speaker.h`**

```c
#pragma once
#include "board_types.h"

typedef struct {
    int port;             // I2S port number (MAX98357A TX)
    int bclk, lrc, din;   // I2S BCLK, LRCK, data-out pins
} i2s_speaker_cfg_t;

extern const speaker_ops_t i2s_speaker_ops;
```

- [ ] **Step 3: Create `components/audio/drivers/i2s_mic.c`** (mic half of the old `i2s_std.c`, verbatim logic)

```c
#include "i2s_mic.h"
#include "driver/i2s_std.h"
#include "soc/soc_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

#if SOC_I2S_NUM > 1   // dedicated RX controller (dual-I2S SoCs, e.g. ESP32-S3)

static const char *TAG = "i2s_mic";
static i2s_chan_handle_t s_rx;

// Largest samples value any caller passes (mic_task reads OPUS_UP_SAMPLES == 960).
// Fixed buffer (not a VLA) to keep stack usage bounded.
#define MIC_MAX_SAMPLES 960

static esp_err_t mic_init(const void *cfg_v) {
    const i2s_mic_cfg_t *c = (const i2s_mic_cfg_t *)cfg_v;
    // INMP441 outputs 24-bit samples left-justified in a 32-bit I2S frame (it
    // always clocks 32 SCK per WS half-period), so the RX channel must run at
    // 32-bit slot width or the mic reads garbage. STEREO: the INMP441 drives
    // only the left slot (L/R tied low); mic_read keeps the left sample.
    i2s_chan_config_t rx_cc = I2S_CHANNEL_DEFAULT_CONFIG((i2s_port_t)c->port, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&rx_cc, NULL, &s_rx));
    i2s_std_config_t rx_std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED, .bclk = c->sck,
            .ws = c->ws, .dout = I2S_GPIO_UNUSED, .din = c->sd,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &rx_std));
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx));
    ESP_LOGI(TAG, "mic ready");
    return ESP_OK;
}

static int mic_read(int16_t *pcm, int samples) {
    if (samples > MIC_MAX_SAMPLES) samples = MIC_MAX_SAMPLES;
    static int32_t raw[MIC_MAX_SAMPLES * 2];   // two 32-bit slots (L,R) per sample
    size_t bytes_read = 0;
    esp_err_t err = i2s_channel_read(s_rx, raw, samples * 2 * sizeof(int32_t), &bytes_read, portMAX_DELAY);
    if (err != ESP_OK) return -1;
    int frames = (int)(bytes_read / sizeof(int32_t) / 2);
    // Keep the left slot (INMP441 delivers ~18-bit-deep, left-justified). A
    // straight >>16 leaves speech near -60 dBFS; >>11 adds ~+30 dB, clamped.
    for (int i = 0; i < frames; i++) {
        int32_t v = raw[2 * i] >> 11;
        if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
        pcm[i] = (int16_t)v;
    }
    return frames;
}

const mic_ops_t i2s_mic_ops = { .init = mic_init, .read = mic_read };

#endif // SOC_I2S_NUM > 1
```

- [ ] **Step 4: Create `components/audio/drivers/i2s_speaker.c`** (speaker half of the old `i2s_std.c`, verbatim logic)

```c
#include "i2s_speaker.h"
#include "driver/i2s_std.h"
#include "soc/soc_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#if SOC_I2S_NUM > 1   // dedicated TX controller (dual-I2S SoCs, e.g. ESP32-S3)

static const char *TAG = "i2s_speaker";
static i2s_chan_handle_t s_tx;
// Serializes the two speaker writers (spk_task + voice_play) so they don't
// interleave on the TX channel or race on the static scratch buffer below.
static SemaphoreHandle_t s_tx_mutex;

// Software output volume (0..100). MAX98357A has no hardware volume, so
// spk_write() scales samples by this before the I2S write.
static volatile int s_volume = 80;
#define SPK_SCRATCH 512

static void spk_set_volume(int pct) {
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    s_volume = pct;
}
static int spk_get_volume(void) { return s_volume; }
static int spk_adjust_volume(int delta) {
    int v = s_volume + delta;
    if (v < 0) v = 0;
    if (v > 100) v = 100;
    s_volume = v;
    return v;
}

static esp_err_t spk_init(const void *cfg_v) {
    const i2s_speaker_cfg_t *c = (const i2s_speaker_cfg_t *)cfg_v;
    // MAX98357A takes standard 16-bit I2S directly, no bit-shift on the way out.
    i2s_chan_config_t tx_cc = I2S_CHANNEL_DEFAULT_CONFIG((i2s_port_t)c->port, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&tx_cc, &s_tx, NULL));
    i2s_std_config_t tx_std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED, .bclk = c->bclk,
            .ws = c->lrc, .dout = c->din, .din = I2S_GPIO_UNUSED,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &tx_std));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx));

    s_tx_mutex = xSemaphoreCreateMutex();
    if (!s_tx_mutex) { ESP_LOGE(TAG, "mutex create failed"); return ESP_FAIL; }
    ESP_LOGI(TAG, "speaker ready");
    return ESP_OK;
}

static int spk_write(const int16_t *pcm, int samples) {
    int vol = s_volume;
    size_t total_written = 0;
    esp_err_t err = ESP_OK;
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    if (vol >= 100) {
        err = i2s_channel_write(s_tx, pcm, samples * sizeof(int16_t),
                                &total_written, portMAX_DELAY);
    } else {
        static int16_t scratch[SPK_SCRATCH];
        int off = 0;
        while (off < samples) {
            int chunk = samples - off;
            if (chunk > SPK_SCRATCH) chunk = SPK_SCRATCH;
            for (int i = 0; i < chunk; i++)
                scratch[i] = (int16_t)(((int32_t)pcm[off + i] * vol) / 100);
            size_t bw = 0;
            err = i2s_channel_write(s_tx, scratch, chunk * sizeof(int16_t),
                                    &bw, portMAX_DELAY);
            total_written += bw;
            if (err != ESP_OK) break;
            off += chunk;
        }
    }
    xSemaphoreGive(s_tx_mutex);
    if (err != ESP_OK) return -1;
    return (int)(total_written / sizeof(int16_t));
}

static void spk_reset(void) {
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    // Disable+re-enable the TX channel to discard queued DMA (barge-in).
    i2s_channel_disable(s_tx);
    i2s_channel_enable(s_tx);
    xSemaphoreGive(s_tx_mutex);
}

const speaker_ops_t i2s_speaker_ops = {
    .init = spk_init, .write = spk_write, .reset = spk_reset,
    .set_volume = spk_set_volume, .get_volume = spk_get_volume, .adjust_volume = spk_adjust_volume,
};

#endif // SOC_I2S_NUM > 1
```

- [ ] **Step 5: Delete the old combined driver and header**

```bash
cd esp32-assistant
git rm components/audio/drivers/i2s_std.c components/audio/include/audio_i2s.h
```

- [ ] **Step 6: Update `components/audio/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "audio.c" "drivers/i2s_mic.c" "drivers/i2s_speaker.c"
    INCLUDE_DIRS "include"
    REQUIRES driver board)
```

- [ ] **Step 7: Rewire `components/boards/lugo_s3_st7789/board_def.c`**

Replace the whole file with (wraps content in the S3 target guard, uses the split ops/cfgs; display + buttons cfg unchanged):

```c
#include "board.h"
#include "i2s_mic.h"
#include "i2s_speaker.h"
#include "display_st7789.h"
#include "buttons_gpio.h"
#include "sdkconfig.h"

#if CONFIG_IDF_TARGET_ESP32S3

// Pins identical to the pre-refactor firmware. Mic on I2S_NUM_0, speaker on I2S_NUM_1.
static const i2s_mic_cfg_t mic_cfg = {
    .port = 0, .ws = CONFIG_AA_MIC_WS, .sck = CONFIG_AA_MIC_SCK, .sd = CONFIG_AA_MIC_SD,
};
static const i2s_speaker_cfg_t spk_cfg = {
    .port = 1, .bclk = CONFIG_AA_SPK_BCLK, .lrc = CONFIG_AA_SPK_LRC, .din = CONFIG_AA_SPK_DIN,
};
static const display_st7789_cfg_t display_cfg = {
    .sclk = 42, .mosi = 41, .dc = 1, .rst = 2, .bl = 17,
};
static const buttons_gpio_cfg_t buttons_cfg = {
    .wake = 47, .vol_up = 40, .vol_down = 39,
};

static bool match(void) { return true; }   // Kconfig-forced; single S3 board

LUGO_BOARD_REGISTER(board_lugo_s3_st7789) {
    .name        = "lugo-s3-st7789",
    .mic         = &i2s_mic_ops,
    .speaker     = &i2s_speaker_ops,
    .display     = &display_st7789_ops,
    .buttons     = &buttons_gpio_ops,
    .mic_cfg     = &mic_cfg,
    .speaker_cfg = &spk_cfg,
    .display_cfg = &display_cfg,
    .buttons_cfg = &buttons_cfg,
    .match       = match,
};

#endif // CONFIG_IDF_TARGET_ESP32S3
```

- [ ] **Step 8: Verify host tests still pass**

Run: `cd esp32-assistant/test && make clean && make test`
Expected: all PASS, no warnings. (Host tests don't compile the drivers/board_def; they confirm Task 1's facade split is intact.)

- [ ] **Step 9: Verify the S3 firmware build (parity gate)**

```bash
. ~/esp/esp-idf/export.sh
cd esp32-assistant && idf.py set-target esp32s3 && idf.py build
```
Expected: build succeeds, no new warnings. This confirms the driver split + board rewire compile and link for S3. (Boot log parity `board: lugo-s3-st7789 (registered=1)` and audio behaviour are the user's on-hardware step.)

- [ ] **Step 10: Commit**

```bash
cd esp32-assistant
git add components/audio components/boards/lugo_s3_st7789/board_def.c
git commit -m "refactor(audio): split i2s_std into i2s_mic + i2s_speaker drivers; rewire S3 board"
```

---

### Task 3: Split sdkconfig defaults + portable task core-pinning

**Files:**
- Modify: `sdkconfig.defaults`
- Create: `sdkconfig.defaults.esp32s3`
- Create: `sdkconfig.defaults.esp32c3`
- Modify: `main/main.c`

**Interfaces:** none new (build config + macro only).

- [ ] **Step 1: Rewrite `sdkconfig.defaults`** (common, target-agnostic — remove S3-only hardware lines)

```
CONFIG_IDF_TARGET="esp32s3"
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
CONFIG_FREERTOS_HZ=1000
CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192
CONFIG_COMPILER_OPTIMIZATION_PERF=y

# Default 512 bytes is too small for real phone browsers hitting the
# provisioning captive portal (headers exceed it → HTTP 431).
CONFIG_HTTPD_MAX_REQ_HDR_LEN=2048
CONFIG_HTTPD_MAX_URI_LEN=1024

# Default 2304 overflows once on_event() calls display_show()/voice_play()
# (SPI/I2S) from the system event task.
CONFIG_ESP_SYSTEM_EVENT_TASK_STACK_SIZE=8192

CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y
```

- [ ] **Step 2: Create `sdkconfig.defaults.esp32s3`** (S3 hardware specifics moved out of the common file — keeps S3 identical)

```
CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_OCT=y
CONFIG_SPIRAM_SPEED_80M=y

# Opus encode of real speech at 160MHz/-Og exceeded the 60ms frame budget
# (IDLE1 starved → task WDT). Run at 240MHz, -O2.
CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=y
```

- [ ] **Step 3: Create `sdkconfig.defaults.esp32c3`** (C3 specifics: 4MB flash, 160MHz max, no PSRAM)

```
CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y
CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_160=y
```

- [ ] **Step 4: Add the core-pinning macro to `main/main.c`**

After the `#include` block near the top of `main.c`, add:

```c
// ESP32-C3 is single-core: core 1 does not exist. Pin audio tasks to core 1 on
// dual-core (S3, keeping them off core 0 where WiFi runs); let the scheduler
// place them on unicore targets.
#if CONFIG_FREERTOS_UNICORE
#define APP_CPU_AUDIO tskNO_AFFINITY
#else
#define APP_CPU_AUDIO 1
#endif
```

- [ ] **Step 5: Use the macro in the four audio task creations in `app_main`**

In `main/main.c`, change the last argument from `1` to `APP_CPU_AUDIO` in exactly these four calls (leave the `buttons` task at core 0):

```c
xTaskCreatePinnedToCore(status_task, "status", 8192, NULL, 4, NULL, APP_CPU_AUDIO);
...
xTaskCreatePinnedToCore(spk_task, "spk", 16384, NULL, 6, NULL, APP_CPU_AUDIO);
xTaskCreatePinnedToCore(mic_task, "mic", 40960, NULL, 5, NULL, APP_CPU_AUDIO);
xTaskCreatePinnedToCore(uplink_task, "uplink", 16384, NULL, 5, NULL, APP_CPU_AUDIO);
```

- [ ] **Step 6: Verify the S3 build is unchanged**

```bash
. ~/esp/esp-idf/export.sh
cd esp32-assistant && idf.py set-target esp32s3 && idf.py build
```
Expected: build succeeds, no new warnings. `set-target esp32s3` regenerates `sdkconfig` from the split defaults; the effective S3 config (240 MHz, SPIRAM oct/80M, 8 MB flash) is unchanged, and `APP_CPU_AUDIO == 1` (S3 is not `FREERTOS_UNICORE`) so task pinning is identical. Host tests are unaffected (`main.c` is not host-compiled), but run `cd test && make test` to confirm they still pass.

- [ ] **Step 7: Commit**

```bash
cd esp32-assistant
git add sdkconfig.defaults sdkconfig.defaults.esp32s3 sdkconfig.defaults.esp32c3 main/main.c
git commit -m "feat(build): split sdkconfig defaults per target; portable audio task core-pinning"
```

---

### Task 4: C3 full-duplex audio driver + C3 board + Kconfig option (C3 build gate)

**Files:**
- Create: `components/audio/include/i2s_fd.h`
- Create: `components/audio/drivers/i2s_fd.c`
- Modify: `components/audio/CMakeLists.txt`
- Create: `components/boards/lugo_c3_devkit/board_def.c`
- Modify: `main/Kconfig.projbuild`

**Interfaces:**
- Consumes: `mic_ops_t`, `speaker_ops_t` (Task 1); `i2s_mic_ops`/`i2s_speaker_ops` are NOT used here.
- Produces: `i2s_fd_mic_ops`, `i2s_fd_speaker_ops`, `i2s_fd_cfg_t`; board `lugo-c3-devkit`.

- [ ] **Step 1: Create `components/audio/include/i2s_fd.h`**

```c
#pragma once
#include "board_types.h"

// Full-duplex single-I2S-controller audio (e.g. ESP32-C3). RX and TX share
// BCLK+WS on one controller; both ops back onto one allocation.
typedef struct {
    int bclk, ws;        // shared bit-clock + word-select
    int mic_data;        // I2S data-in (INMP441)
    int spk_data;        // I2S data-out (MAX98357A)
} i2s_fd_cfg_t;

extern const mic_ops_t     i2s_fd_mic_ops;
extern const speaker_ops_t i2s_fd_speaker_ops;
```

- [ ] **Step 2: Create `components/audio/drivers/i2s_fd.c`**

```c
#include "i2s_fd.h"
#include "driver/i2s_std.h"
#include "soc/soc_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#if SOC_I2S_NUM == 1   // single-I2S SoCs (e.g. ESP32-C3): mic+speaker share one controller

static const char *TAG = "i2s_fd";
static i2s_chan_handle_t s_rx, s_tx;
static SemaphoreHandle_t s_tx_mutex;
static volatile int s_volume = 80;
static bool s_ready;   // guards the shared full-duplex init (both ops call it)

#define FD_MIC_MAX_SAMPLES 960
#define FD_SPK_SCRATCH 512

// Allocate the shared full-duplex controller once. Idempotent: mic->init and
// speaker->init both call it; init order does not matter.
// C3 full-duplex — UNVERIFIED on hardware.
static esp_err_t fd_ensure_init(const void *cfg_v) {
    if (s_ready) return ESP_OK;
    const i2s_fd_cfg_t *c = (const i2s_fd_cfg_t *)cfg_v;
    // RX+TX share BCLK+WS, so both use ONE uniform slot config. The INMP441
    // needs 32-bit slots, so RX and TX are both 32-bit STEREO @16kHz (differing
    // bit widths cannot share one BCLK). C3 full-duplex — UNVERIFIED on hardware.
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&cc, &s_tx, &s_rx));
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED, .bclk = c->bclk, .ws = c->ws,
            .dout = c->spk_data, .din = c->mic_data,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &std));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &std));
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx));
    s_tx_mutex = xSemaphoreCreateMutex();
    if (!s_tx_mutex) { ESP_LOGE(TAG, "mutex create failed"); return ESP_FAIL; }
    s_ready = true;
    ESP_LOGI(TAG, "i2s full-duplex ready (UNVERIFIED on hardware)");
    return ESP_OK;
}

static esp_err_t fd_mic_init(const void *cfg) { return fd_ensure_init(cfg); }
static esp_err_t fd_spk_init(const void *cfg) { return fd_ensure_init(cfg); }

static int fd_mic_read(int16_t *pcm, int samples) {
    if (samples > FD_MIC_MAX_SAMPLES) samples = FD_MIC_MAX_SAMPLES;
    static int32_t raw[FD_MIC_MAX_SAMPLES * 2];   // L,R 32-bit slots
    size_t br = 0;
    if (i2s_channel_read(s_rx, raw, samples * 2 * sizeof(int32_t), &br, portMAX_DELAY) != ESP_OK)
        return -1;
    int frames = (int)(br / sizeof(int32_t) / 2);
    for (int i = 0; i < frames; i++) {            // keep left slot, +30dB, clamp
        int32_t v = raw[2 * i] >> 11;
        if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
        pcm[i] = (int16_t)v;
    }
    return frames;
}

static void fd_set_volume(int pct) { if (pct < 0) pct = 0; if (pct > 100) pct = 100; s_volume = pct; }
static int  fd_get_volume(void) { return s_volume; }
static int  fd_adjust_volume(int d) { int v = s_volume + d; if (v < 0) v = 0; if (v > 100) v = 100; s_volume = v; return v; }

static int fd_spk_write(const int16_t *pcm, int samples) {
    // MAX98357A on a 32-bit STEREO frame: pack each 16-bit mono sample into the
    // top 16 bits of both L and R slots (volume-scaled first).
    // C3 full-duplex — UNVERIFIED on hardware.
    int vol = s_volume;
    static int32_t frame[FD_SPK_SCRATCH * 2];   // 2 slots per sample
    size_t written_frames = 0;
    esp_err_t err = ESP_OK;
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    int off = 0;
    while (off < samples) {
        int chunk = samples - off;
        if (chunk > FD_SPK_SCRATCH) chunk = FD_SPK_SCRATCH;
        for (int i = 0; i < chunk; i++) {
            int32_t s = pcm[off + i];
            if (vol < 100) s = (s * vol) / 100;
            int32_t slot = s << 16;               // 16-bit PCM into 32-bit slot MSBs
            frame[2 * i] = slot; frame[2 * i + 1] = slot;   // duplicate L,R
        }
        size_t bw = 0;
        err = i2s_channel_write(s_tx, frame, chunk * 2 * sizeof(int32_t), &bw, portMAX_DELAY);
        written_frames += bw / (2 * sizeof(int32_t));
        if (err != ESP_OK) break;
        off += chunk;
    }
    xSemaphoreGive(s_tx_mutex);
    if (err != ESP_OK) return -1;
    return (int)written_frames;   // samples written
}

static void fd_spk_reset(void) {
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    i2s_channel_disable(s_tx);
    i2s_channel_enable(s_tx);
    xSemaphoreGive(s_tx_mutex);
}

const mic_ops_t i2s_fd_mic_ops = { .init = fd_mic_init, .read = fd_mic_read };
const speaker_ops_t i2s_fd_speaker_ops = {
    .init = fd_spk_init, .write = fd_spk_write, .reset = fd_spk_reset,
    .set_volume = fd_set_volume, .get_volume = fd_get_volume, .adjust_volume = fd_adjust_volume,
};

#endif // SOC_I2S_NUM == 1
```

- [ ] **Step 3: Add `i2s_fd.c` to `components/audio/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "audio.c" "drivers/i2s_mic.c" "drivers/i2s_speaker.c" "drivers/i2s_fd.c"
    INCLUDE_DIRS "include"
    REQUIRES driver board)
```

- [ ] **Step 4: Create `components/boards/lugo_c3_devkit/board_def.c`**

```c
#include "board.h"
#include "i2s_fd.h"
#include "display_st7789.h"
#include "buttons_gpio.h"

#if CONFIG_IDF_TARGET_ESP32C3

// PLACEHOLDER pins — no physical C3 board yet. C3 usable GPIO is 0-10 and 18-21;
// avoid strapping (2,8,9) and SPI-flash (12-17) pins. Set these to the real
// wiring when a board exists. Mic + speaker share the single full-duplex I2S.
static const i2s_fd_cfg_t fd_cfg = {
    .bclk = 4, .ws = 5, .mic_data = 6, .spk_data = 7,
};
static const display_st7789_cfg_t display_cfg = {
    .sclk = 0, .mosi = 1, .dc = 10, .rst = 18, .bl = 19,
};
static const buttons_gpio_cfg_t buttons_cfg = {
    .wake = 3, .vol_up = 20, .vol_down = 21,
};

static bool match(void) { return true; }   // Kconfig-forced; single C3 board

LUGO_BOARD_REGISTER(board_lugo_c3_devkit) {
    .name        = "lugo-c3-devkit",
    .mic         = &i2s_fd_mic_ops,
    .speaker     = &i2s_fd_speaker_ops,
    .display     = &display_st7789_ops,
    .buttons     = &buttons_gpio_ops,
    .mic_cfg     = &fd_cfg,        // both point at the shared full-duplex cfg
    .speaker_cfg = &fd_cfg,
    .display_cfg = &display_cfg,
    .buttons_cfg = &buttons_cfg,
    .match       = match,
};

#endif // CONFIG_IDF_TARGET_ESP32C3
```

- [ ] **Step 5: Replace the board `choice` in `main/Kconfig.projbuild`**

Replace the existing `choice AA_BOARD … endchoice` and the `AA_BOARD_NAME` config with the target-gated version:

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

(Leave the rest of `Kconfig.projbuild` — server host/port/profile, the `AA_MIC_*`/`AA_SPK_*` pin configs — unchanged; the S3 board still reads `CONFIG_AA_MIC_*`/`CONFIG_AA_SPK_*`.)

- [ ] **Step 6: Verify the C3 build (the key new gate)**

```bash
. ~/esp/esp-idf/export.sh
cd esp32-assistant && idf.py set-target esp32c3 && idf.py build
```
Expected: build succeeds. Watch for: unresolved `i2s_fd_*_ops`, `SOC_I2S_NUM`/target-guard mistakes, Kconfig errors, and any new warnings. A clean build proves the sdkconfig split, core-pinning macro, single-I2S full-duplex driver, target-gated board_def, and Kconfig all resolve for esp32c3. Optionally inspect the map/nm for `board_lugo_c3_devkit` to confirm registered=1.

- [ ] **Step 7: Verify the S3 build still passes (no regression)**

```bash
. ~/esp/esp-idf/export.sh
cd esp32-assistant && idf.py set-target esp32s3 && idf.py build
```
Expected: build succeeds — switching back to S3 still works and registers only `lugo-s3-st7789`.

- [ ] **Step 8: Confirm host tests still pass**

Run: `cd esp32-assistant/test && make clean && make test`
Expected: all PASS, no warnings.

- [ ] **Step 9: Commit**

```bash
cd esp32-assistant
git add components/audio/include/i2s_fd.h components/audio/drivers/i2s_fd.c \
        components/audio/CMakeLists.txt components/boards/lugo_c3_devkit/board_def.c \
        main/Kconfig.projbuild
git commit -m "feat(board): C3 full-duplex audio driver + lugo-c3-devkit board + target-gated Kconfig"
```

---

## Self-Review

**1. Spec coverage:**
- Split `audio_ops_t` → `mic_ops_t`+`speaker_ops_t`, `board_t` restructure → Task 1.
- App facade unchanged, dispatches to mic/speaker → Task 1 Step 4.
- Independent driver modules `i2s_mic`/`i2s_speaker`; delete `i2s_std` → Task 2.
- S3 board rewired to split ops, target-guarded, behaviour-preserving pins/configs → Task 2 Step 7.
- sdkconfig split (common + esp32s3 + esp32c3) → Task 3.
- Portable core-pinning macro → Task 3 Steps 4-5.
- C3 full-duplex driver exporting both ops, idempotent shared init, 32-bit stereo, mono→stereo packing, UNVERIFIED comments → Task 4 Step 2.
- C3 board_def mixing fd audio + st7789 display (proves free mixing), target-guarded, placeholder pins → Task 4 Step 4.
- Kconfig target-gated board choice → Task 4 Step 5.
- Verification: host green each task; S3 build parity (T2/T3); C3 build gate + S3 no-regression (T4). ✓

**2. Placeholder scan:** The C3 board pins are intentional, documented placeholders (spec-sanctioned); no TBD/TODO; all code steps contain complete code; driver splits show full file content, not "similar to". ✓

**3. Type consistency:** `mic_ops_t`{init,read} / `speaker_ops_t`{init,write,reset,set/get/adjust_volume} defined in Task 1 match the facade (Task 1), `i2s_mic_ops`/`i2s_speaker_ops` (Task 2), and `i2s_fd_mic_ops`/`i2s_fd_speaker_ops` (Task 4). `board_t` fields `.mic/.speaker/.mic_cfg/.speaker_cfg` used consistently in facade, both board_defs, and the mock. cfg struct field names (`i2s_mic_cfg_t.port/ws/sck/sd`, `i2s_speaker_cfg_t.port/bclk/lrc/din`, `i2s_fd_cfg_t.bclk/ws/mic_data/spk_data`) match their drivers and board_defs. `APP_CPU_AUDIO` defined and used in the four calls. `SOC_I2S_NUM > 1` (mic/speaker) vs `== 1` (fd) guards are mutually exclusive and complete. ✓
