# ESP32 WiFi Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `esp32-assistant` be configured (WiFi SSID/password + gateway host/port) at runtime via a SoftAP + captive-portal web form, instead of compile-time Kconfig, and self-heal into that portal whenever WiFi fails to connect instead of dying silently.

**Architecture:** A new `wifi_cfg_t` struct is loaded from NVS at boot (falling back to Kconfig defaults for host/port only). `wifi_sta_start()` takes ssid/password as parameters instead of reading Kconfig macros. If STA connect times out, `main.c` calls into a new `provisioning` component, which brings up a SoftAP (`Lugo-XXXX`, open, `192.168.9.1`), a minimal DNS responder that answers every query with that IP (captive-portal trigger), and an `esp_http_server` serving a config form. Submitting the form writes to NVS and calls `esp_restart()`.

**Tech Stack:** ESP-IDF 5.4 (`esp_wifi`, `esp_netif`, `esp_http_server`, `nvs_flash`, `lwip` sockets), plain C11 for host-testable logic (matches the existing `ws_protocol` pattern).

## Global Constraints

- Target chip: `esp32s3` (already set in this project's `sdkconfig`).
- ESP-IDF is at `~/esp/esp-idf`; every `idf.py` invocation in this plan must be preceded by `source ~/esp/esp-idf/export.sh` in the same shell command (each Bash tool call is a fresh shell).
- The `esp32-assistant` directory is its **own git repo** (`esp32-assistant/.git`), separate from the outer `speech-text-transformer` repo. All `git add`/`git commit` commands in this plan run with `-C /Users/lugon/code/speech-text-transformer/esp32-assistant` (or equivalent cwd), never in the outer repo.
- Host-testable modules must stay dependency-free plain C11 (no ESP-IDF headers), per the existing `ws_protocol` convention — this is what makes `test/Makefile`'s `cc`-based tests possible without ESP-IDF installed.
- AP SoftAP IP is fixed at `192.168.9.1` (not ESP-IDF's `192.168.4.1` default) — user-specified.
- AP SSID pattern is `Lugo-XXXX` where `XXXX` = last 2 bytes of the STA MAC address as uppercase hex, stable across reboots (not re-randomized each boot).
- Config scope for this plan: WiFi SSID/password + gateway host/port only. STT/TTS engine, profile, language remain Kconfig.
- A physical ESP32-S3 device is connected at `/dev/cu.usbmodem101` for the final on-device verification task. `idf.py monitor` needs a real TTY — it will not work from a sandboxed Bash tool call; either ask the user to run it in their own terminal, or use the `esptool`/pyserial workaround documented in this plan's last task.

---

### Task 1: `wifi_cfg` — NVS-backed config load/save in the `wifi` component

**Files:**
- Create: `esp32-assistant/components/wifi/include/wifi_cfg_types.h`
- Create: `esp32-assistant/components/wifi/include/wifi_cfg.h`
- Create: `esp32-assistant/components/wifi/wifi_cfg.c`
- Modify: `esp32-assistant/components/wifi/CMakeLists.txt`

**Interfaces:**
- Produces: `wifi_cfg_t` struct (`ssid[33]`, `password[65]`, `server_host[128]`, `server_port`) in `wifi_cfg_types.h`; `wifi_cfg_load(wifi_cfg_t *out)` → `esp_err_t`, `wifi_cfg_save(const wifi_cfg_t *cfg)` → `esp_err_t` in `wifi_cfg.h`. Later ESP-IDF tasks (`provisioning.c`, `main.c`) consume `wifi_cfg.h`. The host-tested `provisioning_form` (Task 4) consumes only `wifi_cfg_types.h`, never `wifi_cfg.h` — that split matters: `wifi_cfg.h` pulls in `esp_err.h` (ESP-IDF-only), which would break the plain-C11 host test build if `provisioning_form.h` included it just to get the struct.

This task is not host-testable (`wifi_cfg.c` calls ESP-IDF's `nvs.h` APIs directly) — verification happens on-device in Task 8. There is no failing-test step here; write the implementation directly, matching the existing project convention that ESP-IDF-coupled code (e.g. `wifi_sta.c` itself) has no host unit tests.

- [ ] **Step 1: Create `wifi_cfg_types.h`** (no ESP-IDF headers — this is the one `provisioning_form.c` host-tested code is allowed to depend on)

```c
#pragma once

#define WIFI_CFG_SSID_MAX 32
#define WIFI_CFG_PASS_MAX 64
#define WIFI_CFG_HOST_MAX 127

typedef struct {
    char ssid[WIFI_CFG_SSID_MAX + 1];
    char password[WIFI_CFG_PASS_MAX + 1];
    char server_host[WIFI_CFG_HOST_MAX + 1];
    int  server_port;
} wifi_cfg_t;
```

- [ ] **Step 2: Create `wifi_cfg.h`**

```c
#pragma once
#include "wifi_cfg_types.h"
#include "esp_err.h"

// Loads saved WiFi/gateway config from NVS (namespace "aa_cfg"). ssid/password
// are "" if never saved (first boot -> caller should provision). server_host/
// server_port fall back to CONFIG_AA_SERVER_HOST/CONFIG_AA_SERVER_PORT when
// not yet saved in NVS. Requires nvs_flash_init() to have already run.
esp_err_t wifi_cfg_load(wifi_cfg_t *out);

// Persists cfg to NVS (namespace "aa_cfg"). Commits before returning.
esp_err_t wifi_cfg_save(const wifi_cfg_t *cfg);
```

- [ ] **Step 3: Create `wifi_cfg.c`**

```c
#include "wifi_cfg.h"
#include "nvs.h"
#include <string.h>

#define NVS_NS "aa_cfg"

static esp_err_t load_str(nvs_handle_t h, const char *key, char *out,
                           size_t outlen, const char *fallback) {
    size_t len = outlen;
    esp_err_t err = nvs_get_str(h, key, out, &len);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        strncpy(out, fallback, outlen - 1);
        out[outlen - 1] = '\0';
        return ESP_OK;
    }
    return err;
}

esp_err_t wifi_cfg_load(wifi_cfg_t *out) {
    memset(out, 0, sizeof(*out));
    strncpy(out->server_host, CONFIG_AA_SERVER_HOST, sizeof(out->server_host) - 1);
    out->server_port = CONFIG_AA_SERVER_PORT;

    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READONLY, &h);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;  // namespace never created yet: keep the defaults above
    }
    if (err != ESP_OK) return err;

    err = load_str(h, "ssid", out->ssid, sizeof(out->ssid), "");
    if (err != ESP_OK) { nvs_close(h); return err; }
    err = load_str(h, "pass", out->password, sizeof(out->password), "");
    if (err != ESP_OK) { nvs_close(h); return err; }
    err = load_str(h, "host", out->server_host, sizeof(out->server_host),
                   CONFIG_AA_SERVER_HOST);
    if (err != ESP_OK) { nvs_close(h); return err; }

    int32_t port = 0;
    err = nvs_get_i32(h, "port", &port);
    if (err == ESP_OK) {
        out->server_port = (int)port;
    } else if (err != ESP_ERR_NVS_NOT_FOUND) {
        nvs_close(h);
        return err;
    }

    nvs_close(h);
    return ESP_OK;
}

esp_err_t wifi_cfg_save(const wifi_cfg_t *cfg) {
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) return err;

    err = nvs_set_str(h, "ssid", cfg->ssid);
    if (err == ESP_OK) err = nvs_set_str(h, "pass", cfg->password);
    if (err == ESP_OK) err = nvs_set_str(h, "host", cfg->server_host);
    if (err == ESP_OK) err = nvs_set_i32(h, "port", cfg->server_port);
    if (err == ESP_OK) err = nvs_commit(h);

    nvs_close(h);
    return err;
}
```

- [ ] **Step 4: Update `components/wifi/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "wifi_sta.c" "wifi_cfg.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_wifi nvs_flash esp_event)
```

- [ ] **Step 5: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add \
  components/wifi/include/wifi_cfg_types.h components/wifi/include/wifi_cfg.h \
  components/wifi/wifi_cfg.c components/wifi/CMakeLists.txt
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
feat(wifi): add NVS-backed wifi_cfg_t load/save

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: change `wifi_sta_start()` to take credentials as parameters

**Files:**
- Modify: `esp32-assistant/components/wifi/include/wifi_sta.h`
- Modify: `esp32-assistant/components/wifi/wifi_sta.c`

**Interfaces:**
- Consumes: nothing new.
- Produces: `wifi_sta_start(const char *ssid, const char *password)` → `esp_err_t` (signature change; previously `wifi_sta_start(void)`). `main.c` (Task 6) calls this with values from `wifi_cfg_t`.

No host test (ESP-IDF-coupled, same as Task 1). This is a small, mechanical signature change — verify by build in Task 8.

- [ ] **Step 1: Update `wifi_sta.h`**

```c
#pragma once
#include "esp_err.h"
#include <stdbool.h>

esp_err_t wifi_sta_start(const char *ssid, const char *password);
bool wifi_sta_wait_connected(int timeout_ms);
```

- [ ] **Step 2: Update `wifi_sta.c`**

Replace the `wifi_sta_start` function (currently reading `CONFIG_AA_WIFI_SSID`/`CONFIG_AA_WIFI_PASS`) with:

```c
esp_err_t wifi_sta_start(const char *ssid, const char *password) {
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
    strncpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, password, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    return ESP_OK;
}
```

Note: the `nvs_flash_init()` block that used to be the first thing in this function is **removed** — it moves to `main.c` (Task 6) so it runs before `wifi_cfg_load()`, which also needs NVS initialized.

- [ ] **Step 3: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add \
  components/wifi/include/wifi_sta.h components/wifi/wifi_sta.c
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
refactor(wifi): wifi_sta_start takes ssid/password params, not Kconfig

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `provisioning_ssid` — AP SSID generation (host-tested)

**Files:**
- Create: `esp32-assistant/components/provisioning/include/provisioning_ssid.h`
- Create: `esp32-assistant/components/provisioning/provisioning_ssid.c`
- Create: `esp32-assistant/test/test_provisioning_ssid.c`
- Modify: `esp32-assistant/test/Makefile`

**Interfaces:**
- Produces: `provisioning_build_ssid(const uint8_t mac[6], char *buf, size_t buflen)` → `int` (length written, or -1 if `buf` too small). Consumed by `provisioning.c` in Task 5.

- [ ] **Step 1: Create `provisioning_ssid.h`**

```c
#pragma once
#include <stdint.h>
#include <stddef.h>

// Builds AP SSID "Lugo-XXXX" where XXXX is the last 2 bytes of `mac` (6 bytes)
// as uppercase hex, e.g. {..,0x48,0xD0} -> "Lugo-48D0". Stable across reboots
// (derived from the device's own MAC, not randomized). Returns length written
// (excluding NUL), or -1 if buf is too small.
int provisioning_build_ssid(const uint8_t mac[6], char *buf, size_t buflen);
```

- [ ] **Step 2: Create `provisioning_ssid.c`**

```c
#include "provisioning_ssid.h"
#include <stdio.h>

int provisioning_build_ssid(const uint8_t mac[6], char *buf, size_t buflen) {
    int n = snprintf(buf, buflen, "Lugo-%02X%02X", mac[4], mac[5]);
    if (n < 0 || (size_t)n >= buflen) return -1;
    return n;
}
```

- [ ] **Step 3: Write the failing test — create `test/test_provisioning_ssid.c`**

```c
#include "provisioning_ssid.h"
#include <string.h>
#include <stdio.h>

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
  printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static void test_basic(void) {
    uint8_t mac[6] = {0x28, 0x84, 0x85, 0x50, 0x48, 0xD0};
    char buf[32];
    int n = provisioning_build_ssid(mac, buf, sizeof buf);
    CHECK(n == 9);
    CHECK(strcmp(buf, "Lugo-48D0") == 0);
}

static void test_zero_mac(void) {
    uint8_t mac[6] = {0, 0, 0, 0, 0, 0};
    char buf[32];
    int n = provisioning_build_ssid(mac, buf, sizeof buf);
    CHECK(n == 9);
    CHECK(strcmp(buf, "Lugo-0000") == 0);
}

static void test_buf_exact_fit(void) {
    uint8_t mac[6] = {0, 0, 0, 0, 0, 0};
    char buf[10];  // "Lugo-0000" (9 chars) + NUL = 10
    CHECK(provisioning_build_ssid(mac, buf, sizeof buf) == 9);
}

static void test_buf_too_small(void) {
    uint8_t mac[6] = {0, 0, 0, 0, 0, 0};
    char buf[5];
    CHECK(provisioning_build_ssid(mac, buf, sizeof buf) == -1);
}

int main(void) {
    test_basic();
    test_zero_mac();
    test_buf_exact_fit();
    test_buf_too_small();
    if (failures) { printf("%d FAILURES\n", failures); return 1; }
    printf("ALL PASS\n");
    return 0;
}
```

- [ ] **Step 4: Update `test/Makefile` to add the new test target**

```makefile
CC ?= cc
CFLAGS = -std=c11 -Wall -Wextra -g -O0 -I../components/ws_protocol/include \
         -I../components/provisioning/include -I../components/wifi/include
SRC_WS_PROTOCOL = ../components/ws_protocol/ws_protocol.c
SRC_PROVISIONING_SSID = ../components/provisioning/provisioning_ssid.c
SRC_PROVISIONING_FORM = ../components/provisioning/provisioning_form.c

.PHONY: test
test: test_ws_protocol test_provisioning_ssid test_provisioning_form
	./test_ws_protocol
	./test_provisioning_ssid
	./test_provisioning_form

test_ws_protocol: test_ws_protocol.c $(SRC_WS_PROTOCOL)
	$(CC) $(CFLAGS) -o $@ $^

test_provisioning_ssid: test_provisioning_ssid.c $(SRC_PROVISIONING_SSID)
	$(CC) $(CFLAGS) -o $@ $^

test_provisioning_form: test_provisioning_form.c $(SRC_PROVISIONING_FORM)
	$(CC) $(CFLAGS) -o $@ $^

clean:
	rm -rf test_ws_protocol test_ws_protocol.dSYM \
	       test_provisioning_ssid test_provisioning_ssid.dSYM \
	       test_provisioning_form test_provisioning_form.dSYM
```

(`test_provisioning_form` is created in Task 4 — this Makefile already references it so Task 4 doesn't need to touch the Makefile again beyond what's already here.)

- [ ] **Step 5: Run test to verify it fails (file doesn't compile yet — `provisioning_ssid.c`/`.h` didn't exist before Step 1/2, but if you're executing steps in order they already do; run it anyway to confirm the harness works)**

Run: `cd /Users/lugon/code/speech-text-transformer/esp32-assistant/test && make test_provisioning_ssid && ./test_provisioning_ssid`
Expected: `ALL PASS` (Steps 1-2 already implemented the code, so this should pass immediately — if it fails, fix `provisioning_ssid.c` before continuing).

- [ ] **Step 6: Create `components/provisioning/CMakeLists.txt` (minimal, will gain more SRCS in Task 4-5)**

```cmake
idf_component_register(
    SRCS "provisioning_ssid.c" "provisioning_form.c" "provisioning.c"
    INCLUDE_DIRS "include"
    REQUIRES wifi esp_wifi esp_netif esp_http_server nvs_flash lwip)
```

(This references `provisioning_form.c` and `provisioning.c` which don't exist until Tasks 4 and 5 — the ESP-IDF build isn't run until Task 8, so this is safe to write now and fill in incrementally.)

- [ ] **Step 7: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add \
  components/provisioning/include/provisioning_ssid.h \
  components/provisioning/provisioning_ssid.c \
  components/provisioning/CMakeLists.txt \
  test/test_provisioning_ssid.c test/Makefile
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
feat(provisioning): add host-tested AP SSID builder (Lugo-XXXX from MAC)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `provisioning_form` — HTML render + form parsing (host-tested)

**Files:**
- Create: `esp32-assistant/components/provisioning/include/provisioning_form.h`
- Create: `esp32-assistant/components/provisioning/provisioning_form.c`
- Create: `esp32-assistant/test/test_provisioning_form.c`

**Interfaces:**
- Consumes: `wifi_cfg_t` from Task 1 (`components/wifi/include/wifi_cfg_types.h`).
- Produces: `provisioning_render_form(char *buf, size_t buflen, const wifi_cfg_t *cfg, const char *error_msg)` → `int`; `provisioning_render_saved(char *buf, size_t buflen)` → `int`; `provisioning_parse_form(const char *body, size_t len, wifi_cfg_t *out)` → `int` (0 success, -1 invalid). Consumed by `provisioning.c` in Task 5.

- [ ] **Step 1: Create `provisioning_form.h`**

```c
#pragma once
#include "wifi_cfg_types.h"  // struct only, no ESP-IDF deps — keeps this host-testable
#include <stddef.h>

// Renders the HTML configuration form into buf, pre-filled from `cfg` (ssid
// and server_host/server_port; password is never pre-filled, so it's never
// echoed back in page source). If `error_msg` is non-NULL and non-empty,
// renders it above the form. Returns length written (excluding NUL), or -1
// if buf is too small.
int provisioning_render_form(char *buf, size_t buflen, const wifi_cfg_t *cfg,
                              const char *error_msg);

// Renders the short "saved, restarting" confirmation page. Returns length
// written (excluding NUL), or -1 if buf is too small.
int provisioning_render_saved(char *buf, size_t buflen);

// Parses an application/x-www-form-urlencoded POST body (ssid, password,
// host, port fields) into *out. Percent-decodes and '+'-decodes values.
// Returns 0 on success. Returns -1 if ssid is missing/empty, host is
// missing/empty, or port is missing, non-numeric, or not in [1, 65535]. If
// the password field is absent or empty, out->password is set to "" (caller
// decides whether to preserve a previously-saved password in that case).
int provisioning_parse_form(const char *body, size_t len, wifi_cfg_t *out);
```

- [ ] **Step 2: Create `provisioning_form.c`**

```c
#include "provisioning_form.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static int hex_val(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// Percent/plus-decodes in[0..inlen) into out (NUL-terminated). Returns
// decoded length, or -1 if it would overflow out.
static int url_decode(const char *in, size_t inlen, char *out, size_t outlen) {
    size_t oi = 0;
    for (size_t ii = 0; ii < inlen; ii++) {
        char c = in[ii];
        char decoded;
        if (c == '+') {
            decoded = ' ';
        } else if (c == '%' && ii + 2 < inlen) {
            int hi = hex_val(in[ii + 1]);
            int lo = hex_val(in[ii + 2]);
            if (hi < 0 || lo < 0) {
                decoded = '%';
            } else {
                decoded = (char)((hi << 4) | lo);
                ii += 2;
            }
        } else {
            decoded = c;
        }
        if (oi + 1 >= outlen) return -1;
        out[oi++] = decoded;
    }
    out[oi] = '\0';
    return (int)oi;
}

// Finds `key=` as a whole field within body[0..len) (fields separated by
// '&'). Sets *value_len to the raw (still encoded) value length. Returns a
// pointer to the value start, or NULL if key is not present as a field.
static const char *find_field(const char *body, size_t len, const char *key,
                               size_t *value_len) {
    size_t keylen = strlen(key);
    size_t i = 0;
    while (i < len) {
        size_t field_start = i;
        size_t j = field_start;
        while (j < len && body[j] != '&') j++;
        if (j - field_start > keylen && body[field_start + keylen] == '=' &&
            strncmp(body + field_start, key, keylen) == 0) {
            const char *val = body + field_start + keylen + 1;
            *value_len = j - (field_start + keylen + 1);
            return val;
        }
        i = j + 1;
    }
    return NULL;
}

int provisioning_parse_form(const char *body, size_t len, wifi_cfg_t *out) {
    size_t vlen;
    const char *v;

    v = find_field(body, len, "ssid", &vlen);
    if (!v || vlen == 0) return -1;
    if (url_decode(v, vlen, out->ssid, sizeof out->ssid) < 0) return -1;
    if (out->ssid[0] == '\0') return -1;

    v = find_field(body, len, "password", &vlen);
    if (v && vlen > 0) {
        if (url_decode(v, vlen, out->password, sizeof out->password) < 0) return -1;
    } else {
        out->password[0] = '\0';
    }

    v = find_field(body, len, "host", &vlen);
    if (!v || vlen == 0) return -1;
    if (url_decode(v, vlen, out->server_host, sizeof out->server_host) < 0) return -1;

    v = find_field(body, len, "port", &vlen);
    if (!v || vlen == 0 || vlen >= 7) return -1;
    char portbuf[8];
    memcpy(portbuf, v, vlen);
    portbuf[vlen] = '\0';
    char *endptr;
    long port = strtol(portbuf, &endptr, 10);
    if (*endptr != '\0' || port < 1 || port > 65535) return -1;
    out->server_port = (int)port;

    return 0;
}

// Escapes &, ", <, > for safe inclusion inside an HTML attribute value.
// Returns length written (excluding NUL), or -1 if buf too small.
static int escape_attr(const char *in, char *out, size_t outlen) {
    size_t oi = 0;
    for (const char *p = in; *p; p++) {
        const char *rep;
        switch (*p) {
            case '&': rep = "&amp;"; break;
            case '"': rep = "&quot;"; break;
            case '<': rep = "&lt;"; break;
            case '>': rep = "&gt;"; break;
            default: rep = NULL; break;
        }
        size_t rlen = rep ? strlen(rep) : 1;
        if (oi + rlen >= outlen) return -1;
        if (rep) { memcpy(out + oi, rep, rlen); oi += rlen; }
        else { out[oi++] = *p; }
    }
    out[oi] = '\0';
    return (int)oi;
}

int provisioning_render_form(char *buf, size_t buflen, const wifi_cfg_t *cfg,
                              const char *error_msg) {
    char ssid_esc[WIFI_CFG_SSID_MAX * 6 + 1];
    char host_esc[WIFI_CFG_HOST_MAX * 6 + 1];
    if (escape_attr(cfg->ssid, ssid_esc, sizeof ssid_esc) < 0) return -1;
    if (escape_attr(cfg->server_host, host_esc, sizeof host_esc) < 0) return -1;

    int n = snprintf(buf, buflen,
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
        "<title>esp32-assistant setup</title></head><body>"
        "<h1>esp32-assistant setup</h1>"
        "%s%s%s"
        "<form method=post action=/save>"
        "<label>WiFi SSID<br><input name=ssid value=\"%s\" required></label><br><br>"
        "<label>WiFi password (leave blank to keep the saved one)<br>"
        "<input name=password type=password></label><br><br>"
        "<label>Gateway host<br><input name=host value=\"%s\" required></label><br><br>"
        "<label>Gateway port<br><input name=port type=number value=\"%d\" "
        "min=1 max=65535 required></label><br><br>"
        "<button type=submit>Save &amp; restart</button>"
        "</form></body></html>",
        (error_msg && error_msg[0]) ? "<p style=\"color:red\">" : "",
        (error_msg && error_msg[0]) ? error_msg : "",
        (error_msg && error_msg[0]) ? "</p>" : "",
        ssid_esc, host_esc, cfg->server_port);
    if (n < 0 || (size_t)n >= buflen) return -1;
    return n;
}

int provisioning_render_saved(char *buf, size_t buflen) {
    int n = snprintf(buf, buflen,
        "<!doctype html><html><head><meta charset=utf-8></head>"
        "<body><h1>Saved. Restarting...</h1></body></html>");
    if (n < 0 || (size_t)n >= buflen) return -1;
    return n;
}
```

- [ ] **Step 3: Write the failing test — create `test/test_provisioning_form.c`**

```c
#include "provisioning_form.h"
#include <string.h>
#include <stdio.h>

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
  printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static void test_parse_basic(void) {
    wifi_cfg_t cfg = {0};
    const char *body = "ssid=MyHome&password=secret123&host=192.168.1.50&port=8000";
    int rc = provisioning_parse_form(body, strlen(body), &cfg);
    CHECK(rc == 0);
    CHECK(strcmp(cfg.ssid, "MyHome") == 0);
    CHECK(strcmp(cfg.password, "secret123") == 0);
    CHECK(strcmp(cfg.server_host, "192.168.1.50") == 0);
    CHECK(cfg.server_port == 8000);
}

static void test_parse_url_encoded(void) {
    wifi_cfg_t cfg = {0};
    const char *body = "ssid=My+Home&password=p%40ss%21&host=gw.local&port=443";
    int rc = provisioning_parse_form(body, strlen(body), &cfg);
    CHECK(rc == 0);
    CHECK(strcmp(cfg.ssid, "My Home") == 0);
    CHECK(strcmp(cfg.password, "p@ss!") == 0);
    CHECK(strcmp(cfg.server_host, "gw.local") == 0);
    CHECK(cfg.server_port == 443);
}

static void test_parse_missing_ssid(void) {
    wifi_cfg_t cfg = {0};
    const char *body = "password=secret&host=192.168.1.50&port=8000";
    CHECK(provisioning_parse_form(body, strlen(body), &cfg) == -1);
}

static void test_parse_empty_ssid(void) {
    wifi_cfg_t cfg = {0};
    const char *body = "ssid=&host=192.168.1.50&port=8000";
    CHECK(provisioning_parse_form(body, strlen(body), &cfg) == -1);
}

static void test_parse_no_password_field_defaults_empty(void) {
    wifi_cfg_t cfg = {0};
    strcpy(cfg.password, "leftover");
    const char *body = "ssid=MyHome&host=192.168.1.50&port=8000";
    CHECK(provisioning_parse_form(body, strlen(body), &cfg) == 0);
    CHECK(cfg.password[0] == '\0');
}

static void test_parse_bad_port_non_numeric(void) {
    wifi_cfg_t cfg = {0};
    const char *body = "ssid=MyHome&host=192.168.1.50&port=notanumber";
    CHECK(provisioning_parse_form(body, strlen(body), &cfg) == -1);
}

static void test_parse_port_out_of_range(void) {
    wifi_cfg_t cfg = {0};
    const char *body = "ssid=MyHome&host=192.168.1.50&port=70000";
    CHECK(provisioning_parse_form(body, strlen(body), &cfg) == -1);
}

static void test_parse_missing_host(void) {
    wifi_cfg_t cfg = {0};
    const char *body = "ssid=MyHome&port=8000";
    CHECK(provisioning_parse_form(body, strlen(body), &cfg) == -1);
}

static void test_render_form_contains_values_not_password(void) {
    wifi_cfg_t cfg = {0};
    strcpy(cfg.ssid, "MyHome");
    strcpy(cfg.password, "supersecret");
    strcpy(cfg.server_host, "192.168.1.50");
    cfg.server_port = 8000;
    char buf[2048];
    int n = provisioning_render_form(buf, sizeof buf, &cfg, NULL);
    CHECK(n > 0);
    CHECK(strstr(buf, "MyHome") != NULL);
    CHECK(strstr(buf, "192.168.1.50") != NULL);
    CHECK(strstr(buf, "8000") != NULL);
    CHECK(strstr(buf, "supersecret") == NULL);
}

static void test_render_form_escapes_html(void) {
    wifi_cfg_t cfg = {0};
    strcpy(cfg.ssid, "My\"Net<script>");
    strcpy(cfg.server_host, "host");
    cfg.server_port = 80;
    char buf[2048];
    int n = provisioning_render_form(buf, sizeof buf, &cfg, NULL);
    CHECK(n > 0);
    CHECK(strstr(buf, "<script>") == NULL);
    CHECK(strstr(buf, "&lt;script&gt;") != NULL);
}

static void test_render_form_shows_error(void) {
    wifi_cfg_t cfg = {0};
    strcpy(cfg.ssid, "MyHome");
    strcpy(cfg.server_host, "192.168.1.50");
    cfg.server_port = 8000;
    char buf[2048];
    int n = provisioning_render_form(buf, sizeof buf, &cfg, "bad input");
    CHECK(n > 0);
    CHECK(strstr(buf, "bad input") != NULL);
}

static void test_render_form_too_small(void) {
    wifi_cfg_t cfg = {0};
    strcpy(cfg.ssid, "MyHome");
    strcpy(cfg.server_host, "192.168.1.50");
    cfg.server_port = 8000;
    char buf[10];
    CHECK(provisioning_render_form(buf, sizeof buf, &cfg, NULL) == -1);
}

static void test_render_saved(void) {
    char buf[256];
    int n = provisioning_render_saved(buf, sizeof buf);
    CHECK(n > 0);
    CHECK(strstr(buf, "Restarting") != NULL);
}

static void test_render_saved_too_small(void) {
    char buf[4];
    CHECK(provisioning_render_saved(buf, sizeof buf) == -1);
}

int main(void) {
    test_parse_basic();
    test_parse_url_encoded();
    test_parse_missing_ssid();
    test_parse_empty_ssid();
    test_parse_no_password_field_defaults_empty();
    test_parse_bad_port_non_numeric();
    test_parse_port_out_of_range();
    test_parse_missing_host();
    test_render_form_contains_values_not_password();
    test_render_form_escapes_html();
    test_render_form_shows_error();
    test_render_form_too_small();
    test_render_saved();
    test_render_saved_too_small();
    if (failures) { printf("%d FAILURES\n", failures); return 1; }
    printf("ALL PASS\n");
    return 0;
}
```

- [ ] **Step 4: Run the tests**

Run: `cd /Users/lugon/code/speech-text-transformer/esp32-assistant/test && make test`
Expected: all three binaries (`test_ws_protocol`, `test_provisioning_ssid`, `test_provisioning_form`) print `ALL PASS`. If `test_provisioning_form` fails, fix `provisioning_form.c` (most likely culprits: the `find_field` boundary check, or `escape_attr` buffer sizing) before continuing.

- [ ] **Step 5: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add \
  components/provisioning/include/provisioning_form.h \
  components/provisioning/provisioning_form.c \
  test/test_provisioning_form.c
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
feat(provisioning): add host-tested form render/parse (HTML escaping, urlencoded parsing)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `provisioning.c` — SoftAP + captive DNS + HTTP server orchestration

**Files:**
- Create: `esp32-assistant/components/provisioning/include/provisioning.h`
- Create: `esp32-assistant/components/provisioning/provisioning.c`

**Interfaces:**
- Consumes: `wifi_cfg_t`, `wifi_cfg_save()` (Task 1); `provisioning_build_ssid()` (Task 3); `provisioning_render_form()`, `provisioning_render_saved()`, `provisioning_parse_form()` (Task 4).
- Produces: `provisioning_start(const wifi_cfg_t *current)` → `void` (never returns on the success path — triggers `esp_restart()`). Consumed by `main.c` in Task 6.

Not host-testable (ESP-IDF `esp_wifi`/`esp_http_server`/lwip sockets throughout). Verified on-device in Task 8.

- [ ] **Step 1: Create `provisioning.h`**

```c
#pragma once
#include "wifi_cfg.h"

// Brings up SoftAP "Lugo-XXXX" (open, 192.168.9.1) + a captive DNS responder
// + an HTTP config portal pre-filled from `current`. Blocks the calling task
// forever. On successful form submission it saves the new config to NVS and
// calls esp_restart() (does not return in that case either). Assumes
// esp_netif_init()/esp_event_loop_create_default() and esp_wifi_init() have
// already run (true whenever called from app_main after wifi_sta_start()).
void provisioning_start(const wifi_cfg_t *current);
```

- [ ] **Step 2: Create `provisioning.c`**

```c
#include "provisioning.h"
#include "provisioning_ssid.h"
#include "provisioning_form.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "lwip/ip4_addr.h"
#include <string.h>
#include <stdlib.h>

static const char *TAG = "provisioning";

static wifi_cfg_t s_cfg;  // working copy shown/edited by the portal

static void dns_task(void *arg) {
    (void)arg;
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) { ESP_LOGE(TAG, "dns socket failed"); vTaskDelete(NULL); return; }

    struct sockaddr_in addr = {
        .sin_family = AF_INET, .sin_port = htons(53),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(sock, (struct sockaddr *)&addr, sizeof addr) < 0) {
        ESP_LOGE(TAG, "dns bind failed");
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    uint8_t req[512];
    uint8_t resp[512];
    for (;;) {
        struct sockaddr_in from;
        socklen_t fromlen = sizeof from;
        int len = recvfrom(sock, req, sizeof req, 0, (struct sockaddr *)&from, &fromlen);
        if (len < 12) continue;  // shorter than a DNS header

        int qend = 12;
        while (qend < len && req[qend] != 0) qend += req[qend] + 1;
        qend += 1 + 4;  // zero label + QTYPE(2) + QCLASS(2)
        if (qend > len || qend + 16 > (int)sizeof resp) continue;

        memcpy(resp, req, qend);
        resp[2] = 0x81; resp[3] = 0x80;   // QR=1, RA=1
        resp[6] = 0; resp[7] = 1;         // ANCOUNT = 1
        resp[8] = 0; resp[9] = 0;         // NSCOUNT = 0
        resp[10] = 0; resp[11] = 0;       // ARCOUNT = 0

        int p = qend;
        resp[p++] = 0xC0; resp[p++] = 0x0C;              // name = pointer to offset 12
        resp[p++] = 0x00; resp[p++] = 0x01;              // TYPE = A
        resp[p++] = 0x00; resp[p++] = 0x01;              // CLASS = IN
        resp[p++] = 0x00; resp[p++] = 0x00;
        resp[p++] = 0x00; resp[p++] = 0x3C;              // TTL = 60
        resp[p++] = 0x00; resp[p++] = 0x04;               // RDLENGTH = 4
        resp[p++] = 192; resp[p++] = 168; resp[p++] = 9; resp[p++] = 1;  // 192.168.9.1

        sendto(sock, resp, p, 0, (struct sockaddr *)&from, fromlen);
    }
}

static esp_err_t root_get_handler(httpd_req_t *req) {
    char *buf = malloc(4096);
    if (!buf) return ESP_ERR_NO_MEM;
    int n = provisioning_render_form(buf, 4096, &s_cfg, NULL);
    if (n < 0) { free(buf); httpd_resp_send_500(req); return ESP_FAIL; }
    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, buf, n);
    free(buf);
    return ESP_OK;
}

static esp_err_t save_post_handler(httpd_req_t *req) {
    if (req->content_len <= 0 || req->content_len > 2048) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "body too large");
        return ESP_FAIL;
    }
    char *body = malloc(req->content_len + 1);
    if (!body) return ESP_ERR_NO_MEM;
    int received = 0;
    while (received < req->content_len) {
        int r = httpd_req_recv(req, body + received, req->content_len - received);
        if (r <= 0) { free(body); httpd_resp_send_500(req); return ESP_FAIL; }
        received += r;
    }
    body[received] = '\0';

    wifi_cfg_t parsed;
    memset(&parsed, 0, sizeof parsed);
    int rc = provisioning_parse_form(body, received, &parsed);
    free(body);

    char *resp_buf = malloc(4096);
    if (!resp_buf) return ESP_ERR_NO_MEM;

    if (rc != 0) {
        int n = provisioning_render_form(resp_buf, 4096, &s_cfg,
            "Invalid input: SSID and gateway host are required, port must be 1-65535.");
        if (n < 0) { free(resp_buf); httpd_resp_send_500(req); return ESP_FAIL; }
        httpd_resp_set_type(req, "text/html");
        httpd_resp_send(req, resp_buf, n);
        free(resp_buf);
        return ESP_OK;
    }

    // The form never pre-fills the password field; if left blank, keep the
    // previously-saved one instead of wiping it.
    if (parsed.password[0] == '\0') {
        strncpy(parsed.password, s_cfg.password, sizeof(parsed.password) - 1);
    }

    esp_err_t err = wifi_cfg_save(&parsed);
    if (err != ESP_OK) {
        int n = provisioning_render_form(resp_buf, 4096, &parsed,
                                          "Failed to save. Try again.");
        if (n < 0) { free(resp_buf); httpd_resp_send_500(req); return ESP_FAIL; }
        httpd_resp_set_type(req, "text/html");
        httpd_resp_send(req, resp_buf, n);
        free(resp_buf);
        return ESP_OK;
    }

    int n = provisioning_render_saved(resp_buf, 4096);
    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, resp_buf, n > 0 ? n : 0);
    free(resp_buf);

    vTaskDelay(pdMS_TO_TICKS(500));  // let the response flush before rebooting
    esp_restart();
    return ESP_OK;  // unreachable
}

void provisioning_start(const wifi_cfg_t *current) {
    s_cfg = *current;

    esp_netif_t *ap_netif = esp_netif_create_default_wifi_ap();
    esp_netif_dhcps_stop(ap_netif);

    esp_netif_ip_info_t ip_info;
    IP4_ADDR(&ip_info.ip, 192, 168, 9, 1);
    IP4_ADDR(&ip_info.gw, 192, 168, 9, 1);
    IP4_ADDR(&ip_info.netmask, 255, 255, 255, 0);
    ESP_ERROR_CHECK(esp_netif_set_ip_info(ap_netif, &ip_info));
    ESP_ERROR_CHECK(esp_netif_dhcps_start(ap_netif));

    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, mac));
    char ssid[32];
    provisioning_build_ssid(mac, ssid, sizeof ssid);

    wifi_config_t ap_config = { 0 };
    strncpy((char *)ap_config.ap.ssid, ssid, sizeof(ap_config.ap.ssid) - 1);
    ap_config.ap.ssid_len = strlen(ssid);
    ap_config.ap.channel = 1;
    ap_config.ap.max_connection = 4;
    ap_config.ap.authmode = WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "provisioning AP '%s' up at 192.168.9.1", ssid);

    xTaskCreate(dns_task, "prov_dns", 4096, NULL, 5, NULL);

    httpd_config_t http_cfg = HTTPD_DEFAULT_CONFIG();
    http_cfg.max_uri_handlers = 4;
    http_cfg.uri_match_fn = httpd_uri_match_wildcard;
    httpd_handle_t server;
    ESP_ERROR_CHECK(httpd_start(&server, &http_cfg));

    httpd_uri_t root_uri = { .uri = "/*", .method = HTTP_GET, .handler = root_get_handler };
    httpd_uri_t save_uri = { .uri = "/save", .method = HTTP_POST, .handler = save_post_handler };
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &root_uri));
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &save_uri));

    // save_post_handler() calls esp_restart() on success; block here so
    // app_main doesn't fall through to starting audio/ws_client without a
    // working WiFi connection.
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
```

- [ ] **Step 3: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add \
  components/provisioning/include/provisioning.h components/provisioning/provisioning.c
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
feat(provisioning): SoftAP + captive DNS + HTTP config portal orchestration

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: wire it into `main.c` and Kconfig

**Files:**
- Modify: `esp32-assistant/main/main.c`
- Modify: `esp32-assistant/main/Kconfig.projbuild`
- Modify: `esp32-assistant/main/CMakeLists.txt`

**Interfaces:**
- Consumes: `wifi_cfg_t`, `wifi_cfg_load()` (Task 1); `wifi_sta_start(ssid, password)` (Task 2); `provisioning_start()` (Task 5).

- [ ] **Step 1: Update `main/main.c`**

Add includes near the top (after the existing `#include "wifi_sta.h"`):

```c
#include "wifi_cfg.h"
#include "provisioning.h"
#include "nvs_flash.h"
```

Replace the body of `app_main` (everything from `ESP_ERROR_CHECK(wifi_sta_start())` through the `if (!wifi_sta_wait_connected(...))` block) with:

```c
void app_main(void) {
    ESP_LOGI(TAG, "esp32-assistant booting");

    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    wifi_cfg_t cfg;
    ESP_ERROR_CHECK(wifi_cfg_load(&cfg));

    ESP_ERROR_CHECK(wifi_sta_start(cfg.ssid, cfg.password));
    if (!wifi_sta_wait_connected(15000)) {
        ESP_LOGW(TAG, "wifi connect failed, starting provisioning portal");
        provisioning_start(&cfg);  // does not return
    }

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
    ESP_ERROR_CHECK(ws_client_start(&wcfg, on_event, on_audio));

    xTaskCreatePinnedToCore(spk_task, "spk", 4096, NULL, 6, NULL, 1);
    xTaskCreatePinnedToCore(mic_task, "mic", 4096, NULL, 5, NULL, 1);
    ESP_LOGI(TAG, "running");
}
```

- [ ] **Step 2: Remove the now-dead WiFi Kconfig options from `main/Kconfig.projbuild`**

Delete these two blocks (the `AA_WIFI_SSID`/`AA_WIFI_PASS` config entries near the top of the `menu "Assistant configuration"` block):

```
config AA_WIFI_SSID
    string "WiFi SSID"
    default "myssid"

config AA_WIFI_PASS
    string "WiFi password"
    default "mypassword"

```

`AA_SERVER_HOST`/`AA_SERVER_PORT` stay unchanged — they're still used as the first-boot NVS fallback default in `wifi_cfg_load()`.

- [ ] **Step 3: Update `main/CMakeLists.txt`**

```cmake
idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES wifi ws_protocol ws_client audio opus_codec nvs_flash provisioning)
```

- [ ] **Step 4: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add \
  main/main.c main/Kconfig.projbuild main/CMakeLists.txt
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
feat(main): fall back to WiFi provisioning portal instead of dying on connect timeout

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: update README

**Files:**
- Modify: `esp32-assistant/README.md`

- [ ] **Step 1: Remove the two WiFi rows from the Configure table (lines 44-45)**

Delete:
```
| WiFi SSID | `AA_WIFI_SSID` | `myssid` | 2.4 GHz network |
| WiFi password | `AA_WIFI_PASS` | `mypassword` | |
```

Add this sentence right after the table's introductory line ("Navigate to **"Assistant configuration"** and set:"), i.e. before the table:

```
WiFi credentials are no longer set here — see **WiFi provisioning** below. The
table below covers the gateway/hardware settings that remain compile-time.
```

- [ ] **Step 2: Replace the "Web-based WiFi provisioning" bullet in "Out of scope (MVP)" (line 187)**

Delete the line `- Web-based WiFi provisioning` from that list (it's now implemented).

- [ ] **Step 3: Add a new "WiFi provisioning" section**, right after the "Configure" section (after the GPIO defaults paragraph, before "## Build, flash, and monitor"):

```markdown
---

## WiFi provisioning

The device has no compile-time WiFi credentials. On every boot it tries to
connect using whatever SSID/password is saved in NVS. If nothing is saved yet
(first boot), or the saved credentials fail to connect within 15 seconds, the
device switches into **provisioning mode**:

1. It starts an open WiFi access point named `Lugo-XXXX` (`XXXX` = the last
   4 hex digits of the device's MAC address — stable across reboots, so it's
   always the same network name for a given device).
2. Connect a phone or laptop to that network. Most OSes will pop up a
   "Sign in to network" / captive-portal prompt automatically; if not,
   browse to `http://192.168.9.1`.
3. Fill in your WiFi SSID/password and the gateway host/port, then submit.
4. The device saves the values to NVS and restarts, this time connecting to
   your WiFi and the gateway.

To reconfigure later (new WiFi network, moved gateway), the easiest path is
to erase NVS and reboot so it goes straight back into provisioning mode:

```bash
source ~/esp/esp-idf/export.sh
idf.py -p <port> erase-flash
idf.py -p <port> flash
```
```

- [ ] **Step 4: Add `provisioning` to the Components table** (in the `## Components` section)

Insert this row right after the `wifi` row:

```
| `provisioning` | SoftAP + captive DNS + HTTP config portal (`provisioning_start`); host-tested SSID/form logic in `provisioning_ssid.c`/`provisioning_form.c` |
```

- [ ] **Step 5: Commit**

```bash
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant add README.md
git -C /Users/lugon/code/speech-text-transformer/esp32-assistant commit -m "$(cat <<'EOF'
docs: document WiFi provisioning flow, remove dead AA_WIFI_SSID/PASS Kconfig docs

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: build, flash, and verify on the connected device

**Files:** none (build/flash/manual verification only).

- [ ] **Step 1: Run host tests one more time (fast feedback before the slow ESP-IDF build)**

Run: `cd /Users/lugon/code/speech-text-transformer/esp32-assistant/test && make clean && make test`
Expected: `ALL PASS` for all three test binaries.

- [ ] **Step 2: Build the firmware**

Run:
```bash
source ~/esp/esp-idf/export.sh
idf.py -C /Users/lugon/code/speech-text-transformer/esp32-assistant build
```
Expected: `Project build complete.` with no errors. If it fails on `provisioning` component symbols (e.g. `httpd_uri_match_wildcard` or `IP4_ADDR` undefined), check that `components/provisioning/CMakeLists.txt`'s `REQUIRES` includes `esp_http_server` and `lwip`.

- [ ] **Step 3: Confirm the device is connected**

Run: `ls /dev/cu.usbmodem*`
Expected: `/dev/cu.usbmodem101` (or wherever it currently enumerates — adjust the port in later steps if different).

- [ ] **Step 4: Erase NVS so the device has no saved WiFi credentials (forces provisioning mode)**

Run:
```bash
source ~/esp/esp-idf/export.sh
idf.py -C /Users/lugon/code/speech-text-transformer/esp32-assistant -p /dev/cu.usbmodem101 erase-flash
```
Expected: `Chip erase completed successfully`.

- [ ] **Step 5: Flash the new firmware**

Run:
```bash
source ~/esp/esp-idf/export.sh
idf.py -C /Users/lugon/code/speech-text-transformer/esp32-assistant -p /dev/cu.usbmodem101 flash
```
Expected: `Hash of data verified.` for all three images, ending in `Hard resetting via RTS pin... Done`. If it fails with "port is busy", check for a leftover `idf.py monitor` process (`lsof /dev/cu.usbmodem101`) and ask the user to close it (do not kill a process you didn't start without asking, per this project's operating norms).

- [ ] **Step 6: Manually verify provisioning on your phone/laptop (report exact results back to the user — this cannot be automated from the sandboxed shell)**

Ask the user to:
1. Confirm they see a WiFi network named `Lugo-XXXX` in their WiFi list within ~15-20 seconds of the flash finishing.
2. Join it, confirm whether a captive-portal sign-in prompt appears automatically (note their phone OS — behavior varies iOS/Android/desktop).
3. If no prompt, browse to `http://192.168.9.1` manually and confirm the form loads.
4. Submit real WiFi SSID/password + the gateway's real host/port.
5. Confirm the device restarts and (if the gateway is reachable) logs `session ready` — either via `idf.py -C esp32-assistant -p /dev/cu.usbmodem101 monitor` in their own terminal (needs a real TTY, won't work from this sandboxed session), or by describing what they observe.

- [ ] **Step 7: If any step in Step 6 fails, debug with systematic-debugging**

If the AP doesn't appear, the portal doesn't load, or the save/restart doesn't work, invoke the `superpowers:systematic-debugging` skill rather than guessing — this is exactly the kind of "unexpected behavior on hardware" it's meant for, and the failure mode (SoftAP config vs. HTTP server vs. DNS hijack vs. NVS write) needs to be isolated methodically since none of this path is unit-tested.
