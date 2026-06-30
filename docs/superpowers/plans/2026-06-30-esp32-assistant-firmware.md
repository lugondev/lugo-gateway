# ESP32-S3 Voice Firmware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A lean ESP-IDF firmware for ESP32-S3 (ES8311 codec) that holds a hands-free, duplex voice conversation with this repo's gateway over its native WebSocket protocol.

**Architecture:** Thin client — WiFi + WebSocket + Opus + I2S only; all STT/LLM/TTS run server-side. A pure-logic `ws_protocol` module (host-testable) parses gateway events and builds client messages; `wifi`, `audio` (ES8311+I2S), and `opus_codec` are ESP-IDF components; `main` runs the conversation state machine over three FreeRTOS tasks (mic capture, WS receive, speaker playback) with a half-duplex mute and a ~150 ms Opus jitter buffer.

**Tech Stack:** ESP-IDF v5.x (C), `esp_websocket_client`, bundled `cJSON`, `espressif/esp_codec_dev` (ES8311 + `i2s_std`), Opus managed component, FreeRTOS. Host tests: plain C + vendored cJSON compiled with `cc`.

## Global Constraints

- Target chip: **ESP32-S3**; SDK: **ESP-IDF v5.x**; language: **C**.
- Audio codec chip: **ES8311** (single codec: mic ADC + speaker DAC).
- Uplink audio: **Opus, 16000 Hz, mono, 60 ms = 960 samples/frame**, one Opus packet per binary WS frame.
- Downlink audio: **Opus, 24000 Hz, mono, 60 ms = 1440 samples/frame**, one Opus packet per binary WS frame.
- Gateway endpoint path: **`/v1/conversation/stream`**. Fixed query: `audio_codec=opus`, `output=audio,text`, `audio_out=opus`. Configurable query: `stt_engine` (default `whisper_mlx`), `tts_engine` (`vieneu`), `language` (`vi`), `sample_rate` (16000), `output_sample_rate` (24000).
- Turn-taking: **hands-free, server-side VAD**; half-duplex — do NOT send mic uplink while playing the reply.
- `ws_protocol` MUST NOT include any ESP-IDF header (it must compile on the host); its only dependency is cJSON.
- PCM is signed 16-bit little-endian.
- Out of scope (do not build): OLED, wake-word, OTA, web WiFi provisioning, push-to-talk.

---

### Task 1: `ws_protocol` — parse server events

**Files:**
- Create: `esp32-assistant/components/ws_protocol/include/ws_protocol.h`
- Create: `esp32-assistant/components/ws_protocol/ws_protocol.c`
- Create: `esp32-assistant/test/vendor/.gitkeep`
- Create: `esp32-assistant/test/Makefile`
- Create: `esp32-assistant/test/test_ws_protocol.c`

**Interfaces:**
- Consumes: cJSON (`cJSON_Parse`, `cJSON_GetObjectItem`, etc.).
- Produces:
  - `wsp_event_type_t` enum: `WSP_EV_UNKNOWN, WSP_EV_SESSION_STARTED, WSP_EV_SPEECH_START, WSP_EV_SPEECH_END, WSP_EV_PROCESSING, WSP_EV_USER_TRANSCRIPT, WSP_EV_RESPONSE_TEXT, WSP_EV_AUDIO_START, WSP_EV_AUDIO_END, WSP_EV_TURN_DONE, WSP_EV_ABORTED, WSP_EV_ERROR`.
  - `typedef struct { wsp_event_type_t type; int chunk_index; int frames; int sample_rate; char text[256]; } wsp_event_t;`
  - `int wsp_parse_event(const char *json, wsp_event_t *out);` — returns 0 on success, -1 on JSON parse failure. Unknown event names yield `WSP_EV_UNKNOWN` with return 0. Numeric/text fields default to 0 / "" when absent.

- [ ] **Step 1: Set up the host test harness**

Create `esp32-assistant/test/vendor/.gitkeep` (empty). Then fetch the single-file cJSON into the vendor dir (run from repo root):

```bash
cd esp32-assistant/test/vendor
curl -fsSL -o cJSON.c https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.18/cJSON.c
curl -fsSL -o cJSON.h https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.18/cJSON.h
cd -
```

Create `esp32-assistant/test/Makefile`:

```makefile
CC ?= cc
CFLAGS = -std=c11 -Wall -Wextra -g -O0 \
  -I../components/ws_protocol/include -Ivendor
SRC = ../components/ws_protocol/ws_protocol.c vendor/cJSON.c
.PHONY: test
test: test_ws_protocol
	./test_ws_protocol
test_ws_protocol: test_ws_protocol.c $(SRC)
	$(CC) $(CFLAGS) -o $@ $^
clean:
	rm -f test_ws_protocol
```

- [ ] **Step 2: Write the failing test**

Create `esp32-assistant/test/test_ws_protocol.c`:

```c
#include "ws_protocol.h"
#include <assert.h>
#include <string.h>
#include <stdio.h>

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
  printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static void test_parse_session_started(void) {
    wsp_event_t e;
    int rc = wsp_parse_event(
        "{\"event\":\"session_started\",\"output_sample_rate\":24000}", &e);
    CHECK(rc == 0);
    CHECK(e.type == WSP_EV_SESSION_STARTED);
    CHECK(e.sample_rate == 24000);
}

static void test_parse_audio_start(void) {
    wsp_event_t e;
    int rc = wsp_parse_event(
        "{\"event\":\"audio_start\",\"chunk_index\":2,\"codec\":\"opus\","
        "\"sample_rate\":24000,\"frames\":5}", &e);
    CHECK(rc == 0);
    CHECK(e.type == WSP_EV_AUDIO_START);
    CHECK(e.chunk_index == 2);
    CHECK(e.frames == 5);
    CHECK(e.sample_rate == 24000);
}

static void test_parse_user_transcript(void) {
    wsp_event_t e;
    int rc = wsp_parse_event(
        "{\"event\":\"user_transcript\",\"text\":\"xin chao\"}", &e);
    CHECK(rc == 0);
    CHECK(e.type == WSP_EV_USER_TRANSCRIPT);
    CHECK(strcmp(e.text, "xin chao") == 0);
}

static void test_parse_simple_events(void) {
    wsp_event_t e;
    struct { const char *name; wsp_event_type_t t; } cases[] = {
        {"speech_start", WSP_EV_SPEECH_START},
        {"speech_end", WSP_EV_SPEECH_END},
        {"processing", WSP_EV_PROCESSING},
        {"audio_end", WSP_EV_AUDIO_END},
        {"turn_done", WSP_EV_TURN_DONE},
        {"aborted", WSP_EV_ABORTED},
    };
    for (size_t i = 0; i < sizeof(cases)/sizeof(cases[0]); i++) {
        char buf[64];
        snprintf(buf, sizeof buf, "{\"event\":\"%s\"}", cases[i].name);
        CHECK(wsp_parse_event(buf, &e) == 0);
        CHECK(e.type == cases[i].t);
    }
}

static void test_parse_error_and_unknown(void) {
    wsp_event_t e;
    CHECK(wsp_parse_event("{\"event\":\"error\",\"message\":\"boom\"}", &e) == 0);
    CHECK(e.type == WSP_EV_ERROR);
    CHECK(strcmp(e.text, "boom") == 0);
    CHECK(wsp_parse_event("{\"event\":\"made_up\"}", &e) == 0);
    CHECK(e.type == WSP_EV_UNKNOWN);
    CHECK(wsp_parse_event("not json", &e) == -1);
}

int main(void) {
    test_parse_session_started();
    test_parse_audio_start();
    test_parse_user_transcript();
    test_parse_simple_events();
    test_parse_error_and_unknown();
    if (failures) { printf("%d FAILURES\n", failures); return 1; }
    printf("ALL PASS\n");
    return 0;
}
```

