# ESP32 Lugo Migration (idle-timeout + barge-in) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the ESP32 firmware from the legacy `/v1/conversation/stream` route to the Lugo device protocol (`/v1/lugo/stream`) so the idle-timeout and barge-in features actually run on the device, and fix the Wake-button barge-in so a press stops playback instantly without an audio glitch.

**Architecture:** This is rollout step 2 of the approved Lugo design (`docs/superpowers/specs/2026-07-08-lugo-device-protocol-design.md`). First we close three server-side gaps in the existing `lugo.py` route that block a real device (output sample-rate negotiation, turn-level `tts` start/stop signalling, and `aborted` on the wire). Then we build a host-tested `lugo_protocol` firmware component (v3 frame codec + JSON build/parse) and rewire `ws_client`, `audio`, `opus_codec`, and the `main.c` state machine onto it — including an immediate local playback stop on barge-in and a `goodbye`-driven sleep.

**Tech Stack:** Python 3.12 / FastAPI / pytest (gateway); C11 / ESP-IDF / FreeRTOS (firmware); host C test harness compiled with `cc` via `esp32-assistant/test/Makefile`.

## Global Constraints

- Do NOT break the legacy `/v1/conversation/stream` route or the browser client — the 467 existing tests must stay green (regression gate). The `event` shim stays until step 6 of the rollout.
- Lugo wire format is fixed by the spec: JSON control on **text** frames (`{"type": ...}`), Opus audio on **binary** frames wrapped in the v3 header (4 bytes: `uint8 type; uint8 reserved; uint16 payload_size` big-endian; `type` 0 = OPUS).
- The device sends only a profile id (`CONFIG_AA_PROFILE`); the server resolves STT/TTS/LLM/language. No engine/voice query params on the Lugo wire.
- ESP32 Opus codec runs at 16000 Hz fixed (`OPUS_DOWN_RATE`/`OPUS_UP_RATE` in `opus_codec.h`); the device must negotiate a 16000 Hz downlink, not the server's 24000 Hz default.
- Firmware C: match existing style in `esp32-assistant/components/` — no dynamic allocation in parse/build helpers, fixed-size buffers, host-testable pure functions separated from hardware.
- Run gateway tests with the repo `.venv` (Python 3.12): `.venv/bin/pytest`.

---

## File Structure

**Gateway (server):**
- Modify: `apps/api_gateway/app/api/routes/lugo.py` — output sample-rate negotiation; turn-level `tts` semantics; `aborted` → `tts{stop}`.
- Modify: `tests/unit/test_lugo_stream.py`, `tests/unit/test_lugo_barge_in.py` — new assertions.

**Firmware (`esp32-assistant/`):**
- Create: `components/lugo_protocol/lugo_protocol.c`, `components/lugo_protocol/include/lugo_protocol.h`, `components/lugo_protocol/CMakeLists.txt` — v3 frame codec + Lugo JSON build/parse (pure, host-testable).
- Create: `test/test_lugo_protocol.c` — host test; add to `test/Makefile`.
- Modify: `components/audio/audio.c`, `components/audio/include/audio.h` — `audio_spk_reset()` (clear I2S DMA).
- Modify: `components/opus_codec/opus_codec.c`, `components/opus_codec/include/opus_codec.h` — `opus_codec_reset()` (reset decoder state).
- Modify: `components/ws_client/ws_client.c`, `components/ws_client/include/ws_client.h` — switch to `lugo_protocol`; send `wakeup` on connect; v3-decode downlink; `ws_client_send_abort()`/`_send_wakeup()`.
- Modify: `main/main.c` — FSM on Lugo events (`welcome`/`tts start|stop`/`goodbye`), barge-in immediate local stop, fixed `s_active`, secondary idle watchdog.
- Modify: `main/Kconfig.projbuild` — `AA_PROFILE` required; drop `AA_STT_ENGINE`/`AA_TTS_ENGINE` if present.
- Modify: `main/CMakeLists.txt` — depend on `lugo_protocol` instead of `ws_protocol`.

---

## PHASE A — Server prerequisites (gateway, fully test-driven)

### Task A1: Lugo honors the device's requested output sample rate

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py:92-129`
- Test: `tests/unit/test_lugo_stream.py`

**Interfaces:**
- Consumes: `wakeup` frame `audio_params.output_sample_rate` (int, optional).
- Produces: `welcome.audio_params.sample_rate` equals the requested output rate (default 24000 when absent). Downlink Opus is synthesized at that rate.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_lugo_stream.py`:

```python
def test_welcome_honors_requested_output_sample_rate(client):
    with client.websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({
            "type": "wakeup",
            "profile": None,
            "audio_params": {"sample_rate": 16000, "output_sample_rate": 16000},
        })
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        assert welcome["audio_params"]["sample_rate"] == 16000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_lugo_stream.py::test_welcome_honors_requested_output_sample_rate -v`
Expected: FAIL — `assert 24000 == 16000` (out_sr is hardcoded to 24000).

- [ ] **Step 3: Implement**

In `lugo.py`, replace the hardcoded `out_sr = 24000` (currently at line 97) with:

```python
    try:
        out_sr = int((hello.get("audio_params") or {}).get("output_sample_rate", 24000))
    except (TypeError, ValueError):
        out_sr = 24000
```

The existing `welcome` send (lines 126-129) already reports `"audio_params": {"sample_rate": out_sr}`, and `SessionRuntimeConfig(..., output_sample_rate=out_sr, ...)` already uses it — no other change needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_lugo_stream.py -v`
Expected: PASS (including the existing `welcome` tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/lugo.py tests/unit/test_lugo_stream.py
git commit -m "feat(lugo): negotiate downlink sample rate from wakeup audio_params"
```

---

### Task A2: Turn-level `tts` start/stop on the Lugo wire (+ `aborted` → stop)

