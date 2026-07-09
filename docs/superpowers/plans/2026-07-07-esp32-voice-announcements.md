# ESP32 Voice Status Announcements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Play three pre-recorded Vietnamese voice announcements ("setting up WiFi", "connecting WiFi", "connected") at the matching points in the esp32-assistant boot/connection sequence, using audio clips already synthesized via this project's own gateway TTS.

**Architecture:** A new `voice` component embeds three pre-generated 16kHz mono PCM16 clips directly into the firmware binary via ESP-IDF's `EMBED_FILES` (no managed component, no hand-written byte arrays) and exposes a single blocking `voice_play(voice_clip_t)` that streams the embedded samples through the existing `audio_spk_write()`. A small mutex is added to the existing `audio` component so `voice_play()` can't race with `mic_task`/`spk_task`'s concurrent use of the same codec.

**Tech Stack:** ESP-IDF 5.4's `EMBED_FILES` component-registration mechanism, FreeRTOS mutex (`SemaphoreHandle_t`).

## Global Constraints

- Target chip: `esp32s3`. ESP-IDF at `~/esp/esp-idf`, source `export.sh` before any `idf.py` call.
- `esp32-assistant` is its own git repo at `/Users/lugon/code/speech-text-transformer/esp32-assistant`, branch `feat/wifi-provisioning` (already has WiFi provisioning + ST7789 display implemented and reviewed on this branch) — continue on this same branch.
- The three PCM assets already exist and are already committed (commit `06116a0`): `components/voice/assets/voice_setup.pcm`, `voice_connecting.pcm`, `voice_connected.pcm`. Do not regenerate them.
- ESP-IDF's `EMBED_FILES` generates symbols `_binary_<filename-with-dots-as-underscores>_start`/`_end` per embedded file (confirmed by inspecting `~/esp/esp-idf/tools/cmake/utilities.cmake` and `data_file_embed_asm.cmake`) — for `voice_setup.pcm`, the symbols are exactly `_binary_voice_setup_pcm_start`/`_binary_voice_setup_pcm_end`.
- No audio for the WS-error state or the transient "WiFi OK, connecting gateway..." state — only three trigger points, per spec.
- `voice_play()` is blocking (2-3.7 second real-time delay per call) — this is a deliberate, accepted simplification, not a defect to fix.
- This plan is **not host-testable** — every task here is ESP-IDF/hardware-only (embedded audio data, FreeRTOS mutex, real speaker output). There is no TDD/host-test step in any task; verification is a single on-device manual-check task at the end.
- A physical device is connected at `/dev/cu.usbmodem101` for the final task. `idf.py monitor` needs a real TTY unavailable in sandboxed shells — relay manual verification steps to the user.

---

### Task 1: add a concurrency-safety mutex to the existing `audio` component

**Files:**
- Modify: `esp32-assistant/components/audio/audio.c`

**Interfaces:**
- Produces: no new public functions — `audio_mic_read()`/`audio_spk_write()` keep their existing signatures, now internally serialized via a mutex. Later tasks (`voice.c` in Task 2) call `audio_spk_write()` exactly as before; the mutex is invisible to callers.

Not host-testable (ESP-IDF FreeRTOS/codec code). Verified on-device in Task 5 (no audible glitching when a voice announcement and mic/speaker activity could overlap).

The current `audio.c` is:

```c
#include "audio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"

static const char *TAG = "audio";
static i2s_chan_handle_t s_tx, s_rx;
static esp_codec_dev_handle_t s_dev;

static esp_err_t init_i2s(void) {
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&cc, &s_tx, &s_rx));
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = CONFIG_AA_I2S_MCLK, .bclk = CONFIG_AA_I2S_BCLK,
            .ws = CONFIG_AA_I2S_WS, .dout = CONFIG_AA_I2S_DOUT,
            .din = CONFIG_AA_I2S_DIN,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &std));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &std));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx));
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx));
    return ESP_OK;
}

esp_err_t audio_init(void) {
    i2c_master_bus_handle_t i2c;
    i2c_master_bus_config_t ic = {
        .i2c_port = I2C_NUM_0, .sda_io_num = CONFIG_AA_I2C_SDA,
        .scl_io_num = CONFIG_AA_I2C_SCL, .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7, .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&ic, &i2c));
    ESP_ERROR_CHECK(init_i2s());

    audio_codec_i2s_cfg_t di = { .port = I2S_NUM_0, .rx_handle = s_rx, .tx_handle = s_tx };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&di);
    audio_codec_i2c_cfg_t ci = { .port = I2C_NUM_0, .addr = CONFIG_AA_ES8311_ADDR, .bus_handle = i2c };
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&ci);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();
    if (!data_if || !ctrl_if || !gpio_if) {
        ESP_LOGE(TAG, "codec interface alloc failed");
        return ESP_FAIL;
    }

    es8311_codec_cfg_t es = {
        .ctrl_if = ctrl_if, .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH,
        .pa_pin = -1, .use_mclk = true,
    };
    const audio_codec_if_t *codec_if = es8311_codec_new(&es);
    if (!codec_if) { ESP_LOGE(TAG, "es8311_codec_new failed"); return ESP_FAIL; }
    esp_codec_dev_cfg_t dc = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN_OUT, .codec_if = codec_if, .data_if = data_if };
    s_dev = esp_codec_dev_new(&dc);
    if (!s_dev) { ESP_LOGE(TAG, "esp_codec_dev_new failed"); return ESP_FAIL; }

    // Single 16 kHz mono format for both capture and playback (half-duplex).
    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16, .channel = 1, .sample_rate = 16000 };
    ESP_ERROR_CHECK(esp_codec_dev_open(s_dev, &fs));
    esp_codec_dev_set_out_vol(s_dev, 80);
    esp_codec_dev_set_in_gain(s_dev, 30.0);
    ESP_LOGI(TAG, "audio ready");
    return ESP_OK;
}

int audio_mic_read(int16_t *pcm, int samples) {
    int bytes = samples * (int)sizeof(int16_t);
    return esp_codec_dev_read(s_dev, pcm, bytes) == ESP_CODEC_DEV_OK ? samples : -1;
}

int audio_spk_write(const int16_t *pcm, int samples) {
    int bytes = samples * (int)sizeof(int16_t);
    return esp_codec_dev_write(s_dev, (void *)pcm, bytes) == ESP_CODEC_DEV_OK ? samples : -1;
}
```

- [ ] **Step 1: Add the FreeRTOS semaphore include and mutex handle**

Add these two lines to the include block at the top of the file (after `#include "esp_log.h"`):

```c
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
```

Add this static right below `static esp_codec_dev_handle_t s_dev;`:

```c
static SemaphoreHandle_t s_mutex;
```

- [ ] **Step 2: Create the mutex at the end of `audio_init()`**

Change the tail of `audio_init()` from:

```c
    esp_codec_dev_set_out_vol(s_dev, 80);
    esp_codec_dev_set_in_gain(s_dev, 30.0);
    ESP_LOGI(TAG, "audio ready");
    return ESP_OK;
}
```

to:

```c
    esp_codec_dev_set_out_vol(s_dev, 80);
    esp_codec_dev_set_in_gain(s_dev, 30.0);

    s_mutex = xSemaphoreCreateMutex();
    if (!s_mutex) { ESP_LOGE(TAG, "mutex create failed"); return ESP_FAIL; }

    ESP_LOGI(TAG, "audio ready");
    return ESP_OK;
}
```

- [ ] **Step 3: Wrap `audio_mic_read()` and `audio_spk_write()` with the mutex**

Replace both functions:

```c
int audio_mic_read(int16_t *pcm, int samples) {
    int bytes = samples * (int)sizeof(int16_t);
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    int ret = esp_codec_dev_read(s_dev, pcm, bytes) == ESP_CODEC_DEV_OK ? samples : -1;
    xSemaphoreGive(s_mutex);
    return ret;
}

int audio_spk_write(const int16_t *pcm, int samples) {
    int bytes = samples * (int)sizeof(int16_t);
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    int ret = esp_codec_dev_write(s_dev, (void *)pcm, bytes) == ESP_CODEC_DEV_OK ? samples : -1;
    xSemaphoreGive(s_mutex);
    return ret;
}
```