Create the header `esp32-assistant/components/ws_protocol/include/ws_protocol.h` with declarations only (no body yet):

```c
#pragma once
#include <stddef.h>
#include <stdbool.h>

typedef enum {
    WSP_EV_UNKNOWN = 0,
    WSP_EV_SESSION_STARTED,
    WSP_EV_SPEECH_START,
    WSP_EV_SPEECH_END,
    WSP_EV_PROCESSING,
    WSP_EV_USER_TRANSCRIPT,
    WSP_EV_RESPONSE_TEXT,
    WSP_EV_AUDIO_START,
    WSP_EV_AUDIO_END,
    WSP_EV_TURN_DONE,
    WSP_EV_ABORTED,
    WSP_EV_ERROR,
} wsp_event_type_t;

typedef struct {
    wsp_event_type_t type;
    int chunk_index;
    int frames;
    int sample_rate;
    char text[256];
} wsp_event_t;

// Parse one server JSON text frame. Returns 0 on success (including unknown
// event names -> WSP_EV_UNKNOWN), -1 if json is not valid JSON.
int wsp_parse_event(const char *json, wsp_event_t *out);
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd esp32-assistant/test && make test`
Expected: link error — `undefined reference to wsp_parse_event` (no implementation yet).

- [ ] **Step 4: Write minimal implementation**

Create `esp32-assistant/components/ws_protocol/ws_protocol.c`:

```c
#include "ws_protocol.h"
#include "cJSON.h"
#include <string.h>

static void copy_str(char *dst, size_t cap, const cJSON *item) {
    dst[0] = '\0';
    if (cJSON_IsString(item) && item->valuestring) {
        strncpy(dst, item->valuestring, cap - 1);
        dst[cap - 1] = '\0';
    }
}

static int get_int(const cJSON *root, const char *key) {
    const cJSON *it = cJSON_GetObjectItemCaseSensitive(root, key);
    return cJSON_IsNumber(it) ? it->valueint : 0;
}

int wsp_parse_event(const char *json, wsp_event_t *out) {
    memset(out, 0, sizeof(*out));
    cJSON *root = cJSON_Parse(json);
    if (!root) return -1;

    const cJSON *ev = cJSON_GetObjectItemCaseSensitive(root, "event");
    const char *name = cJSON_IsString(ev) ? ev->valuestring : "";

    if (!strcmp(name, "session_started")) {
        out->type = WSP_EV_SESSION_STARTED;
        out->sample_rate = get_int(root, "output_sample_rate");
    } else if (!strcmp(name, "speech_start")) {
        out->type = WSP_EV_SPEECH_START;
    } else if (!strcmp(name, "speech_end")) {
        out->type = WSP_EV_SPEECH_END;
    } else if (!strcmp(name, "processing")) {
        out->type = WSP_EV_PROCESSING;
    } else if (!strcmp(name, "user_transcript")) {
        out->type = WSP_EV_USER_TRANSCRIPT;
        copy_str(out->text, sizeof(out->text),
                 cJSON_GetObjectItemCaseSensitive(root, "text"));
    } else if (!strcmp(name, "response_text")) {
        out->type = WSP_EV_RESPONSE_TEXT;
        out->chunk_index = get_int(root, "chunk_index");
        copy_str(out->text, sizeof(out->text),
                 cJSON_GetObjectItemCaseSensitive(root, "text"));
    } else if (!strcmp(name, "audio_start")) {
        out->type = WSP_EV_AUDIO_START;
        out->chunk_index = get_int(root, "chunk_index");
        out->frames = get_int(root, "frames");
        out->sample_rate = get_int(root, "sample_rate");
    } else if (!strcmp(name, "audio_end")) {
        out->type = WSP_EV_AUDIO_END;
        out->chunk_index = get_int(root, "chunk_index");
    } else if (!strcmp(name, "turn_done")) {
        out->type = WSP_EV_TURN_DONE;
    } else if (!strcmp(name, "aborted")) {
        out->type = WSP_EV_ABORTED;
        copy_str(out->text, sizeof(out->text),
                 cJSON_GetObjectItemCaseSensitive(root, "reason"));
    } else if (!strcmp(name, "error")) {
        out->type = WSP_EV_ERROR;
        copy_str(out->text, sizeof(out->text),
                 cJSON_GetObjectItemCaseSensitive(root, "message"));
    } else {
        out->type = WSP_EV_UNKNOWN;
    }

    cJSON_Delete(root);
    return 0;
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd esp32-assistant/test && make test`
Expected: `ALL PASS`, exit 0.

- [ ] **Step 6: Commit**

```bash
git add esp32-assistant/components/ws_protocol esp32-assistant/test
git commit -m "feat(esp32): ws_protocol event parser + host test harness"
```

---

### Task 2: `ws_protocol` — build client control & text messages

**Files:**
- Modify: `esp32-assistant/components/ws_protocol/include/ws_protocol.h`
- Modify: `esp32-assistant/components/ws_protocol/ws_protocol.c`
- Modify: `esp32-assistant/test/test_ws_protocol.c`