**Why:** The core emits `audio_start`/`audio_end` **per sentence chunk** (`session.py:383,401`) and `turn_done` only at end of turn (`session.py:431,487`), but `turn_done`/`aborted` are dropped by the current `emit()` and `audio_end` maps to `stop` per-chunk. A device FSM needs exactly one `tts{start}` when the bot starts speaking and one `tts{stop}` when the whole turn ends (or is aborted), so it knows when to reopen the mic.

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py:33-119`
- Test: `tests/unit/test_lugo_stream.py`, `tests/unit/test_lugo_barge_in.py`

**Interfaces:**
- Produces on the wire, per turn with audio: exactly one `{"type":"tts","state":"start"}`, then `{"type":"tts","state":"sentence_start","text":...}` per sentence, then exactly one `{"type":"tts","state":"stop"}` at `turn_done` or `aborted`. `user_transcript` → `{"type":"stt",...}` unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_lugo_stream.py` (reuse whatever fixture the existing one-turn test uses to drive a text turn; mirror its structure):

```python
def test_tts_start_and_stop_bracket_a_turn(client):
    with client.websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": None,
                      "audio_params": {"sample_rate": 16000, "output_sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "xin chào"})
        states = []
        for _ in range(20):
            msg = ws.receive_json()
            if msg.get("type") == "tts":
                states.append(msg["state"])
            if msg.get("type") == "tts" and msg["state"] == "stop":
                break
        assert states[0] == "start"
        assert states[-1] == "stop"
        assert states.count("start") == 1
        assert states.count("stop") == 1
```