- [ ] **Step 4: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add components/audio/audio.c
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
feat(audio): serialize codec access with a mutex

Lets voice_play() (added in a later task) safely call
audio_spk_write() concurrently with mic_task/spk_task.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `voice` component — embedded clips + `voice_play()`

**Files:**
- Create: `esp32-assistant/components/voice/include/voice.h`
- Create: `esp32-assistant/components/voice/voice.c`
- Create: `esp32-assistant/components/voice/CMakeLists.txt`

**Interfaces:**
- Consumes: `audio_spk_write()` (existing, now mutex-protected per Task 1).
- Produces: `voice_clip_t` enum (`VOICE_SETUP`, `VOICE_CONNECTING`, `VOICE_CONNECTED`); `void voice_play(voice_clip_t clip)`. Consumed by `main.c` (Task 3) and `provisioning.c` (Task 4).

Not host-testable (ESP-IDF `EMBED_FILES` + real audio hardware). Verified on-device in Task 5.

- [ ] **Step 1: Create `voice.h`**

```c
#pragma once

typedef enum {
    VOICE_SETUP,
    VOICE_CONNECTING,
    VOICE_CONNECTED,
} voice_clip_t;

// Blocking playback of a pre-recorded status announcement (16kHz mono
// PCM16), via the existing audio_spk_write(). Requires audio_init() to
// have already run. Takes as long as the clip's real-time duration
// (roughly 2-4 seconds) — call only from non-latency-sensitive contexts.
void voice_play(voice_clip_t clip);
```

- [ ] **Step 2: Create `voice.c`**

```c
#include "voice.h"
#include "audio.h"
#include <stdint.h>
#include <stddef.h>

extern const uint8_t voice_setup_pcm_start[]      asm("_binary_voice_setup_pcm_start");
extern const uint8_t voice_setup_pcm_end[]        asm("_binary_voice_setup_pcm_end");
extern const uint8_t voice_connecting_pcm_start[] asm("_binary_voice_connecting_pcm_start");
extern const uint8_t voice_connecting_pcm_end[]   asm("_binary_voice_connecting_pcm_end");
extern const uint8_t voice_connected_pcm_start[]  asm("_binary_voice_connected_pcm_start");
extern const uint8_t voice_connected_pcm_end[]    asm("_binary_voice_connected_pcm_end");

#define VOICE_CHUNK_SAMPLES 1600  // 100ms @ 16kHz mono — matches this project's
                                  // existing convention of bounded, chunked writes
                                  // rather than one large write.

static void play_pcm(const uint8_t *start, const uint8_t *end) {
    const int16_t *pcm = (const int16_t *)start;
    size_t total_samples = ((size_t)(end - start)) / sizeof(int16_t);
    size_t offset = 0;
    while (offset < total_samples) {
        size_t chunk = total_samples - offset;
        if (chunk > VOICE_CHUNK_SAMPLES) chunk = VOICE_CHUNK_SAMPLES;
        audio_spk_write(pcm + offset, (int)chunk);
        offset += chunk;
    }
}

void voice_play(voice_clip_t clip) {
    switch (clip) {
    case VOICE_SETUP:      play_pcm(voice_setup_pcm_start, voice_setup_pcm_end); break;
    case VOICE_CONNECTING: play_pcm(voice_connecting_pcm_start, voice_connecting_pcm_end); break;
    case VOICE_CONNECTED:  play_pcm(voice_connected_pcm_start, voice_connected_pcm_end); break;
    default: break;
    }
}
```

- [ ] **Step 3: Create `CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "voice.c"
    INCLUDE_DIRS "include"
    REQUIRES audio
    EMBED_FILES "assets/voice_setup.pcm" "assets/voice_connecting.pcm" "assets/voice_connected.pcm")
```

- [ ] **Step 4: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add \
  components/voice/include/voice.h components/voice/voice.c components/voice/CMakeLists.txt
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
feat(voice): add embedded status-clip playback (voice_play)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: wire `voice` into `main.c`