**Interfaces:**
- Consumes: cJSON.
- Produces:
  - `int wsp_build_control(char *buf, size_t buflen, const char *type);` — writes `{"type":"<type>"}` (e.g. `flush`, `abort`, `reset`, `end`). Returns string length, or -1 if buffer too small.
  - `int wsp_build_text(char *buf, size_t buflen, const char *text);` — writes `{"type":"text","text":"<text>"}` with proper JSON escaping. Returns length or -1.

- [ ] **Step 1: Write the failing test** — append to `test_ws_protocol.c` and call from `main`:

```c
static void test_build_control(void) {
    char buf[64];
    int n = wsp_build_control(buf, sizeof buf, "flush");
    CHECK(n > 0);
    CHECK(strcmp(buf, "{\"type\":\"flush\"}") == 0);
}

static void test_build_text_escapes(void) {
    char buf[128];
    int n = wsp_build_text(buf, sizeof buf, "say \"hi\"");
    CHECK(n > 0);
    CHECK(strcmp(buf, "{\"type\":\"text\",\"text\":\"say \\\"hi\\\"\"}") == 0);
}

static void test_build_too_small(void) {
    char buf[4];
    CHECK(wsp_build_control(buf, sizeof buf, "flush") == -1);
}
```

Add to `main()` before the failures check:
```c
    test_build_control();
    test_build_text_escapes();
    test_build_too_small();
```

Add declarations to `ws_protocol.h`:
```c
int wsp_build_control(char *buf, size_t buflen, const char *type);
int wsp_build_text(char *buf, size_t buflen, const char *text);
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd esp32-assistant/test && make test`
Expected: link error — `undefined reference to wsp_build_control`.

- [ ] **Step 3: Write minimal implementation** — append to `ws_protocol.c`:

```c
#include <stdio.h>

static int emit(char *buf, size_t buflen, cJSON *root) {
    int rc = -1;
    if (cJSON_PrintPreallocated(root, buf, (int)buflen, false)) {
        rc = (int)strlen(buf);
    }
    cJSON_Delete(root);
    return rc;
}

int wsp_build_control(char *buf, size_t buflen, const char *type) {
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", type);
    return emit(buf, buflen, root);
}

int wsp_build_text(char *buf, size_t buflen, const char *text) {
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "text");
    cJSON_AddStringToObject(root, "text", text);
    return emit(buf, buflen, root);
}
```

Note: `cJSON_PrintPreallocated` with `fmt=false` emits compact JSON and returns false (→ -1) when the buffer is too small, satisfying `test_build_too_small`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd esp32-assistant/test && make test`
Expected: `ALL PASS`.

- [ ] **Step 5: Commit**

```bash
git add esp32-assistant/components/ws_protocol esp32-assistant/test
git commit -m "feat(esp32): ws_protocol client control/text message builders"
```

---

### Task 3: `ws_protocol` — build connect URI

**Files:**
- Modify: `esp32-assistant/components/ws_protocol/include/ws_protocol.h`
- Modify: `esp32-assistant/components/ws_protocol/ws_protocol.c`
- Modify: `esp32-assistant/test/test_ws_protocol.c`

**Interfaces:**
- Produces:
  - `typedef struct { const char *host; int port; bool secure; const char *stt_engine; const char *tts_engine; const char *language; int sample_rate; int output_sample_rate; } wsp_config_t;`
  - `int wsp_build_uri(char *buf, size_t buflen, const wsp_config_t *cfg);` — builds `ws://host:port/v1/conversation/stream?stt_engine=…&tts_engine=…&language=…&sample_rate=…&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=…` (`wss://` when `cfg->secure`). Returns length or -1 if truncated.

- [ ] **Step 1: Write the failing test** — append to `test_ws_protocol.c` and call from `main`:

```c
static void test_build_uri(void) {
    wsp_config_t cfg = {
        .host = "192.168.1.50", .port = 8000, .secure = false,
        .stt_engine = "whisper_mlx", .tts_engine = "vieneu",
        .language = "vi", .sample_rate = 16000, .output_sample_rate = 24000,
    };
    char buf[512];
    int n = wsp_build_uri(buf, sizeof buf, &cfg);
    CHECK(n > 0);
    CHECK(strcmp(buf,
        "ws://192.168.1.50:8000/v1/conversation/stream"
        "?stt_engine=whisper_mlx&tts_engine=vieneu&language=vi"
        "&sample_rate=16000&audio_codec=opus&output=audio,text"
        "&audio_out=opus&output_sample_rate=24000") == 0);
}

static void test_build_uri_secure(void) {
    wsp_config_t cfg = { .host = "h", .port = 443, .secure = true,
        .stt_engine = "whisper_mlx", .tts_engine = "vieneu", .language = "vi",
        .sample_rate = 16000, .output_sample_rate = 24000 };
    char buf[512];
    CHECK(wsp_build_uri(buf, sizeof buf, &cfg) > 0);
    CHECK(strncmp(buf, "wss://h:443/", 12) == 0);
}
```

Add to `main()`: `test_build_uri(); test_build_uri_secure();`

Add to `ws_protocol.h` (above the function declarations):
```c
typedef struct {
    const char *host;
    int port;
    bool secure;
    const char *stt_engine;
    const char *tts_engine;
    const char *language;
    int sample_rate;
    int output_sample_rate;
} wsp_config_t;

int wsp_build_uri(char *buf, size_t buflen, const wsp_config_t *cfg);
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd esp32-assistant/test && make test`
Expected: link error — `undefined reference to wsp_build_uri`.

- [ ] **Step 3: Write minimal implementation** — append to `ws_protocol.c`:

```c
int wsp_build_uri(char *buf, size_t buflen, const wsp_config_t *cfg) {
    int n = snprintf(buf, buflen,
        "%s://%s:%d/v1/conversation/stream"
        "?stt_engine=%s&tts_engine=%s&language=%s"
        "&sample_rate=%d&audio_codec=opus&output=audio,text"
        "&audio_out=opus&output_sample_rate=%d",
        cfg->secure ? "wss" : "ws", cfg->host, cfg->port,
        cfg->stt_engine, cfg->tts_engine, cfg->language,
        cfg->sample_rate, cfg->output_sample_rate);
    if (n < 0 || (size_t)n >= buflen) return -1;
    return n;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd esp32-assistant/test && make test`
Expected: `ALL PASS`.