Add to `tests/unit/test_lugo_barge_in.py` (mirror the existing abort test's setup):

```python
def test_abort_emits_tts_stop(client):
    with client.websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": None,
                      "audio_params": {"sample_rate": 16000, "output_sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "kể một câu chuyện dài"})
        # Wait until the bot has started speaking.
        while ws.receive_json().get("state") != "start":
            pass
        ws.send_json({"type": "abort", "reason": "user"})
        # A tts stop must arrive after the abort.
        saw_stop = any(ws.receive_json().get("state") == "stop" for _ in range(10))
        assert saw_stop
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_lugo_stream.py::test_tts_start_and_stop_bracket_a_turn tests/unit/test_lugo_barge_in.py::test_abort_emits_tts_stop -v`
Expected: FAIL — current code emits `stop` per chunk (multiple) and never on abort.

- [ ] **Step 3: Implement**

In `lugo.py`, delete the `_TTS_STATE` constant (line 34) and replace the `emit` function (lines 107-119) with a turn-state-tracking version:

```python
    speaking = False  # one tts{start} per turn, one tts{stop} at turn end/abort

    async def emit(event: str, **payload) -> None:
        nonlocal speaking
        if event == "user_transcript":
            await websocket.send_json({"type": "stt", "text": payload.get("text", ""), "final": True})
        elif event == "response_text":
            await websocket.send_json({"type": "tts", "state": "sentence_start", "text": payload.get("text", "")})
        elif event == "audio_start":
            if not speaking:
                speaking = True
                await websocket.send_json({"type": "tts", "state": "start"})
        elif event in ("turn_done", "aborted"):
            if speaking:
                speaking = False
                await websocket.send_json({"type": "tts", "state": "stop"})
        elif event == "command":
            await websocket.send_json({"type": "mcp", **payload})
        elif event == "error":
            await websocket.send_json({"type": "error", "message": payload.get("message", "")})
        # session_started / processing / engines_ready / speech_* / audio_end / reset: not on the wire
```

Note: `speaking` must be defined before `session = ConversationSession(cfg, emit, emit_audio)` (line 124); place it just above the `emit` definition.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_lugo_stream.py tests/unit/test_lugo_barge_in.py tests/unit/test_lugo_idle_timeout.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite (regression gate)**

Run: `.venv/bin/pytest -q`
Expected: all green (467+).

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/lugo.py tests/unit/test_lugo_stream.py tests/unit/test_lugo_barge_in.py
git commit -m "feat(lugo): turn-level tts start/stop on the wire, stop on abort"
```

---

## PHASE B — Firmware Lugo protocol component (host-tested)

### Task B1: `lugo_protocol` component skeleton + v3 frame codec

**Files:**
- Create: `esp32-assistant/components/lugo_protocol/include/lugo_protocol.h`
- Create: `esp32-assistant/components/lugo_protocol/lugo_protocol.c`
- Create: `esp32-assistant/components/lugo_protocol/CMakeLists.txt`
- Create: `esp32-assistant/test/test_lugo_protocol.c`
- Modify: `esp32-assistant/test/Makefile`

**Interfaces:**
- Produces:
  - `#define LUGO_FRAME_OPUS 0`, `#define LUGO_FRAME_JSON 1`
  - `int lugo_frame_encode(uint8_t type, const uint8_t *payload, int len, uint8_t *out, int out_cap);` — writes 4-byte header + payload to `out`; returns total bytes or `-1` on overflow/`len>0xFFFF`.
  - `int lugo_frame_decode(const uint8_t *data, int len, uint8_t *out_type, const uint8_t **payload, int *payload_len);` — returns `0` on success (sets `*out_type`, `*payload` into `data`, `*payload_len`), `-1` on short/mismatched frame.

- [ ] **Step 1: Write the header**

`esp32-assistant/components/lugo_protocol/include/lugo_protocol.h`:

```c
#pragma once
#include <stdint.h>
#include <stddef.h>

// v3 binary frame: uint8 type; uint8 reserved; uint16 payload_size (big-endian); payload[]
#define LUGO_FRAME_HEADER 4
#define LUGO_FRAME_OPUS   0
#define LUGO_FRAME_JSON   1

// Encode header+payload into out (cap out_cap). Returns total bytes, or -1 on
// overflow or payload > 0xFFFF.
int lugo_frame_encode(uint8_t type, const uint8_t *payload, int len,
                      uint8_t *out, int out_cap);

// Decode a v3 frame in-place. On success returns 0 and sets *out_type,
// *payload (points into data), *payload_len. Returns -1 if data is shorter than
// the header or the declared size doesn't match the actual payload length.
int lugo_frame_decode(const uint8_t *data, int len, uint8_t *out_type,
                      const uint8_t **payload, int *payload_len);
```

- [ ] **Step 2: Write the failing host test**

`esp32-assistant/test/test_lugo_protocol.c`:

```c
#include "lugo_protocol.h"
#include <assert.h>
#include <string.h>
#include <stdio.h>

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
  printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static void test_frame_roundtrip(void) {
    uint8_t payload[3] = {0xAA, 0xBB, 0xCC};
    uint8_t buf[16];
    int n = lugo_frame_encode(LUGO_FRAME_OPUS, payload, 3, buf, sizeof buf);
    CHECK(n == 7);
    CHECK(buf[0] == LUGO_FRAME_OPUS);
    CHECK(buf[1] == 0);
    CHECK(buf[2] == 0 && buf[3] == 3);   // big-endian size
    uint8_t type; const uint8_t *p; int plen;
    CHECK(lugo_frame_decode(buf, n, &type, &p, &plen) == 0);
    CHECK(type == LUGO_FRAME_OPUS);
    CHECK(plen == 3);
    CHECK(memcmp(p, payload, 3) == 0);
}

static void test_frame_bad(void) {
    uint8_t type; const uint8_t *p; int plen;
    uint8_t two[2] = {0, 0};
    CHECK(lugo_frame_decode(two, 2, &type, &p, &plen) == -1);   // shorter than header
    uint8_t bad[6] = {0, 0, 0, 5, 1, 2};                        // says 5, has 2
    CHECK(lugo_frame_decode(bad, 6, &type, &p, &plen) == -1);
    uint8_t small[2];
    CHECK(lugo_frame_encode(LUGO_FRAME_OPUS, (const uint8_t *)"xy", 2, small, 2) == -1);  // no room
}

int main(void) {
    test_frame_roundtrip();
    test_frame_bad();
    if (failures) { printf("%d failure(s)\n", failures); return 1; }
    printf("all lugo_protocol tests passed\n");
    return 0;
}
```

- [ ] **Step 3: Wire the test into the Makefile**

In `esp32-assistant/test/Makefile`: add the include path `-I../components/lugo_protocol/include` to `CFLAGS`, add `SRC_LUGO_PROTOCOL = ../components/lugo_protocol/lugo_protocol.c`, add `test_lugo_protocol` to the `test:` target's prerequisites and run line, and add the build rule:

```make
test_lugo_protocol: test_lugo_protocol.c $(SRC_LUGO_PROTOCOL)
	$(CC) $(CFLAGS) -o $@ $^
```

Also add `test_lugo_protocol test_lugo_protocol.dSYM` to the `clean:` rule.

- [ ] **Step 4: Run to verify it fails (link error)**

Run: `cd esp32-assistant/test && make test_lugo_protocol`
Expected: FAIL — undefined `lugo_frame_encode`/`lugo_frame_decode` (no `.c` yet).

- [ ] **Step 5: Implement the codec**

`esp32-assistant/components/lugo_protocol/lugo_protocol.c` (frame codec section — JSON added in B2):

```c
#include "lugo_protocol.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

int lugo_frame_encode(uint8_t type, const uint8_t *payload, int len,
                      uint8_t *out, int out_cap) {
    if (len < 0 || len > 0xFFFF) return -1;
    if (out_cap < LUGO_FRAME_HEADER + len) return -1;
    out[0] = type;
    out[1] = 0;
    out[2] = (uint8_t)((len >> 8) & 0xFF);
    out[3] = (uint8_t)(len & 0xFF);
    if (len > 0) memcpy(out + LUGO_FRAME_HEADER, payload, (size_t)len);
    return LUGO_FRAME_HEADER + len;
}

int lugo_frame_decode(const uint8_t *data, int len, uint8_t *out_type,
                      const uint8_t **payload, int *payload_len) {
    if (len < LUGO_FRAME_HEADER) return -1;
    int size = (data[2] << 8) | data[3];
    if (size != len - LUGO_FRAME_HEADER) return -1;
    *out_type = data[0];
    *payload = data + LUGO_FRAME_HEADER;
    *payload_len = size;
    return 0;
}
```

- [ ] **Step 6: Write the component CMakeLists**

`esp32-assistant/components/lugo_protocol/CMakeLists.txt`:

```cmake
idf_component_register(SRCS "lugo_protocol.c"
                       INCLUDE_DIRS "include")
```

- [ ] **Step 7: Run to verify pass**

Run: `cd esp32-assistant/test && make test_lugo_protocol && ./test_lugo_protocol`
Expected: `all lugo_protocol tests passed`.

- [ ] **Step 8: Commit**

```bash
git add esp32-assistant/components/lugo_protocol esp32-assistant/test/test_lugo_protocol.c esp32-assistant/test/Makefile
git commit -m "feat(esp32): lugo_protocol component with v3 frame codec (host-tested)"
```

---

### Task B2: Lugo JSON parse + build

**Files:**
- Modify: `esp32-assistant/components/lugo_protocol/include/lugo_protocol.h`
- Modify: `esp32-assistant/components/lugo_protocol/lugo_protocol.c`
- Modify: `esp32-assistant/test/test_lugo_protocol.c`

**Interfaces:**
- Produces:
  - `typedef enum { LUGO_EV_WELCOME, LUGO_EV_STT, LUGO_EV_TTS_START, LUGO_EV_TTS_SENTENCE, LUGO_EV_TTS_STOP, LUGO_EV_MCP, LUGO_EV_GOODBYE, LUGO_EV_ERROR, LUGO_EV_UNKNOWN } lugo_ev_type_t;`
  - `typedef struct { lugo_ev_type_t type; char text[256]; int sample_rate; int idle_timeout_s; } lugo_event_t;`
  - `int lugo_parse_event(const char *json, lugo_event_t *out);` — returns 0 (sets fields), -1 if not a JSON object.
  - `int lugo_build_wakeup(char *buf, int buflen, const char *profile, int in_sr, int out_sr, int frame_ms);`
  - `int lugo_build_abort(char *buf, int buflen, const char *reason);`
  - `int lugo_build_text(char *buf, int buflen, const char *text);`
  - Each build fn returns bytes written (excluding NUL) or -1 on overflow.

- [ ] **Step 1: Extend the header**

Append to `lugo_protocol.h`:

```c
typedef enum {
    LUGO_EV_WELCOME, LUGO_EV_STT, LUGO_EV_TTS_START, LUGO_EV_TTS_SENTENCE,
    LUGO_EV_TTS_STOP, LUGO_EV_MCP, LUGO_EV_GOODBYE, LUGO_EV_ERROR, LUGO_EV_UNKNOWN
} lugo_ev_type_t;

typedef struct {
    lugo_ev_type_t type;
    char text[256];       // stt/sentence text, error message, or goodbye reason
    int  sample_rate;     // welcome: audio_params.sample_rate
    int  idle_timeout_s;  // welcome
} lugo_event_t;

// Parse a Lugo text frame. Returns 0 on success (type=UNKNOWN for unrecognized),
// -1 if the payload isn't a JSON object.
int lugo_parse_event(const char *json, lugo_event_t *out);

// Builders. Return bytes written (excluding NUL), or -1 on overflow.
int lugo_build_wakeup(char *buf, int buflen, const char *profile,
                      int in_sr, int out_sr, int frame_ms);
int lugo_build_abort(char *buf, int buflen, const char *reason);
int lugo_build_text(char *buf, int buflen, const char *text);
```

- [ ] **Step 2: Write the failing tests**

Append to `test_lugo_protocol.c` (and call them from `main`):

```c
static void test_parse_welcome(void) {
    lugo_event_t e;
    int rc = lugo_parse_event(
        "{\"type\":\"welcome\",\"session_id\":\"x\","
        "\"audio_params\":{\"sample_rate\":16000},\"idle_timeout_s\":30}", &e);
    CHECK(rc == 0);
    CHECK(e.type == LUGO_EV_WELCOME);
    CHECK(e.sample_rate == 16000);
    CHECK(e.idle_timeout_s == 30);
}

static void test_parse_tts_states(void) {
    lugo_event_t e;
    CHECK(lugo_parse_event("{\"type\":\"tts\",\"state\":\"start\"}", &e) == 0);
    CHECK(e.type == LUGO_EV_TTS_START);
    CHECK(lugo_parse_event("{\"type\":\"tts\",\"state\":\"stop\"}", &e) == 0);
    CHECK(e.type == LUGO_EV_TTS_STOP);
    CHECK(lugo_parse_event("{\"type\":\"tts\",\"state\":\"sentence_start\",\"text\":\"chào\"}", &e) == 0);
    CHECK(e.type == LUGO_EV_TTS_SENTENCE);
    CHECK(strcmp(e.text, "chào") == 0);
}

static void test_parse_stt_goodbye_error(void) {
    lugo_event_t e;
    CHECK(lugo_parse_event("{\"type\":\"stt\",\"text\":\"xin chao\",\"final\":true}", &e) == 0);
    CHECK(e.type == LUGO_EV_STT);
    CHECK(strcmp(e.text, "xin chao") == 0);
    CHECK(lugo_parse_event("{\"type\":\"goodbye\",\"reason\":\"idle_timeout\"}", &e) == 0);
    CHECK(e.type == LUGO_EV_GOODBYE);
    CHECK(strcmp(e.text, "idle_timeout") == 0);
    CHECK(lugo_parse_event("{\"type\":\"error\",\"message\":\"boom\"}", &e) == 0);
    CHECK(e.type == LUGO_EV_ERROR);
    CHECK(strcmp(e.text, "boom") == 0);
}

static void test_build_wakeup_and_controls(void) {
    char buf[256];
    int n = lugo_build_wakeup(buf, sizeof buf, "esp32-assistant", 16000, 16000, 60);
    CHECK(n > 0);
    CHECK(strstr(buf, "\"type\":\"wakeup\"") != NULL);
    CHECK(strstr(buf, "\"profile\":\"esp32-assistant\"") != NULL);
    CHECK(strstr(buf, "\"sample_rate\":16000") != NULL);
    CHECK(strstr(buf, "\"output_sample_rate\":16000") != NULL);
    CHECK(lugo_build_abort(buf, sizeof buf, "user") > 0);
    CHECK(strstr(buf, "\"type\":\"abort\"") != NULL);
    CHECK(strstr(buf, "\"reason\":\"user\"") != NULL);
    CHECK(lugo_build_text(buf, sizeof buf, "hi \"there\"") > 0);
    CHECK(strstr(buf, "\\\"there\\\"") != NULL);   // quotes escaped
    CHECK(lugo_build_abort(buf, 4, "user") == -1); // overflow
}
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd esp32-assistant/test && make test_lugo_protocol`
Expected: FAIL — undefined `lugo_parse_event`/`lugo_build_*`.

- [ ] **Step 4: Implement parse + build**

Append to `lugo_protocol.c`. The JSON helpers are copied verbatim from `components/ws_protocol/ws_protocol.c:6-54` (`skip_ws`, `find_value`, `get_string`, `get_int`) and the escaping builder from `ws_protocol.c:103-130` (`append_escaped`) — keep them `static`:

```c
// ---- minimal JSON helpers (same approach as ws_protocol.c) ----
static const char *skip_ws(const char *p) {
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    return p;
}
static const char *find_value(const char *json, const char *key) {
    char pat[64];
    int n = snprintf(pat, sizeof pat, "\"%s\"", key);
    if (n < 0 || (size_t)n >= sizeof pat) return NULL;
    const char *p = strstr(json, pat);
    if (!p) return NULL;
    p = skip_ws(p + n);
    if (*p != ':') return NULL;
    return skip_ws(p + 1);
}
static void get_string(const char *json, const char *key, char *out, size_t cap) {
    if (cap == 0) return;
    out[0] = '\0';
    const char *p = find_value(json, key);
    if (!p || *p != '"') return;
    p++;
    size_t o = 0;
    while (*p && *p != '"') {
        char c = *p;
        if (c == '\\' && p[1]) {
            p++;
            switch (*p) {
                case 'n': c = '\n'; break; case 't': c = '\t'; break;
                case 'r': c = '\r'; break; case 'b': c = '\b'; break;
                case 'f': c = '\f'; break; default: c = *p; break;
            }
        }
        if (o < cap - 1) out[o++] = c;
        p++;
    }
    out[o] = '\0';
}
static int get_int(const char *json, const char *key) {
    const char *p = find_value(json, key);
    if (!p) return 0;
    char *end;
    long v = strtol(p, &end, 10);
    return end == p ? 0 : (int)v;
}

int lugo_parse_event(const char *json, lugo_event_t *out) {
    memset(out, 0, sizeof(*out));
    if (*skip_ws(json) != '{') return -1;
    char type[32];
    get_string(json, "type", type, sizeof type);
    if (!strcmp(type, "welcome")) {
        out->type = LUGO_EV_WELCOME;
        out->sample_rate = get_int(json, "sample_rate");   // inside audio_params; flat scan is fine
        out->idle_timeout_s = get_int(json, "idle_timeout_s");
    } else if (!strcmp(type, "stt")) {
        out->type = LUGO_EV_STT;
        get_string(json, "text", out->text, sizeof out->text);
    } else if (!strcmp(type, "tts")) {
        char state[24];
        get_string(json, "state", state, sizeof state);
        if (!strcmp(state, "start")) out->type = LUGO_EV_TTS_START;
        else if (!strcmp(state, "stop")) out->type = LUGO_EV_TTS_STOP;
        else if (!strcmp(state, "sentence_start")) {
            out->type = LUGO_EV_TTS_SENTENCE;
            get_string(json, "text", out->text, sizeof out->text);
        } else out->type = LUGO_EV_UNKNOWN;
    } else if (!strcmp(type, "mcp")) {
        out->type = LUGO_EV_MCP;
    } else if (!strcmp(type, "goodbye")) {
        out->type = LUGO_EV_GOODBYE;
        get_string(json, "reason", out->text, sizeof out->text);
    } else if (!strcmp(type, "error")) {
        out->type = LUGO_EV_ERROR;
        get_string(json, "message", out->text, sizeof out->text);
    } else {
        out->type = LUGO_EV_UNKNOWN;
    }
    return 0;
}

static bool append_escaped(char *buf, size_t cap, size_t *o, const char *src) {
    static const char hex[] = "0123456789abcdef";
    for (; *src; src++) {
        unsigned char ch = (unsigned char)*src;
        const char *esc = NULL;
        switch (ch) {
            case '"':  esc = "\\\""; break;
            case '\\': esc = "\\\\"; break;
            case '\n': esc = "\\n";  break;
            case '\t': esc = "\\t";  break;
            case '\r': esc = "\\r";  break;
        }
        if (esc) {
            if (*o + 2 >= cap) return false;
            buf[(*o)++] = esc[0]; buf[(*o)++] = esc[1];
        } else if (ch < 0x20) {
            if (*o + 6 >= cap) return false;
            buf[(*o)++] = '\\'; buf[(*o)++] = 'u';
            buf[(*o)++] = '0';  buf[(*o)++] = '0';
            buf[(*o)++] = hex[(ch >> 4) & 0xF];
            buf[(*o)++] = hex[ch & 0xF];
        } else {
            if (*o + 1 >= cap) return false;
            buf[(*o)++] = ch;
        }
    }
    return true;
}

int lugo_build_wakeup(char *buf, int buflen, const char *profile,
                      int in_sr, int out_sr, int frame_ms) {
    int n = snprintf(buf, buflen,
        "{\"type\":\"wakeup\",\"profile\":\"%s\",\"trigger\":\"button\","
        "\"audio_params\":{\"format\":\"opus\",\"sample_rate\":%d,"
        "\"output_sample_rate\":%d,\"frame_duration\":%d}}",
        profile ? profile : "", in_sr, out_sr, frame_ms);
    if (n < 0 || n >= buflen) return -1;
    return n;
}

int lugo_build_abort(char *buf, int buflen, const char *reason) {
    int n = snprintf(buf, buflen, "{\"type\":\"abort\",\"reason\":\"%s\"}",
                     reason ? reason : "user");
    if (n < 0 || n >= buflen) return -1;
    return n;
}

int lugo_build_text(char *buf, int buflen, const char *text) {
    const char *prefix = "{\"type\":\"text\",\"text\":\"";
    const char *suffix = "\"}";
    size_t o = 0;
    size_t plen = strlen(prefix), slen = strlen(suffix);
    if ((int)(plen + slen) >= buflen) return -1;
    memcpy(buf, prefix, plen); o = plen;
    if (!append_escaped(buf, (size_t)buflen - slen, &o, text)) return -1;
    memcpy(buf + o, suffix, slen); o += slen;
    buf[o] = '\0';
    return (int)o;
}
```

Add `#include <stdbool.h>` to the top of `lugo_protocol.c`.

- [ ] **Step 5: Run to verify pass**

Run: `cd esp32-assistant/test && make test_lugo_protocol && ./test_lugo_protocol`
Expected: `all lugo_protocol tests passed`.

- [ ] **Step 6: Commit**

```bash
git add esp32-assistant/components/lugo_protocol esp32-assistant/test/test_lugo_protocol.c
git commit -m "feat(esp32): lugo_protocol JSON parse + wakeup/abort/text builders"
```

---

## PHASE C — Firmware integration (compile-gated; final verification on hardware)

### Task C1: `audio_spk_reset()` and `opus_codec_reset()`

**Files:**
- Modify: `esp32-assistant/components/audio/audio.c`, `esp32-assistant/components/audio/include/audio.h`
- Modify: `esp32-assistant/components/opus_codec/opus_codec.c`, `esp32-assistant/components/opus_codec/include/opus_codec.h`

**Interfaces:**
- Produces: `void audio_spk_reset(void);` (drops queued I2S DMA so a barge-in cuts playback immediately) and `void opus_codec_reset(void);` (resets the decoder so the next turn doesn't inherit mid-stream state → no glitch).

- [ ] **Step 1: Add `audio_spk_reset` declaration**

In `audio.h`, after `int audio_spk_write(...)`:

```c
// Drop any audio already committed to the I2S TX DMA (barge-in): the current
// utterance stops within one DMA buffer instead of playing out. Safe to call
// from any task; serialized against audio_spk_write via the TX mutex.
void audio_spk_reset(void);
```

- [ ] **Step 2: Implement `audio_spk_reset`**

In `audio.c` (uses the existing `s_tx` channel handle and `s_tx_mutex` already used by `audio_spk_write`). Add after `audio_spk_write`:

```c
void audio_spk_reset(void) {
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    // Disable+re-enable the TX channel to discard the DMA buffer contents; a
    // plain zero-write would still play the already-queued tail first.
    i2s_channel_disable(s_tx);
    i2s_channel_enable(s_tx);
    xSemaphoreGive(s_tx_mutex);
}
```

- [ ] **Step 3: Add `opus_codec_reset`**

In `opus_codec.h`, after `int opus_codec_decode(...)`:

```c
// Reset the decoder's internal state (call on barge-in/turn abort so a new
// reply doesn't decode against stale inter-frame state, which clicks/warbles).
void opus_codec_reset(void);
```

In `opus_codec.c`, using the existing decoder handle (find the `OpusDecoder *` static, e.g. `s_dec`):

```c
void opus_codec_reset(void) {
    if (s_dec) opus_decoder_ctl(s_dec, OPUS_RESET_STATE);
}
```

(If the static is named differently, match the actual name in `opus_codec.c`.)

- [ ] **Step 4: Verify compile via host is not possible (hardware headers); defer to C5 build**

These call ESP-IDF driver functions (`i2s_channel_disable`, `opus_decoder_ctl`) not available to the host `cc` harness. Compilation is verified by the full `idf.py build` in Task C5. No host test here.

- [ ] **Step 5: Commit**

```bash
git add esp32-assistant/components/audio esp32-assistant/components/opus_codec
git commit -m "feat(esp32): audio_spk_reset + opus_codec_reset for clean barge-in"
```

---

### Task C2: `ws_client` on Lugo — wakeup handshake, v3 downlink decode, abort builder

**Files:**
- Modify: `esp32-assistant/components/ws_client/ws_client.c`, `esp32-assistant/components/ws_client/include/ws_client.h`
- Modify: `esp32-assistant/components/ws_client/CMakeLists.txt` (depend on `lugo_protocol` instead of `ws_protocol`)

**Interfaces:**
- Consumes: `lugo_frame_decode`, `lugo_parse_event`, `lugo_build_wakeup`, `lugo_build_abort` (Phase B); `lugo_event_t`.
- Produces (new `ws_client.h`):
  - `typedef void (*ws_event_cb_t)(const lugo_event_t *ev);`
  - `typedef void (*ws_audio_cb_t)(const uint8_t *opus, int len);`
  - `esp_err_t ws_client_start(const char *host, int port, bool secure, const char *profile, int in_sr, int out_sr, int frame_ms, ws_event_cb_t on_event, ws_audio_cb_t on_audio);` — connects to `/v1/lugo/stream`, sends `wakeup` on WS-connected.
  - `int ws_client_send_audio(const uint8_t *opus, int len);` — wraps payload in a v3 OPUS frame before sending.
  - `int ws_client_send_abort(const char *reason);`
  - `bool ws_client_connected(void);`

- [ ] **Step 1: Rewrite `ws_client.h`**

```c
#pragma once
#include "esp_err.h"
#include "lugo_protocol.h"
#include <stdbool.h>
#include <stdint.h>

typedef void (*ws_event_cb_t)(const lugo_event_t *ev);
typedef void (*ws_audio_cb_t)(const uint8_t *opus, int len);

esp_err_t ws_client_start(const char *host, int port, bool secure,
                          const char *profile, int in_sr, int out_sr, int frame_ms,
                          ws_event_cb_t on_event, ws_audio_cb_t on_audio);
int  ws_client_send_audio(const uint8_t *opus, int len);
int  ws_client_send_abort(const char *reason);
bool ws_client_connected(void);
```

- [ ] **Step 2: Update the URI builder**

In `ws_client.c`, build the connect URI as `"%s://%s:%d/v1/lugo/stream"` (scheme from `secure`). No query params. Store `profile`, `in_sr`, `out_sr`, `frame_ms` in static state for the wakeup send.

- [ ] **Step 3: Send `wakeup` on connect**

In the `WEBSOCKET_EVENT_CONNECTED` handler, build and send the wakeup text frame:

```c
    case WEBSOCKET_EVENT_CONNECTED: {
        char buf[256];
        int n = lugo_build_wakeup(buf, sizeof buf, s_profile, s_in_sr, s_out_sr, s_frame_ms);
        if (n > 0) esp_websocket_client_send_text(s_client, buf, n, portMAX_DELAY);
        break;
    }
```

- [ ] **Step 4: Decode downlink binary as v3 frames; parse text as Lugo events**

Replace the `WEBSOCKET_EVENT_DATA` body (currently `ws_client.c:31-...`) so binary frames are v3-decoded and only OPUS payloads reach `on_audio`, and text frames are parsed via `lugo_parse_event`:

```c
    case WEBSOCKET_EVENT_DATA: {
        esp_websocket_event_data_t *d = (esp_websocket_event_data_t *)event_data;
        if (d->op_code == 0x02) {            // binary = v3 frame
            uint8_t type; const uint8_t *payload; int plen;
            if (lugo_frame_decode((const uint8_t *)d->data_ptr, d->data_len,
                                  &type, &payload, &plen) == 0 &&
                type == LUGO_FRAME_OPUS && s_on_audio && plen > 0) {
                s_on_audio(payload, plen);
            }
        } else if (d->op_code == 0x01) {     // text = one Lugo JSON event
            char buf[512];
            int n = d->data_len < (int)sizeof(buf) - 1 ? d->data_len : (int)sizeof(buf) - 1;
            memcpy(buf, d->data_ptr, n); buf[n] = '\0';
            lugo_event_t ev;
            if (lugo_parse_event(buf, &ev) == 0 && s_on_event) s_on_event(&ev);
        }
        break;
    }
```

- [ ] **Step 5: v3-wrap uplink audio and add abort builder**

```c
int ws_client_send_audio(const uint8_t *opus, int len) {
    if (!s_client || len <= 0) return -1;
    uint8_t frame[LUGO_FRAME_HEADER + OPUS_MAX_PACKET];
    int n = lugo_frame_encode(LUGO_FRAME_OPUS, opus, len, frame, sizeof frame);
    if (n < 0) return -1;
    return esp_websocket_client_send_bin(s_client, (const char *)frame, n, portMAX_DELAY);
}

int ws_client_send_abort(const char *reason) {
    if (!s_client) return -1;
    char buf[64];
    int n = lugo_build_abort(buf, sizeof buf, reason);
    if (n < 0) return -1;
    return esp_websocket_client_send_text(s_client, buf, n, portMAX_DELAY);
}
```

Add `#include "opus_codec.h"` for `OPUS_MAX_PACKET`. (Server Phase 1 accepts raw or v3-wrapped uplink — `lugo.py:181-183` feeds `message["bytes"]` to `feed_audio`; if the server does not yet strip a v3 uplink header, send raw opus here instead: `esp_websocket_client_send_bin(s_client, (const char*)opus, len, ...)`. Confirm against `session.feed_audio` during C5 hardware test and pick whichever the server decodes.)

- [ ] **Step 6: Update `ws_client/CMakeLists.txt`**

Change `REQUIRES`/`PRIV_REQUIRES` from `ws_protocol` to `lugo_protocol opus_codec`.

- [ ] **Step 7: Commit**

```bash
git add esp32-assistant/components/ws_client
git commit -m "feat(esp32): ws_client speaks Lugo — wakeup handshake + v3 framing"
```

---

### Task C3: `main.c` FSM on Lugo — barge-in immediate stop, fixed Wake, goodbye→sleep

**Files:**
- Modify: `esp32-assistant/main/main.c`
- Modify: `esp32-assistant/main/CMakeLists.txt` (REQUIRES `lugo_protocol` not `ws_protocol`)

**Interfaces:**
- Consumes: `ws_client_start(host,port,secure,profile,in_sr,out_sr,frame_ms,on_event,on_audio)`, `ws_client_send_abort`, `ws_client_send_audio`, `ws_client_connected`; `lugo_event_t`/`lugo_ev_type_t`; `audio_spk_reset`, `opus_codec_reset`.

- [ ] **Step 1: Swap the event callback to Lugo types**

Replace `on_event(const wsp_event_t *ev)` with `on_event(const lugo_event_t *ev)` and map:
- `LUGO_EV_WELCOME` → `s_state = APP_LISTENING`; if `ev->idle_timeout_s > 0` store it in a `static volatile int s_idle_timeout_s`; queue the "Connected / Press wake to talk" status (as today at `main.c:143-151`).
- `LUGO_EV_TTS_START` → `s_turn_ending = false; s_state = APP_SPEAKING;` (mirrors old `WSP_EV_AUDIO_START`).
- `LUGO_EV_TTS_STOP` → if `s_state == APP_SPEAKING` set `s_turn_ending = true;` else `s_state = APP_LISTENING;` (this is the turn boundary that old code got from `WSP_EV_TURN_DONE`).
- `LUGO_EV_STT` → `ESP_LOGI(TAG, "you: %s", ev->text);`
- `LUGO_EV_TTS_SENTENCE` → `ESP_LOGI(TAG, "bot: %s", ev->text);`
- `LUGO_EV_GOODBYE` → server idle disconnect: `s_active = false; s_state = APP_LISTENING;` flush the playback queue (same loop as old abort) and queue an "Idle / Press wake to talk" status. (WS will close; on reconnect the wakeup handshake re-runs.)
- `LUGO_EV_ERROR` → same error status as today (`main.c:168-174`).
- `LUGO_EV_MCP` / `LUGO_EV_UNKNOWN` → ignore.

- [ ] **Step 2: Fix the Wake button — immediate local stop + correct s_active**

Replace `on_button`'s `BTN_WAKE` case (`main.c:117-129`) with:

```c
    case BTN_WAKE: {
        if (s_state == APP_SPEAKING) {
            // Barge-in: stop the bot NOW, locally, before the network round-trip.
            // Flush queued packets, drop the committed I2S DMA, and reset the
            // decoder so the next turn starts clean (no click/warble). Then tell
            // the server to cancel the turn. The connection stays open and we go
            // straight to LISTENING so the user can speak — do NOT toggle to Idle.
            { pkt_t *p; while (xQueueReceive(s_pktq, &p, 0) == pdTRUE) free(p); }
            audio_spk_reset();
            opus_codec_reset();
            s_turn_ending = false;
            s_state = APP_LISTENING;
            s_active = true;
            ws_client_send_abort("user");
            status_msg_t m = { .play_voice = false, .has_line2 = true };
            strncpy(m.line1, "Listening", sizeof(m.line1) - 1);
            strncpy(m.line2, "Speak now", sizeof(m.line2) - 1);
            xQueueSend(s_status_q, &m, 0);
            break;
        }
        // Not speaking: toggle idle/listening as before.
        s_active = !s_active;
        status_msg_t m = { .play_voice = false, .has_line2 = true };
        strncpy(m.line1, s_active ? "Listening" : "Idle", sizeof(m.line1) - 1);
        strncpy(m.line2, s_active ? "Speak now" : "Press wake to talk",
                sizeof(m.line2) - 1);
        xQueueSend(s_status_q, &m, 0);
        break;
    }
```

This fixes both reported symptoms: playback stops on the press (not after a round-trip), the DMA/decoder reset removes the glitch, and `s_active` stays true so the mic reopens for the barge-in utterance.

- [ ] **Step 3: Secondary idle watchdog on the device**

Add a lightweight watchdog so a silently dropped WS still returns the device to idle. Track `static volatile int64_t s_last_activity_us;` updated whenever audio is received (`on_audio`) or the user is streaming, and in an existing periodic task (or a small new `watchdog_task`) compare `esp_timer_get_time() - s_last_activity_us` against `(s_idle_timeout_s + 5) * 1000000LL`; on expiry set `s_active = false` and queue the Idle status. (Server `goodbye` remains the primary path; this is the backup per spec §"Idle timeout ownership".) Keep it minimal — one `vTaskDelay(1s)` loop.

- [ ] **Step 4: Update `ws_client_start` call in `app_main`**

Replace the `wsp_config_t wcfg = {...}; ws_client_start(&wcfg, on_event, on_audio);` block (`main.c:300-308`) with the new signature:

```c
    ESP_ERROR_CHECK(ws_client_start(
        cfg.server_host, cfg.server_port, CONFIG_AA_SERVER_SECURE,
        CONFIG_AA_PROFILE, 16000, 16000, 60, on_event, on_audio));
```

Remove the now-unused `s_wcfg_host`/`s_wcfg_port` if only the old status screen used them (or keep for the display and set from `cfg`).

- [ ] **Step 5: Update includes / CMakeLists**

In `main.c` replace `#include "ws_protocol.h"` usage with `#include "lugo_protocol.h"` (via `ws_client.h`). In `main/CMakeLists.txt` change the `REQUIRES` list: `lugo_protocol` instead of `ws_protocol` (keep `ws_client audio opus_codec buttons display ...`).

- [ ] **Step 6: Commit**

```bash
git add esp32-assistant/main/main.c esp32-assistant/main/CMakeLists.txt
git commit -m "feat(esp32): FSM on Lugo events + instant barge-in stop + goodbye sleep"
```

---

### Task C4: Kconfig — profile required, drop engine selects

**Files:**
- Modify: `esp32-assistant/main/Kconfig.projbuild`

- [ ] **Step 1: Make `AA_PROFILE` required and drop engine configs**

Ensure `AA_PROFILE` has a non-empty default and a help note that the server resolves STT/TTS/LLM from it. Remove `AA_STT_ENGINE` / `AA_TTS_ENGINE` entries if present (server owns these now). Keep `AA_SERVER_HOST/PORT/SECURE` and pin configs.

- [ ] **Step 2: Commit**

```bash
git add esp32-assistant/main/Kconfig.projbuild
git commit -m "chore(esp32): AA_PROFILE required; drop device-side engine selects"
```

---

### Task C5: Build + retire `ws_protocol` + hardware verification

**Files:**
- Delete (after build is green): `esp32-assistant/components/ws_protocol/` and `esp32-assistant/test/test_ws_protocol.c` (+ its Makefile lines) — only once nothing references it.

- [ ] **Step 1: Full host test run**

Run: `cd esp32-assistant/test && make clean && make test`
Expected: all host suites pass, including `test_lugo_protocol`.

- [ ] **Step 2: Firmware build**

Run: `cd esp32-assistant && idf.py build`
Expected: compiles clean. Fix any signature mismatches surfaced here (this is the compile gate for C1–C4).

- [ ] **Step 3: Retire `ws_protocol`**

Once the build is green and nothing includes `ws_protocol.h`, remove the `ws_protocol` component and its host test, and drop its lines from `test/Makefile`. Rebuild host tests and firmware to confirm.

```bash
git rm -r esp32-assistant/components/ws_protocol esp32-assistant/test/test_ws_protocol.c
# edit test/Makefile to drop ws_protocol lines
cd esp32-assistant && idf.py build && (cd test && make clean && make test)
git add -A && git commit -m "chore(esp32): retire ws_protocol; device is Lugo-only"
```

- [ ] **Step 4: Flash + on-device verification (manual, required)**

Run: `cd esp32-assistant && idf.py flash monitor`
Verify against the two reported bugs:
1. **Idle:** connect, press Wake, stay silent for `idle_timeout_s` (profile default 30s). The device must receive `goodbye{idle_timeout}`, show "Idle", and stop streaming. (Watch the monitor for the goodbye and the server log for the watchdog firing.)
2. **Barge-in:** press Wake to talk, let the bot start a long reply, press Wake mid-speech. Playback must stop **immediately** with no click/warble, the screen shows "Listening / Speak now", and speaking again is transcribed (mic reopened). Confirm the WS stays connected (no reconnect in the monitor).

- [ ] **Step 5: Update the running-notes / memory**

Note in the session that ESP32 is migrated to `/v1/lugo/stream` (rollout step 2 done); agent/RPi/browser (steps 3-5) still on the legacy route.

---

## Self-Review notes

- **Spec coverage:** wakeup/welcome handshake (B2/C2), v3 framing (B1/C2), abort/barge-in keeping the connection (A2/C3), idle goodbye + device watchdog (A1 rate, C3 watchdog), profile-as-identity (C4), retire path (C5). The core `ConversationSession` and `lugo_frame.py` already exist from Phase 1 — not re-created.
- **Sample-rate gap** (server hardcoded 24000 vs device 16000) is closed in A1 and negotiated via wakeup in B2/C2.
- **Turn-boundary gap** (no turn-level stop on the wire) is closed in A2 and consumed by the FSM in C3.
- **Verification honesty:** C1–C4 are compile-gated only (host `cc` can't link ESP-IDF drivers); the real behavioral proof is the manual flash test in C5 Step 4. Do not claim the bugs are fixed before that step passes on hardware.