**Files:**
- Modify: `esp32-assistant/main/main.c`
- Modify: `esp32-assistant/main/CMakeLists.txt`

**Interfaces:**
- Consumes: `voice_play(voice_clip_t)` (Task 2).

**Important sequencing fix, not optional:** `audio_init()` currently runs AFTER the WiFi connection succeeds (it only has ever been needed for the conversation audio pipeline, which naturally starts after WiFi). But `voice_play(VOICE_CONNECTING)` needs to run BEFORE `wifi_sta_start()`, and it calls `audio_spk_write()`, which requires `audio_init()` to have already run — calling it beforehand would use an uninitialized codec handle. `audio_init()` itself has no WiFi dependency (it only sets up I2C/I2S/the ES8311 codec), so this task moves the existing `ESP_ERROR_CHECK(audio_init());` call earlier, right after `display_init()`, before anything WiFi-related. `opus_codec_init()` is NOT moved (voice playback doesn't need Opus, and moving it isn't necessary).

- [ ] **Step 1: Add the include**

Add `#include "voice.h"` to `main/main.c`, right after the existing `#include "display.h"` line.

- [ ] **Step 2: Replace `app_main`'s body**

Current `app_main`:

```c
void app_main(void) {
    ESP_LOGI(TAG, "esp32-assistant booting");
    ESP_ERROR_CHECK(display_init());

    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    wifi_cfg_t cfg;
    ESP_ERROR_CHECK(wifi_cfg_load(&cfg));

    display_show("Connecting WiFi...", NULL);
    ESP_ERROR_CHECK(wifi_sta_start(cfg.ssid, cfg.password));
    if (!wifi_sta_wait_connected(15000)) {
        ESP_LOGW(TAG, "wifi connect failed, starting provisioning portal");
        display_show("WiFi failed", "Starting setup AP...");
        provisioning_start(&cfg);  // does not return
    }

    display_show("WiFi OK", "Connecting gateway...");
    ESP_ERROR_CHECK(audio_init());
    ESP_ERROR_CHECK(opus_codec_init());

    s_pktq = xQueueCreate(16, sizeof(pkt_t *));   // ~16*60ms buffer ceiling

    wsp_config_t wcfg = {
        .host = cfg.server_host, .port = cfg.server_port,
        .secure = CONFIG_AA_SERVER_SECURE,
        .stt_engine = CONFIG_AA_STT_ENGINE, .tts_engine = CONFIG_AA_TTS_ENGINE,
        .language = CONFIG_AA_LANGUAGE, .sample_rate = 16000, .output_sample_rate = 16000,
        .profile = CONFIG_AA_PROFILE,
    };
    strncpy(s_wcfg_host, cfg.server_host, sizeof(s_wcfg_host) - 1);
    s_wcfg_port = cfg.server_port;
    ESP_ERROR_CHECK(ws_client_start(&wcfg, on_event, on_audio));

    xTaskCreatePinnedToCore(spk_task, "spk", 4096, NULL, 6, NULL, 1);
    xTaskCreatePinnedToCore(mic_task, "mic", 4096, NULL, 5, NULL, 1);
    ESP_LOGI(TAG, "running");
}
```

Replace it with:

```c
void app_main(void) {
    ESP_LOGI(TAG, "esp32-assistant booting");
    ESP_ERROR_CHECK(display_init());
    ESP_ERROR_CHECK(audio_init());  // moved earlier: voice_play() needs the codec
                                     // ready before the first status announcement,
                                     // and audio_init() has no WiFi dependency.

    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    wifi_cfg_t cfg;
    ESP_ERROR_CHECK(wifi_cfg_load(&cfg));

    display_show("Connecting WiFi...", NULL);
    voice_play(VOICE_CONNECTING);
    ESP_ERROR_CHECK(wifi_sta_start(cfg.ssid, cfg.password));
    if (!wifi_sta_wait_connected(15000)) {
        ESP_LOGW(TAG, "wifi connect failed, starting provisioning portal");
        display_show("WiFi failed", "Starting setup AP...");
        provisioning_start(&cfg);  // does not return
    }

    display_show("WiFi OK", "Connecting gateway...");
    ESP_ERROR_CHECK(opus_codec_init());

    s_pktq = xQueueCreate(16, sizeof(pkt_t *));   // ~16*60ms buffer ceiling

    wsp_config_t wcfg = {
        .host = cfg.server_host, .port = cfg.server_port,
        .secure = CONFIG_AA_SERVER_SECURE,
        .stt_engine = CONFIG_AA_STT_ENGINE, .tts_engine = CONFIG_AA_TTS_ENGINE,
        .language = CONFIG_AA_LANGUAGE, .sample_rate = 16000, .output_sample_rate = 16000,
        .profile = CONFIG_AA_PROFILE,
    };
    strncpy(s_wcfg_host, cfg.server_host, sizeof(s_wcfg_host) - 1);
    s_wcfg_port = cfg.server_port;
    ESP_ERROR_CHECK(ws_client_start(&wcfg, on_event, on_audio));

    xTaskCreatePinnedToCore(spk_task, "spk", 4096, NULL, 6, NULL, 1);
    xTaskCreatePinnedToCore(mic_task, "mic", 4096, NULL, 5, NULL, 1);
    ESP_LOGI(TAG, "running");
}
```

- [ ] **Step 3: Add `voice_play(VOICE_CONNECTED)` to `on_event`'s session-started case**

Current case in `on_event`:

```c
    case WSP_EV_SESSION_STARTED: {
        s_state = APP_LISTENING;
        ESP_LOGI(TAG, "session ready");
        char host_port[128 + 1 + 6];  // host + ':' + up to 5-digit port + NUL
        snprintf(host_port, sizeof host_port, "%s:%d", s_wcfg_host, s_wcfg_port);
        display_show("Connected", host_port);
        break;
    }
```

Replace with:

```c
    case WSP_EV_SESSION_STARTED: {
        s_state = APP_LISTENING;
        ESP_LOGI(TAG, "session ready");
        char host_port[128 + 1 + 6];  // host + ':' + up to 5-digit port + NUL
        snprintf(host_port, sizeof host_port, "%s:%d", s_wcfg_host, s_wcfg_port);
        display_show("Connected", host_port);
        voice_play(VOICE_CONNECTED);
        break;
    }
```

(Everything else in `on_event` — the other cases — is unchanged.)

- [ ] **Step 4: Update `main/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES wifi ws_protocol ws_client audio opus_codec nvs_flash provisioning display voice)
```

- [ ] **Step 5: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add main/main.c main/CMakeLists.txt
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
feat(main): play voice status announcements alongside display updates

Also moves audio_init() earlier (before WiFi) since voice_play() needs
the codec ready before the first status announcement, and audio_init()
has no WiFi dependency.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: wire `voice` into `provisioning.c`

**Files:**
- Modify: `esp32-assistant/components/provisioning/provisioning.c`
- Modify: `esp32-assistant/components/provisioning/CMakeLists.txt`

**Interfaces:**
- Consumes: `voice_play(voice_clip_t)` (Task 2). Note: by the time `provisioning_start()` runs, `audio_init()` has already run in `main.c` (Task 3 moves it before `wifi_sta_start()`, and `provisioning_start()` is only ever called after a failed `wifi_sta_start()`/`wifi_sta_wait_connected()`), so no additional init ordering concern here.

- [ ] **Step 1: Add the include**

Add `#include "voice.h"` to `components/provisioning/provisioning.c`, in the include block near the top (after `#include "display.h"`, which was added by the prior ST7789 display plan).

- [ ] **Step 2: Call `voice_play(VOICE_SETUP)` right after the display call**

The current code (added by the prior display plan) reads:

```c
    char ssid_ip[64];
    snprintf(ssid_ip, sizeof ssid_ip, "%s 192.168.9.1", ssid);
    display_show("Setup WiFi", ssid_ip);
```

Add one line right after it:

```c
    char ssid_ip[64];
    snprintf(ssid_ip, sizeof ssid_ip, "%s 192.168.9.1", ssid);
    display_show("Setup WiFi", ssid_ip);
    voice_play(VOICE_SETUP);
```

- [ ] **Step 3: Update `components/provisioning/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "provisioning_ssid.c" "provisioning_form.c" "provisioning.c"
    INCLUDE_DIRS "include"
    REQUIRES wifi esp_wifi esp_netif esp_http_server nvs_flash lwip display voice)
```

- [ ] **Step 4: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add \
  components/provisioning/provisioning.c components/provisioning/CMakeLists.txt
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
feat(provisioning): play voice announcement when entering WiFi setup mode

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: build, flash, and verify all three voice announcements on the connected device

**Files:** none (build/flash/manual verification only).

- [ ] **Step 1: Run existing host tests as a sanity check (no new tests in this plan — this just confirms nothing else broke)**

Run: `cd /Users/lugon/code/speech-text-transformer/esp32-assistant/test && make clean && make test`
Expected: `ALL PASS` for all four existing test binaries (`test_ws_protocol`, `test_provisioning_ssid`, `test_provisioning_form`, `test_display_font`) — none of this plan's tasks touch host-testable code, so this is just confirming no accidental regression.

- [ ] **Step 2: Build the firmware**

Run:
```bash
source ~/esp/esp-idf/export.sh
idf.py -C /Users/lugon/code/speech-text-transformer/esp32-assistant build
```
Expected: `Project build complete.` with no errors. If it fails with an unresolved `_binary_voice_*` symbol, check that `components/voice/CMakeLists.txt`'s `EMBED_FILES` paths are correct and that the three `.pcm` files exist at `components/voice/assets/`.

- [ ] **Step 3: Confirm the device is connected**

Run: `ls /dev/cu.usbmodem*`
Expected: `/dev/cu.usbmodem101` (adjust the port in later steps if it enumerates differently).

- [ ] **Step 4: Flash**

Run:
```bash
source ~/esp/esp-idf/export.sh
idf.py -C /Users/lugon/code/speech-text-transformer/esp32-assistant -p /dev/cu.usbmodem101 flash
```
Expected: `Hash of data verified.` for all images, ending in `Hard resetting via RTS pin... Done`. If it fails with "port is busy", check for a leftover `idf.py monitor` process (`lsof /dev/cu.usbmodem101`) and ask the user to close it.

- [ ] **Step 5: Manually verify each voice announcement with the user (cannot be automated from the sandboxed shell)**

Ask the user to confirm, listening to the device's speaker:
1. If WiFi isn't yet configured (or times out), when the device enters SoftAP/setup mode: hear "Đang cài đặt WiFi. Kết nối vào mạng Lugo." — clear, correct words, no static/garbling.
2. Right at boot (before WiFi connects, on a device with already-saved credentials): hear "Đang kết nối WiFi."
3. Once the gateway session becomes ready: hear "Đã kết nối. Sẵn sàng."
4. Ask specifically whether any clip sounds cut off, distorted, or wrong — since the audio content itself was generated via TTS earlier in this session and has not yet been verified by ear.
5. If the device happens to receive any real conversational audio very close in time to one of these announcements, confirm there's no audible glitch/overlap (this is what Task 1's mutex is protecting against) — this is a low-probability scenario to catch by chance, not something to force, so don't block on it if it doesn't come up naturally.

- [ ] **Step 6: If anything in Step 5 sounds wrong, debug with systematic-debugging**

If a clip doesn't play, plays garbled, or the wrong clip plays for a given state, invoke the `superpowers:systematic-debugging` skill rather than guessing — likely causes to investigate methodically: wrong `EMBED_FILES` path/symbol name mismatch, `audio_init()` not actually running before the first `voice_play()` call (check the moved position in `app_main`), or an actual defect in the source PCM files themselves (re-check by playing the original `.wav` files in `/Users/lugon/code/speech-text-transformer/artifacts/` on a computer, if still present, to isolate whether the problem is in the original synthesis/conversion or in the firmware playback path).