- [ ] **Step 5: Commit**

```bash
git add esp32-assistant/components/ws_protocol esp32-assistant/test
git commit -m "feat(esp32): ws_protocol connect-URI builder"
```

---

### Task 4: ESP-IDF project scaffold + WiFi STA

**Files:**
- Create: `esp32-assistant/CMakeLists.txt`
- Create: `esp32-assistant/sdkconfig.defaults`
- Create: `esp32-assistant/partitions.csv`
- Create: `esp32-assistant/main/CMakeLists.txt`
- Create: `esp32-assistant/main/idf_component.yml`
- Create: `esp32-assistant/main/Kconfig.projbuild`
- Create: `esp32-assistant/main/main.c`
- Create: `esp32-assistant/components/wifi/include/wifi_sta.h`
- Create: `esp32-assistant/components/wifi/wifi_sta.c`
- Create: `esp32-assistant/components/wifi/CMakeLists.txt`
- Create: `esp32-assistant/components/ws_protocol/CMakeLists.txt`

**Interfaces:**
- Consumes: ESP-IDF `esp_wifi`, `nvs_flash`, `esp_event`.
- Produces:
  - `esp_err_t wifi_sta_start(void);` — init NVS, netif, event loop, connect to `CONFIG_AA_WIFI_SSID`/`CONFIG_AA_WIFI_PASS`, auto-reconnect on disconnect.
  - `bool wifi_sta_wait_connected(int timeout_ms);` — block until got-IP or timeout.

**Note:** This and all later tasks build/flash on hardware. Verification commands are run by the developer in an ESP-IDF environment with the board attached. The dev host (macOS, no ESP-IDF) cannot run these — do not fake a pass.

- [ ] **Step 1: Create the build files**

`esp32-assistant/CMakeLists.txt`:
```cmake
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32-assistant)
```

`esp32-assistant/sdkconfig.defaults`:
```
CONFIG_IDF_TARGET="esp32s3"
CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
CONFIG_FREERTOS_HZ=1000
CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_OCT=y
CONFIG_SPIRAM_SPEED_80M=y
CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y
```

`esp32-assistant/partitions.csv`:
```
# Name,   Type, SubType, Offset,  Size
nvs,      data, nvs,     ,        0x6000
phy_init, data, phy,     ,        0x1000
factory,  app,  factory, ,        0x300000
```

`esp32-assistant/main/CMakeLists.txt`:
```cmake
idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES wifi ws_protocol nvs_flash)
```

`esp32-assistant/main/idf_component.yml`:
```yaml
dependencies:
  idf: ">=5.1"
  espressif/esp_websocket_client: "^1.2.0"
```

`esp32-assistant/components/ws_protocol/CMakeLists.txt`:
```cmake
idf_component_register(
    SRCS "ws_protocol.c"
    INCLUDE_DIRS "include"
    REQUIRES json)
```

`esp32-assistant/components/wifi/CMakeLists.txt`:
```cmake
idf_component_register(
    SRCS "wifi_sta.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_wifi nvs_flash esp_event)
```

- [ ] **Step 2: Add Kconfig options**

`esp32-assistant/main/Kconfig.projbuild`:
```
menu "Assistant configuration"

config AA_WIFI_SSID
    string "WiFi SSID"
    default "myssid"

config AA_WIFI_PASS
    string "WiFi password"
    default "mypassword"

config AA_SERVER_HOST
    string "Gateway host (IP or domain)"
    default "192.168.1.50"

config AA_SERVER_PORT
    int "Gateway port"
    default 8000

config AA_SERVER_SECURE
    bool "Use wss:// (TLS)"
    default n

config AA_STT_ENGINE
    string "STT engine"
    default "whisper_mlx"

config AA_TTS_ENGINE
    string "TTS engine"
    default "vieneu"

config AA_LANGUAGE
    string "Language hint"
    default "vi"

endmenu
```

- [ ] **Step 3: Implement WiFi STA**

`esp32-assistant/components/wifi/include/wifi_sta.h`:
```c
#pragma once
#include "esp_err.h"
#include <stdbool.h>

esp_err_t wifi_sta_start(void);
bool wifi_sta_wait_connected(int timeout_ms);
```

`esp32-assistant/components/wifi/wifi_sta.c`:
```c
#include "wifi_sta.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include <string.h>

static const char *TAG = "wifi";
static EventGroupHandle_t s_events;
#define BIT_CONNECTED BIT0

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg; (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_events, BIT_CONNECTED);
        ESP_LOGW(TAG, "disconnected; reconnecting");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ESP_LOGI(TAG, "got IP");
        xEventGroupSetBits(s_events, BIT_CONNECTED);
    }
}

esp_err_t wifi_sta_start(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
    s_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL, NULL));

    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, CONFIG_AA_WIFI_SSID, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, CONFIG_AA_WIFI_PASS, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    return ESP_OK;
}

bool wifi_sta_wait_connected(int timeout_ms) {
    EventBits_t bits = xEventGroupWaitBits(
        s_events, BIT_CONNECTED, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms));
    return (bits & BIT_CONNECTED) != 0;
}
```

- [ ] **Step 4: Minimal main that connects and logs**

`esp32-assistant/main/main.c`:
```c
#include "wifi_sta.h"
#include "esp_log.h"

static const char *TAG = "app";

void app_main(void) {
    ESP_LOGI(TAG, "esp32-assistant booting");
    ESP_ERROR_CHECK(wifi_sta_start());
    if (wifi_sta_wait_connected(20000)) {
        ESP_LOGI(TAG, "WiFi connected");
    } else {
        ESP_LOGE(TAG, "WiFi connect timeout");
    }
}
```

- [ ] **Step 5: Build, flash, and verify on hardware** (developer runs)

```bash
cd esp32-assistant
idf.py set-target esp32s3
idf.py menuconfig   # set AA_WIFI_SSID / AA_WIFI_PASS under "Assistant configuration"
idf.py build flash monitor
```
Expected monitor output: `esp32-assistant booting`, then `got IP`, then `WiFi connected`.

- [ ] **Step 6: Commit**

```bash
git add esp32-assistant/CMakeLists.txt esp32-assistant/sdkconfig.defaults \
  esp32-assistant/partitions.csv esp32-assistant/main esp32-assistant/components/wifi \
  esp32-assistant/components/ws_protocol/CMakeLists.txt
git commit -m "feat(esp32): project scaffold + WiFi STA connect"
```

---

### Task 5: `opus_codec` component

**Files:**
- Create: `esp32-assistant/components/opus_codec/include/opus_codec.h`
- Create: `esp32-assistant/components/opus_codec/opus_codec.c`
- Create: `esp32-assistant/components/opus_codec/CMakeLists.txt`
- Create: `esp32-assistant/components/opus_codec/idf_component.yml`

**Interfaces:**
- Consumes: Opus managed component (encoder/decoder C API).
- Produces:
  - `#define OPUS_UP_RATE 16000`, `OPUS_DOWN_RATE 24000`, `OPUS_FRAME_MS 60`, `OPUS_UP_SAMPLES 960`, `OPUS_DOWN_SAMPLES 1440`, `OPUS_MAX_PACKET 1500`.
  - `esp_err_t opus_codec_init(void);` — create a 16k mono encoder and a 24k mono decoder.
  - `int opus_codec_encode(const int16_t *pcm960, uint8_t *out, int out_cap);` — encode one 60 ms uplink frame; returns packet bytes or -1.
  - `int opus_codec_decode(const uint8_t *pkt, int pkt_len, int16_t *pcm1440);` — decode one downlink packet into 1440 samples; returns samples decoded or -1.

- [ ] **Step 1: Resolve the Opus dependency** (developer runs)

`esp32-assistant/components/opus_codec/idf_component.yml`:
```yaml
dependencies:
  espressif/opus: "*"
```
Then from `esp32-assistant/`: `idf.py reconfigure`.
Expected: the dependency resolves and `managed_components/espressif__opus` appears.
If `espressif/opus` does not resolve, search the registry — `idf.py add-dependency "chmorgan/esp-libopus"` is the known fallback — and adjust the `#include` in Step 3 accordingly (`opus.h` is the standard header in both).

- [ ] **Step 2: CMake + header**

`esp32-assistant/components/opus_codec/CMakeLists.txt`:
```cmake
idf_component_register(
    SRCS "opus_codec.c"
    INCLUDE_DIRS "include")
```

`esp32-assistant/components/opus_codec/include/opus_codec.h`:
```c
#pragma once
#include "esp_err.h"
#include <stdint.h>

#define OPUS_UP_RATE      16000
#define OPUS_DOWN_RATE    24000
#define OPUS_FRAME_MS     60
#define OPUS_UP_SAMPLES   960    // 16000 * 0.06
#define OPUS_DOWN_SAMPLES 1440   // 24000 * 0.06
#define OPUS_MAX_PACKET   1500

esp_err_t opus_codec_init(void);
int opus_codec_encode(const int16_t *pcm960, uint8_t *out, int out_cap);
int opus_codec_decode(const uint8_t *pkt, int pkt_len, int16_t *pcm1440);
```

- [ ] **Step 3: Implementation**

`esp32-assistant/components/opus_codec/opus_codec.c`:
```c
#include "opus_codec.h"
#include "opus.h"
#include "esp_log.h"

static const char *TAG = "opus";
static OpusEncoder *s_enc;
static OpusDecoder *s_dec;

esp_err_t opus_codec_init(void) {
    int err = 0;
    s_enc = opus_encoder_create(OPUS_UP_RATE, 1, OPUS_APPLICATION_VOIP, &err);
    if (err != OPUS_OK || !s_enc) { ESP_LOGE(TAG, "enc create %d", err); return ESP_FAIL; }
    opus_encoder_ctl(s_enc, OPUS_SET_BITRATE(24000));
    opus_encoder_ctl(s_enc, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    s_dec = opus_decoder_create(OPUS_DOWN_RATE, 1, &err);
    if (err != OPUS_OK || !s_dec) { ESP_LOGE(TAG, "dec create %d", err); return ESP_FAIL; }
    return ESP_OK;
}

int opus_codec_encode(const int16_t *pcm960, uint8_t *out, int out_cap) {
    int n = opus_encode(s_enc, pcm960, OPUS_UP_SAMPLES, out, out_cap);
    return n < 0 ? -1 : n;
}

int opus_codec_decode(const uint8_t *pkt, int pkt_len, int16_t *pcm1440) {
    int n = opus_decode(s_dec, pkt, pkt_len, pcm1440, OPUS_DOWN_SAMPLES, 0);
    return n < 0 ? -1 : n;
}
```

- [ ] **Step 4: Build to verify it compiles and links** (developer runs)

Temporarily call `opus_codec_init()` from `app_main` after WiFi, then:
```bash
cd esp32-assistant && idf.py build
```
Expected: build succeeds; on flash, monitor shows no `enc create` / `dec create` error after boot. Revert the temporary call before committing (or leave it — Task 8 calls it for real).

- [ ] **Step 5: Commit**

```bash
git add esp32-assistant/components/opus_codec
git commit -m "feat(esp32): opus_codec encode/decode wrappers (16k up / 24k down)"
```

---

### Task 6: `audio` component (ES8311 + I2S)

**Files:**
- Create: `esp32-assistant/components/audio/include/audio.h`
- Create: `esp32-assistant/components/audio/audio.c`
- Create: `esp32-assistant/components/audio/CMakeLists.txt`
- Create: `esp32-assistant/components/audio/idf_component.yml`
- Modify: `esp32-assistant/main/Kconfig.projbuild`

**Interfaces:**
- Consumes: `espressif/esp_codec_dev` (ES8311 driver), ESP-IDF `driver/i2s_std.h`, `driver/i2c_master.h`.
- Produces:
  - `esp_err_t audio_init(void);` — init I2C, ES8311 (configured for 16k record / 24k playback via the same I2S full-duplex bus or two channels), and I2S; enable codec.
  - `int audio_mic_read(int16_t *pcm, int samples);` — blocking read of `samples` mono samples at 16 kHz; returns samples read.
  - `int audio_spk_write(const int16_t *pcm, int samples);` — blocking write of `samples` mono samples at 24 kHz; returns samples written.

Pins come from Kconfig (added in Step 1). Defaults below match a common ES8311 xiaozhi board; the developer overrides them in menuconfig for their board.

- [ ] **Step 1: Add pin Kconfig** — append to `esp32-assistant/main/Kconfig.projbuild` inside the existing menu:

```
config AA_I2C_SDA
    int "ES8311 I2C SDA gpio"
    default 1
config AA_I2C_SCL
    int "ES8311 I2C SCL gpio"
    default 2
config AA_I2S_MCLK
    int "I2S MCLK gpio"
    default 16
config AA_I2S_BCLK
    int "I2S BCLK gpio"
    default 9
config AA_I2S_WS
    int "I2S WS/LRCK gpio"
    default 45
config AA_I2S_DOUT
    int "I2S data out (to codec DAC) gpio"
    default 8
config AA_I2S_DIN
    int "I2S data in (from codec ADC) gpio"
    default 10
config AA_ES8311_ADDR
    hex "ES8311 I2C address"
    default 0x18
```

- [ ] **Step 2: CMake + deps**

`esp32-assistant/components/audio/CMakeLists.txt`:
```cmake
idf_component_register(
    SRCS "audio.c"
    INCLUDE_DIRS "include"
    REQUIRES driver esp_codec_dev)
```

`esp32-assistant/components/audio/idf_component.yml`:
```yaml
dependencies:
  espressif/esp_codec_dev: "^1.3.0"
```

`esp32-assistant/components/audio/include/audio.h`:
```c
#pragma once
#include "esp_err.h"
#include <stdint.h>

esp_err_t audio_init(void);
int audio_mic_read(int16_t *pcm, int samples);
int audio_spk_write(const int16_t *pcm, int samples);
```

- [ ] **Step 3: Implementation**

`esp32-assistant/components/audio/audio.c`:
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

    es8311_codec_cfg_t es = {
        .ctrl_if = ctrl_if, .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH,
        .pa_pin = -1, .use_mclk = true,
    };
    const audio_codec_if_t *codec_if = es8311_codec_new(&es);
    esp_codec_dev_cfg_t dc = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN_OUT, .codec_if = codec_if, .data_if = data_if };
    s_dev = esp_codec_dev_open(&dc);

    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16, .channel = 1, .sample_rate = 16000 };
    ESP_ERROR_CHECK(esp_codec_dev_open_input(s_dev, &fs));
    esp_codec_dev_sample_info_t fs_out = {
        .bits_per_sample = 16, .channel = 1, .sample_rate = 24000 };
    ESP_ERROR_CHECK(esp_codec_dev_open_output(s_dev, &fs_out));
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

Note: ES8311 is a single codec sharing one I2S bus. Record at 16 kHz and playback at 24 kHz on one full-duplex I2S channel is not simultaneously sample-rate-independent; in the hands-free half-duplex design the device is either capturing (LISTENING) or playing (SPEAKING), never both, so reconfiguring the codec sample rate per phase is acceptable. If your `esp_codec_dev` version rejects differing in/out rates on one device, set both to 16000 here and have Task 8 resample the 24 kHz downlink, OR open input/output at the same rate the active phase needs. Verify on hardware in Step 4 and pick the path your board+driver supports; document the choice in the README (Task 9).

- [ ] **Step 4: On-device loopback verification** (developer runs)

Temporarily, in `app_main` after WiFi: `audio_init();` then a 3-second loop reading 960 mic samples and writing them straight to the speaker. Build/flash/monitor; speak and confirm you hear yourself (proves mic ADC + speaker DAC + I2S wiring). Then remove the temporary loop.

```bash
cd esp32-assistant && idf.py build flash monitor
```
Expected: `audio ready` in log; mic audio is audible from the speaker.

- [ ] **Step 5: Commit**

```bash
git add esp32-assistant/components/audio esp32-assistant/main/Kconfig.projbuild
git commit -m "feat(esp32): ES8311 + I2S audio component (mic read / speaker write)"
```

---

### Task 7: WebSocket client wrapper

**Files:**
- Create: `esp32-assistant/components/ws_client/include/ws_client.h`
- Create: `esp32-assistant/components/ws_client/ws_client.c`
- Create: `esp32-assistant/components/ws_client/CMakeLists.txt`
- Modify: `esp32-assistant/main/CMakeLists.txt` (add `ws_client` to REQUIRES)

**Interfaces:**
- Consumes: `espressif/esp_websocket_client`, `ws_protocol` (`wsp_*`).
- Produces:
  - `typedef void (*ws_event_cb_t)(const wsp_event_t *ev);`
  - `typedef void (*ws_audio_cb_t)(const uint8_t *data, int len);`
  - `esp_err_t ws_client_start(const wsp_config_t *cfg, ws_event_cb_t on_event, ws_audio_cb_t on_audio);` — builds the URI, connects, auto-reconnects (esp_websocket_client handles backoff); routes text frames through `wsp_parse_event` → `on_event`, binary frames → `on_audio`.
  - `int ws_client_send_audio(const uint8_t *opus, int len);` — send one Opus packet as a binary frame. Returns bytes sent or -1.
  - `int ws_client_send_control(const char *type);` — send `{"type":...}` text frame.
  - `bool ws_client_connected(void);`

- [ ] **Step 1: CMake**

`esp32-assistant/components/ws_client/CMakeLists.txt`:
```cmake
idf_component_register(
    SRCS "ws_client.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_websocket_client ws_protocol)
```

`esp32-assistant/components/ws_client/include/ws_client.h`:
```c
#pragma once
#include "esp_err.h"
#include "ws_protocol.h"
#include <stdbool.h>
#include <stdint.h>

typedef void (*ws_event_cb_t)(const wsp_event_t *ev);
typedef void (*ws_audio_cb_t)(const uint8_t *data, int len);

esp_err_t ws_client_start(const wsp_config_t *cfg,
                          ws_event_cb_t on_event, ws_audio_cb_t on_audio);
int ws_client_send_audio(const uint8_t *opus, int len);
int ws_client_send_control(const char *type);
bool ws_client_connected(void);
```

- [ ] **Step 2: Implementation**

`esp32-assistant/components/ws_client/ws_client.c`:
```c
#include "ws_client.h"
#include "esp_websocket_client.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "ws";
static esp_websocket_client_handle_t s_client;
static ws_event_cb_t s_on_event;
static ws_audio_cb_t s_on_audio;
static volatile bool s_connected;

static void on_ws(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg; (void)base;
    esp_websocket_event_data_t *d = data;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        s_connected = true; ESP_LOGI(TAG, "connected"); break;
    case WEBSOCKET_EVENT_DISCONNECTED:
        s_connected = false; ESP_LOGW(TAG, "disconnected"); break;
    case WEBSOCKET_EVENT_DATA:
        if (d->op_code == 0x02) {            // binary
            if (s_on_audio && d->data_len > 0)
                s_on_audio((const uint8_t *)d->data_ptr, d->data_len);
        } else if (d->op_code == 0x01) {     // text
            char buf[512];
            int n = d->data_len < (int)sizeof(buf) - 1 ? d->data_len : (int)sizeof(buf) - 1;
            memcpy(buf, d->data_ptr, n); buf[n] = '\0';
            wsp_event_t ev;
            if (wsp_parse_event(buf, &ev) == 0 && s_on_event) s_on_event(&ev);
        }
        break;
    default: break;
    }
}

esp_err_t ws_client_start(const wsp_config_t *cfg,
                          ws_event_cb_t on_event, ws_audio_cb_t on_audio) {
    s_on_event = on_event; s_on_audio = on_audio;
    static char uri[512];
    if (wsp_build_uri(uri, sizeof uri, cfg) < 0) return ESP_FAIL;
    ESP_LOGI(TAG, "uri=%s", uri);
    esp_websocket_client_config_t wc = {
        .uri = uri, .reconnect_timeout_ms = 2000, .network_timeout_ms = 10000,
        .buffer_size = 2048,
    };
    s_client = esp_websocket_client_init(&wc);
    esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY, on_ws, NULL);
    return esp_websocket_client_start(s_client);
}

int ws_client_send_audio(const uint8_t *opus, int len) {
    if (!s_connected) return -1;
    return esp_websocket_client_send_bin(s_client, (const char *)opus, len, portMAX_DELAY);
}

int ws_client_send_control(const char *type) {
    if (!s_connected) return -1;
    char buf[64];
    int n = wsp_build_control(buf, sizeof buf, type);
    if (n < 0) return -1;
    return esp_websocket_client_send_text(s_client, buf, n, portMAX_DELAY);
}

bool ws_client_connected(void) { return s_connected; }
```

- [ ] **Step 3: Wire a temporary smoke test into main** (developer runs)

Add `ws_client` to `main/CMakeLists.txt` REQUIRES. In `app_main`, after WiFi, build a `wsp_config_t` from Kconfig and call `ws_client_start` with simple callbacks that `ESP_LOGI` each event type and each binary frame length:

```c
#include "ws_client.h"
static void log_event(const wsp_event_t *ev) { ESP_LOGI("evt", "type=%d text=%s", ev->type, ev->text); }
static void log_audio(const uint8_t *d, int n) { (void)d; ESP_LOGI("aud", "%d bytes", n); }
// in app_main after WiFi connected:
wsp_config_t cfg = {
    .host = CONFIG_AA_SERVER_HOST, .port = CONFIG_AA_SERVER_PORT,
    .secure = CONFIG_AA_SERVER_SECURE,
    .stt_engine = CONFIG_AA_STT_ENGINE, .tts_engine = CONFIG_AA_TTS_ENGINE,
    .language = CONFIG_AA_LANGUAGE, .sample_rate = 16000, .output_sample_rate = 24000,
};
ws_client_start(&cfg, log_event, log_audio);
```

- [ ] **Step 4: Verify against the gateway** (developer runs)

Start the gateway (`make dev` in repo root). Build/flash/monitor the board.
```bash
cd esp32-assistant && idf.py build flash monitor
```
Expected: `connected`, then `type=1` (WSP_EV_SESSION_STARTED) logged from the `session_started` frame. Leave the temporary callbacks; Task 8 replaces them.

- [ ] **Step 5: Commit**

```bash
git add esp32-assistant/components/ws_client esp32-assistant/main/CMakeLists.txt
git commit -m "feat(esp32): WebSocket client wrapper (events + audio frames)"
```

---

### Task 8: Conversation state machine + jitter buffer (end-to-end)

**Files:**
- Modify: `esp32-assistant/main/main.c`
- Modify: `esp32-assistant/main/CMakeLists.txt` (REQUIRES: add `audio opus_codec`)

**Interfaces:**
- Consumes: `wifi_sta`, `ws_client`, `audio`, `opus_codec`, `ws_protocol`.
- Produces: the running firmware. Internal state enum `APP_CONNECTING, APP_LISTENING, APP_SPEAKING`; a FreeRTOS-queue jitter buffer of Opus packets.

- [ ] **Step 1: Implement the full app in main.c**

Replace `esp32-assistant/main/main.c` with:
```c
#include "wifi_sta.h"
#include "ws_client.h"
#include "audio.h"
#include "opus_codec.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include <string.h>

static const char *TAG = "app";

typedef enum { APP_CONNECTING, APP_LISTENING, APP_SPEAKING } app_state_t;
static volatile app_state_t s_state = APP_CONNECTING;

// jitter buffer: queue of heap-allocated Opus packets
typedef struct { uint8_t data[OPUS_MAX_PACKET]; int len; } pkt_t;
static QueueHandle_t s_pktq;   // holds pkt_t* ; depth ~ 150 ms / 60 ms ≈ a few frames + slack

static void on_event(const wsp_event_t *ev) {
    switch (ev->type) {
    case WSP_EV_SESSION_STARTED: s_state = APP_LISTENING; ESP_LOGI(TAG, "session ready"); break;
    case WSP_EV_USER_TRANSCRIPT: ESP_LOGI(TAG, "you: %s", ev->text); break;
    case WSP_EV_RESPONSE_TEXT:   ESP_LOGI(TAG, "bot: %s", ev->text); break;
    case WSP_EV_AUDIO_START:     s_state = APP_SPEAKING; break;
    case WSP_EV_TURN_DONE:       s_state = APP_LISTENING; break;
    case WSP_EV_ABORTED:
        s_state = APP_LISTENING;
        { pkt_t *p; while (xQueueReceive(s_pktq, &p, 0) == pdTRUE) free(p); }  // flush
        break;
    case WSP_EV_ERROR:           ESP_LOGE(TAG, "server error: %s", ev->text); break;
    default: break;
    }
}

static void on_audio(const uint8_t *data, int len) {
    if (len <= 0 || len > OPUS_MAX_PACKET) return;
    pkt_t *p = malloc(sizeof(pkt_t));
    if (!p) return;
    memcpy(p->data, data, len); p->len = len;
    if (xQueueSend(s_pktq, &p, 0) != pdTRUE) free(p);   // drop on overflow
}

static void mic_task(void *arg) {
    (void)arg;
    int16_t pcm[OPUS_UP_SAMPLES];
    uint8_t opus[OPUS_MAX_PACKET];
    for (;;) {
        int got = audio_mic_read(pcm, OPUS_UP_SAMPLES);   // keeps I2S draining always
        if (got != OPUS_UP_SAMPLES) continue;
        if (s_state != APP_LISTENING || !ws_client_connected()) continue;  // half-duplex
        int n = opus_codec_encode(pcm, opus, sizeof opus);
        if (n > 0) ws_client_send_audio(opus, n);
    }
}

static void spk_task(void *arg) {
    (void)arg;
    int16_t pcm[OPUS_DOWN_SAMPLES];
    pkt_t *p;
    for (;;) {
        if (xQueueReceive(s_pktq, &p, pdMS_TO_TICKS(100)) != pdTRUE) continue;
        int n = opus_codec_decode(p->data, p->len, pcm);
        free(p);
        if (n > 0) audio_spk_write(pcm, n);
    }
}

void app_main(void) {
    ESP_LOGI(TAG, "esp32-assistant booting");
    ESP_ERROR_CHECK(wifi_sta_start());
    if (!wifi_sta_wait_connected(20000)) { ESP_LOGE(TAG, "wifi timeout"); return; }
    ESP_ERROR_CHECK(audio_init());
    ESP_ERROR_CHECK(opus_codec_init());

    s_pktq = xQueueCreate(16, sizeof(pkt_t *));   // ~16*60ms buffer ceiling

    wsp_config_t cfg = {
        .host = CONFIG_AA_SERVER_HOST, .port = CONFIG_AA_SERVER_PORT,
        .secure = CONFIG_AA_SERVER_SECURE,
        .stt_engine = CONFIG_AA_STT_ENGINE, .tts_engine = CONFIG_AA_TTS_ENGINE,
        .language = CONFIG_AA_LANGUAGE, .sample_rate = 16000, .output_sample_rate = 24000,
    };
    ESP_ERROR_CHECK(ws_client_start(&cfg, on_event, on_audio));

    xTaskCreatePinnedToCore(spk_task, "spk", 4096, NULL, 6, NULL, 1);
    xTaskCreatePinnedToCore(mic_task, "mic", 4096, NULL, 5, NULL, 1);
    ESP_LOGI(TAG, "running");
}
```

- [ ] **Step 2: Update main REQUIRES**

`esp32-assistant/main/CMakeLists.txt`:
```cmake
idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES wifi ws_protocol ws_client audio opus_codec nvs_flash)
```

- [ ] **Step 3: End-to-end verification** (developer runs)

Start the gateway (`make dev`). Build/flash/monitor.
```bash
cd esp32-assistant && idf.py build flash monitor
```
Expected sequence in the monitor when you speak: `session ready` → `you: <your words>` → `bot: <reply text>` → reply audio plays from the speaker → returns to listening. Confirm the device does not cancel its own reply (half-duplex mute working).

- [ ] **Step 4: Commit**

```bash
git add esp32-assistant/main
git commit -m "feat(esp32): conversation state machine + jitter buffer (end-to-end voice)"
```

---

### Task 9: README + documentation

**Files:**
- Create: `esp32-assistant/README.md`
- Modify: `agent-assistant/integration.md` (add an ESP32 firmware pointer near the RPi section)

**Interfaces:** none (documentation).

- [ ] **Step 1: Write the README**

Create `esp32-assistant/README.md` covering: what it is (thin voice client for the gateway), hardware (ESP32-S3 + ES8311), prerequisites (ESP-IDF v5.x), configure (`idf.py menuconfig` → Assistant configuration: WiFi, server host/port, engines, I2S/codec pins), build/flash (`idf.py set-target esp32s3 && idf.py build flash monitor`), the protocol it speaks (link to `../agent-assistant/integration.md`), how to sanity-check the server first (playground `/ui` Conversation tab), the half-duplex / sample-rate decision recorded in Task 6, running the host unit tests (`cd test && make test`), and the explicit out-of-scope list (OLED, wake-word, OTA, provisioning, push-to-talk).

- [ ] **Step 2: Add a pointer from integration.md** — add under the RPi section a short note:
> **ESP32-S3 firmware:** a native ESP-IDF firmware speaking this same protocol lives in [`../esp32-assistant`](../../esp32-assistant/README.md). Hands-free, ES8311 codec, Opus uplink/downlink.

(Adjust the relative path to match the repo layout.)

- [ ] **Step 3: Verify host tests still pass** (sanity)

Run: `cd esp32-assistant/test && make test`
Expected: `ALL PASS`.

- [ ] **Step 4: Commit**

```bash
git add esp32-assistant/README.md agent-assistant/integration.md
git commit -m "docs(esp32): firmware README + integration.md pointer"
```

---

## Self-Review

**Spec coverage:**
- Protocol parse/build/URI → Tasks 1–3. ✓
- WiFi STA + Kconfig config → Task 4. ✓
- Opus 16k up / 24k down 60 ms → Task 5. ✓
- ES8311 + I2S, mute via state gating → Task 6 + Task 8 (`mic_task` half-duplex check). ✓
- WS client (events + binary frames, reconnect) → Task 7. ✓
- State machine (CONNECTING/LISTENING/SPEAKING), jitter buffer, half-duplex, aborted-flush, reconnect → Task 8. ✓
- Error handling: WS disconnect reconnect (esp_websocket_client + Task 7), jitter overflow drop / underrun silence (Task 8 queue), opus decode skip (Task 8 `if n>0`), WiFi lost (Task 4 reconnect). ✓
- Host unit tests for ws_protocol → Tasks 1–3. ✓
- Directory layout → realized across Tasks 1–9. ✓
- README + on-device verification flow → Task 9. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; the one genuinely uncertain external fact (Opus managed-component name; ES8311 same-bus dual-rate behavior) is handled with an explicit resolve-and-adjust step plus a documented fallback, not a placeholder.

**Type consistency:** `wsp_event_t`/`wsp_config_t` fields and `wsp_*` signatures are consistent across Tasks 1–3, 7, 8. `audio_*`, `opus_codec_*`, `ws_client_*` signatures match between their defining task and their use in Task 8. `OPUS_UP_SAMPLES`/`OPUS_DOWN_SAMPLES`/`OPUS_MAX_PACKET` used consistently.

**Note on TDD honesty:** Only `ws_protocol` (Tasks 1–3) is host-testable and follows strict red-green TDD. Hardware-bound tasks (4–8) use build + on-device-observation verification because they cannot run on the dev host; verification steps are explicit and must be run by the developer on the board, not faked.
