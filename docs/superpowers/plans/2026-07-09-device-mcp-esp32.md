# Device MCP — ESP32 Firmware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the ESP32 firmware (`esp32-assistant/`, nested repo) an MCP server that answers `initialize`/`tools/list`/`tools/call` over the existing Lugo WebSocket, with a reusable "register a tool in a few lines" template, so the gateway's LLM can read device status and drive volume, screen, GPIO, idle and shutdown.

**Architecture:** Two new components, mirroring the existing `board` / `boards` split ([[esp32-board-abstraction]]):
- `components/mcp_server` — the registry **engine**: JSON-RPC dispatch, property/argument validation, result serialization. Normal component, no linker tricks, fully host-testable with an injected tool array (like `board_select`).
- `components/mcp_tools` — the **registrants**: one static `mcp_tool_desc_t` per hardware tool, self-registered into an `mcp_tool` linker section via a `LUGO_MCP_TOOL` macro (mirrors `LUGO_BOARD_REGISTER`). WHOLE_ARCHIVE + `linker.lf`, verified by build/target only.

Hardware access goes through the existing board vtables (`speaker_ops_t`, `display_ops_t`) plus one new op (`display_ops_t.set_backlight`) and direct ESP-IDF GPIO calls with a reserved-pin guard (no new board op needed for raw GPIO — it isn't board-specific hardware, it's a generic MCU peripheral).

**Tech Stack:** C11, ESP-IDF (target: ESP32-S3), host-testable pure C via the existing `test/Makefile` harness (plain `cc`, no ESP-IDF needed for logic tests).

## Global Constraints

- Do **not** use the name "xiaozhi" anywhere in code/comments/docs (this is the "Lugo" protocol; see [[lugo-device-protocol]]).
- No cJSON / no new JSON library dependency — this codebase's `lugo_protocol.c` is a hand-rolled flat-object key/value scanner (`find_value`/`get_string`/`get_int`), and every JSON-RPC object this firmware needs to read (`tools/call` arguments, `initialize`/`tools/list` requests) is a **flat** object — no nesting beyond one level. Extend the existing scanner; do not introduce a general JSON parser.
- `mcp` frame envelope (both directions): `{"type":"mcp","payload":{<JSON-RPC 2.0>}}` — text frame, matches the gateway plan (`docs/superpowers/plans/2026-07-09-device-mcp-gateway.md`) exactly.
- Fixed ids: `initialize` request arrives with id=1, `tools/list` with id=2, `tools/call` with a counter id ≥3. The device only ever **echoes back** whatever id it received — it does not allocate ids itself.
- `self.screen.set_brightness` is renamed to **`self.screen.set_backlight(on: bool)`** — the ST7789 backlight (GPIO17) is a plain on/off GPIO with no PWM in `st7789.c`; the tool must reflect real hardware capability, not an invented percentage.
- `self.gpio.set` has **no hardcoded pin**. It validates the requested pin against a reserved list (mic/speaker/display/button pins, both the literal ints in `board_def.c` and the `CONFIG_AA_MIC_*`/`CONFIG_AA_SPK_*` Kconfig pins) and refuses if reserved. `requires_confirm = true`.
- `self.device.shutdown` also has `requires_confirm = true`. `self.device.idle` does not.
- New components need **WHOLE_ARCHIVE** wherever tools self-register via a linker section — the same archive-member-pruning gotcha as `components/boards` (see [[esp32-board-abstraction]]): without it, `--gc-sections` drops object files whose only reference is a linker-section pointer, and the registry is silently empty.
- Host tests use the existing `test/Makefile` pattern (plain `cc`, `-std=c11 -Wall -Wextra`, mock ops injected — no ESP-IDF headers). New host-testable modules get a `SRC_<NAME>` var + `test_<name>` target following the existing entries exactly.
- On-target verification (`idf.py build` then `idf.py flash monitor`) is the human gate for anything involving the linker section / WHOLE_ARCHIVE / real GPIO — host tests cannot prove that part.

---

### Task 1: Expose the MCP payload + a public JSON scanner from `lugo_protocol`

**Files:**
- Modify: `esp32-assistant/components/lugo_protocol/include/lugo_protocol.h`
- Modify: `esp32-assistant/components/lugo_protocol/lugo_protocol.c`
- Test: `esp32-assistant/test/test_lugo_protocol.c` (append)

**Interfaces:**
- Consumes: nothing new (extends the existing frame/event parser).
- Produces:
  - `lugo_event_t` gains a field: `const char *mcp_payload;` — for `LUGO_EV_MCP` events, points at the `{...}` value of the outer `"payload"` key, **inside the caller's own buffer** (same not-copied, borrowed-pointer convention `lugo_frame_decode` already uses for the opus payload). Valid only as long as the caller's JSON string is alive; `mcp_server` (Task 2) must finish using it before the caller's buffer is reused/freed.
  - Three helpers, promoted from `static` to public so `mcp_server` can reuse them instead of writing a second parser:
    - `const char *lugo_json_find(const char *json, const char *key);` (was `find_value`)
    - `void lugo_json_get_string(const char *json, const char *key, char *out, size_t cap);` (was `get_string`)
    - `int lugo_json_get_int(const char *json, const char *key);` (was `get_int`)
  - One new helper: `int lugo_json_get_bool(const char *json, const char *key, int default_val);` — returns 1/0 by matching literal `true`/`false` at the value position, or `default_val` if the key is absent.

- [ ] **Step 1: Write the failing tests**

Append to `esp32-assistant/test/test_lugo_protocol.c` (before the `main()` at the bottom — check the file's existing structure with `grep -n "int main" test/test_lugo_protocol.c` first so the new calls are added to the runner list too):

```c
static void test_mcp_payload_pointer(void) {
    const char *json =
        "{\"type\":\"mcp\",\"payload\":{\"jsonrpc\":\"2.0\",\"id\":3,"
        "\"method\":\"tools/call\",\"params\":{\"name\":\"self.audio.set_volume\","
        "\"arguments\":{\"volume\":70}}}}";
    lugo_event_t e;
    CHECK(lugo_parse_event(json, &e) == 0);
    CHECK(e.type == LUGO_EV_MCP);
    CHECK(e.mcp_payload != NULL);
    CHECK(e.mcp_payload[0] == '{');
    CHECK(lugo_json_get_int(e.mcp_payload, "id") == 3);
    char method[32];
    lugo_json_get_string(e.mcp_payload, "method", method, sizeof method);
    CHECK(strcmp(method, "tools/call") == 0);
}

static void test_json_get_bool(void) {
    CHECK(lugo_json_get_bool("{\"confirm\":true}", "confirm", 0) == 1);
    CHECK(lugo_json_get_bool("{\"confirm\":false}", "confirm", 1) == 0);
    CHECK(lugo_json_get_bool("{\"other\":1}", "confirm", 1) == 1);   // default when absent
    CHECK(lugo_json_get_bool("{\"other\":1}", "confirm", 0) == 0);
}

static void test_json_find_returns_object_pointer(void) {
    const char *p = lugo_json_find("{\"a\":1,\"payload\":{\"x\":5}}", "payload");
    CHECK(p != NULL);
    CHECK(p[0] == '{');
    CHECK(lugo_json_get_int(p, "x") == 5);
}
```

Add the three new function names to the `main()` runner list (same file), alongside the existing `test_frame_roundtrip();` etc. calls.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd esp32-assistant/test && make test_lugo_protocol && ./test_lugo_protocol`
Expected: build FAIL — `lugo_json_get_bool`/`lugo_json_find`/`e.mcp_payload` undeclared.

- [ ] **Step 3: Implement**

In `esp32-assistant/components/lugo_protocol/include/lugo_protocol.h`, add `mcp_payload` to the struct and declare the new public functions:

```c
typedef struct {
    lugo_ev_type_t type;
    char text[256];       // stt/sentence text, error message, or goodbye reason
    int  sample_rate;     // welcome: audio_params.sample_rate
    int  idle_timeout_s;  // welcome
    // For LUGO_EV_MCP: points at the "payload" object's '{' inside the caller's
    // own JSON buffer (borrowed, not copied — same convention as
    // lugo_frame_decode's payload pointer). NULL for all other event types.
    const char *mcp_payload;
} lugo_event_t;
```

```c
// Find the value for top-level "key" in a flat (non-nested-search) scan;
// returns a pointer to the first character of the value, or NULL. Works for
// any JSON value type (object, string, number, bool) — callers combine this
// with lugo_json_get_* or their own object-scoped calls.
const char *lugo_json_find(const char *json, const char *key);

// Copy the string value for key into out (cap-bounded, common escapes
// unescaped). out is "" if the key is absent or not a string.
void lugo_json_get_string(const char *json, const char *key, char *out, size_t cap);

// Read the integer value for key; 0 if absent/non-numeric.
int lugo_json_get_int(const char *json, const char *key);

// Read the boolean value for key ("true"/"false" literal at the value
// position); default_val if the key is absent.
int lugo_json_get_bool(const char *json, const char *key, int default_val);
```

In `esp32-assistant/components/lugo_protocol/lugo_protocol.c`:

1. Rename `find_value` → `lugo_json_find` (drop `static`), `get_string` → `lugo_json_get_string` (drop `static`), `get_int` → `lugo_json_get_int` (drop `static`). Update their two internal call sites (`lugo_parse_event`) to the new names.
2. Add the new function, placed after `lugo_json_get_int`:

```c
int lugo_json_get_bool(const char *json, const char *key, int default_val) {
    const char *p = lugo_json_find(json, key);
    if (!p) return default_val;
    if (!strncmp(p, "true", 4)) return 1;
    if (!strncmp(p, "false", 5)) return 0;
    return default_val;
}
```

3. In `lugo_parse_event`, the `mcp` branch currently just sets the type. Extend it to capture the payload pointer:

```c
    } else if (!strcmp(type, "mcp")) {
        out->type = LUGO_EV_MCP;
        out->mcp_payload = lugo_json_find(json, "payload");
    } else if (!strcmp(type, "goodbye")) {
```

(`out` was already `memset` to 0 at the top of the function, so `mcp_payload` is NULL for every other branch without extra code.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd esp32-assistant/test && make test_lugo_protocol && ./test_lugo_protocol`
Expected: all checks pass, `failures == 0` (matches the file's existing pass/fail reporting convention).

- [ ] **Step 5: Run the full host test suite (no regressions)**

Run: `cd esp32-assistant/test && make test`
Expected: all 6 existing test binaries still pass (the renamed functions are internal-linkage-only changes plus additive API, so `test_lugo_protocol`'s prior assertions on `welcome`/`stt`/`tts`/`goodbye`/`error` parsing are unaffected).

- [ ] **Step 6: Commit**

```bash
cd esp32-assistant
git add components/lugo_protocol/include/lugo_protocol.h components/lugo_protocol/lugo_protocol.c test/test_lugo_protocol.c
git commit -m "feat(lugo_protocol): expose mcp payload pointer + public JSON scanner helpers"
cd ..
git add esp32-assistant
git commit -m "chore: bump esp32-assistant (mcp payload + json helpers)"
```

---

### Task 2: `mcp_server` — registry engine, property system, JSON-RPC dispatch (host-testable)

**Files:**
- Create: `esp32-assistant/components/mcp_server/CMakeLists.txt`
- Create: `esp32-assistant/components/mcp_server/include/mcp_server.h`
- Create: `esp32-assistant/components/mcp_server/mcp_server.c`
- Test: `esp32-assistant/test/test_mcp_server.c`
- Modify: `esp32-assistant/test/Makefile`

**Interfaces:**
- Consumes: `lugo_json_find`/`lugo_json_get_string`/`lugo_json_get_int`/`lugo_json_get_bool` (Task 1).
- Produces (all pure, host-testable — the linker-section walk is Task 3's job, kept out of this component on purpose so this one is fully unit-testable without WHOLE_ARCHIVE):
  - `typedef enum { MCP_PROP_INT, MCP_PROP_BOOL, MCP_PROP_STRING } mcp_prop_type_t;`
  - `typedef struct { const char *name; mcp_prop_type_t type; int min; int max; bool required; } mcp_prop_t;` — `min`/`max` only apply to `MCP_PROP_INT` (0/0 means "no bound"); `MCP_PROP_END` sentinel is `{NULL, 0, 0, 0, false}`.
  - `#define MCP_PROP_INT(n, lo, hi) {(n), MCP_PROP_INT, (lo), (hi), true}`
  - `#define MCP_PROP_BOOL(n) {(n), MCP_PROP_BOOL, 0, 0, true}`
  - `#define MCP_PROP_STRING(n) {(n), MCP_PROP_STRING, 0, 0, true}`
  - `#define MCP_PROP_END {NULL, 0, 0, 0, false}`
  - `typedef struct { bool is_error; char text[192]; } mcp_result_t;`
  - `mcp_result_t mcp_ok_text(const char *fmt, ...);` (printf-style, truncates at 191 chars)
  - `mcp_result_t mcp_err(const char *fmt, ...);`
  - `typedef mcp_result_t (*mcp_tool_fn_t)(const char *args_json);` — `args_json` is a borrowed pointer to the `"arguments":{...}` object's `{`, or `""` if the tool takes no arguments; never NULL.
  - `typedef struct { const char *name; const char *description; const mcp_prop_t *props; bool requires_confirm; mcp_tool_fn_t fn; } mcp_tool_desc_t;`
  - `int mcp_arg_int(const char *args_json, const char *name, int fallback);`
  - `int mcp_arg_bool(const char *args_json, const char *name, int fallback);`
  - `void mcp_arg_string(const char *args_json, const char *name, char *out, size_t cap);`
  - `int mcp_dispatch(const mcp_tool_desc_t *const *tools, int n_tools, const char *mcp_payload, char *out_buf, int out_cap);` — the whole request/response cycle for one `mcp_payload` (the JSON-RPC object from `lugo_event_t.mcp_payload`). Handles `initialize`, `tools/list` (always returns all `n_tools` in one response — no cursor, see Global Constraints), `tools/call` (looks up by name, validates required props against the descriptor, rejects unknown/missing/out-of-range args as a JSON-RPC `error` without calling `fn`, else calls `fn` and wraps its `mcp_result_t`). Writes the full JSON-RPC response (with echoed `id`) into `out_buf`; returns the byte length, or -1 on overflow.

- [ ] **Step 1: Write the failing tests**

```c
// esp32-assistant/test/test_mcp_server.c
#include "mcp_server.h"
#include <assert.h>
#include <string.h>
#include <stdio.h>

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
  printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static int s_last_volume = -1;

static mcp_result_t set_volume_fn(const char *args) {
    int v = mcp_arg_int(args, "volume", -1);
    if (v < 0) return mcp_err("missing volume");
    s_last_volume = v;
    return mcp_ok_text("volume set to %d", v);
}

static mcp_result_t status_fn(const char *args) {
    (void)args;
    return mcp_ok_text("ok");
}

static const mcp_prop_t set_volume_props[] = {
    MCP_PROP_INT("volume", 0, 100), MCP_PROP_END,
};

static const mcp_tool_desc_t set_volume_tool = {
    .name = "self.audio.set_volume", .description = "Set speaker volume (0-100)",
    .props = set_volume_props, .requires_confirm = false, .fn = set_volume_fn,
};
static const mcp_tool_desc_t status_tool = {
    .name = "self.get_device_status", .description = "Read device status",
    .props = NULL, .requires_confirm = false, .fn = status_fn,
};

static const mcp_tool_desc_t *const s_tools[] = { &set_volume_tool, &status_tool };

static void test_initialize(void) {
    char out[256];
    int n = mcp_dispatch(s_tools, 2,
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\"}", out, sizeof out);
    CHECK(n > 0);
    CHECK(strstr(out, "\"id\":1") != NULL);
    CHECK(strstr(out, "\"result\"") != NULL);
}

static void test_tools_list_lists_both(void) {
    char out[512];
    int n = mcp_dispatch(s_tools, 2,
        "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}", out, sizeof out);
    CHECK(n > 0);
    CHECK(strstr(out, "\"id\":2") != NULL);
    CHECK(strstr(out, "self.audio.set_volume") != NULL);
    CHECK(strstr(out, "self.get_device_status") != NULL);
    CHECK(strstr(out, "\"volume\"") != NULL);   // inputSchema property surfaced
}

static void test_tools_call_dispatches_and_returns_result_text(void) {
    char out[256];
    int n = mcp_dispatch(s_tools, 2,
        "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\","
        "\"params\":{\"name\":\"self.audio.set_volume\",\"arguments\":{\"volume\":70}}}",
        out, sizeof out);
    CHECK(n > 0);
    CHECK(s_last_volume == 70);
    CHECK(strstr(out, "\"id\":3") != NULL);
    CHECK(strstr(out, "volume set to 70") != NULL);
}

static void test_tools_call_missing_required_arg_is_error_without_calling_fn(void) {
    s_last_volume = -1;
    char out[256];
    int n = mcp_dispatch(s_tools, 2,
        "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\","
        "\"params\":{\"name\":\"self.audio.set_volume\",\"arguments\":{}}}",
        out, sizeof out);
    CHECK(n > 0);
    CHECK(s_last_volume == -1);   // fn never called
    CHECK(strstr(out, "\"error\"") != NULL);
}

static void test_tools_call_out_of_range_is_error(void) {
    char out[256];
    int n = mcp_dispatch(s_tools, 2,
        "{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"tools/call\","
        "\"params\":{\"name\":\"self.audio.set_volume\",\"arguments\":{\"volume\":999}}}",
        out, sizeof out);
    CHECK(n > 0);
    CHECK(strstr(out, "\"error\"") != NULL);
}

static void test_tools_call_unknown_tool_is_error(void) {
    char out[256];
    int n = mcp_dispatch(s_tools, 2,
        "{\"jsonrpc\":\"2.0\",\"id\":6,\"method\":\"tools/call\","
        "\"params\":{\"name\":\"nope\",\"arguments\":{}}}", out, sizeof out);
    CHECK(n > 0);
    CHECK(strstr(out, "\"error\"") != NULL);
}

static void test_arg_helpers(void) {
    const char *args = "{\"volume\":42,\"on\":true,\"name\":\"led\"}";
    CHECK(mcp_arg_int(args, "volume", -1) == 42);
    CHECK(mcp_arg_int(args, "missing", -1) == -1);
    CHECK(mcp_arg_bool(args, "on", 0) == 1);
    char s[16];
    mcp_arg_string(args, "name", s, sizeof s);
    CHECK(strcmp(s, "led") == 0);
}

int main(void) {
    test_initialize();
    test_tools_list_lists_both();
    test_tools_call_dispatches_and_returns_result_text();
    test_tools_call_missing_required_arg_is_error_without_calling_fn();
    test_tools_call_out_of_range_is_error();
    test_tools_call_unknown_tool_is_error();
    test_arg_helpers();
    if (failures) { printf("%d FAILURE(S)\n", failures); return 1; }
    printf("OK\n");
    return 0;
}
```

- [ ] **Step 2: Wire the test into `test/Makefile`**

Add to `esp32-assistant/test/Makefile` (following the existing entries exactly — e.g. next to `SRC_LUGO_PROTOCOL`):

```makefile
SRC_MCP_SERVER = ../components/mcp_server/mcp_server.c ../components/lugo_protocol/lugo_protocol.c
```

Add `-I../components/mcp_server/include` to a new `MCP_CFLAGS` var (mcp_server.c also needs `lugo_protocol.h`, so reuse the include path):

```makefile
MCP_CFLAGS = -std=c11 -Wall -Wextra -g -O0 \
             -I../components/mcp_server/include -I../components/lugo_protocol/include
```

Add `test_mcp_server` to the `.PHONY test:` prerequisite list and its run line, plus a build rule and clean entry, mirroring the existing pattern:

```makefile
test_mcp_server: test_mcp_server.c $(SRC_MCP_SERVER)
	$(CC) $(MCP_CFLAGS) -o $@ $^
```

(Append `test_mcp_server` to `test:`'s dependency list and to the `./test_mcp_server` run lines, and to `clean`'s rm list, same as the other five targets.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd esp32-assistant/test && make test_mcp_server`
Expected: FAIL — `mcp_server.h: No such file or directory`.

- [ ] **Step 4: Implement `mcp_server.h`**

```c
// esp32-assistant/components/mcp_server/include/mcp_server.h
#pragma once
#include <stdbool.h>
#include <stddef.h>

typedef enum { MCP_PROP_INT_T, MCP_PROP_BOOL_T, MCP_PROP_STRING_T } mcp_prop_type_t;

typedef struct {
    const char *name;
    mcp_prop_type_t type;
    int min, max;     // MCP_PROP_INT_T only; 0/0 = unbounded
    bool required;
} mcp_prop_t;

#define MCP_PROP_INT(n, lo, hi) {(n), MCP_PROP_INT_T, (lo), (hi), true}
#define MCP_PROP_BOOL(n)        {(n), MCP_PROP_BOOL_T, 0, 0, true}
#define MCP_PROP_STRING(n)      {(n), MCP_PROP_STRING_T, 0, 0, true}
#define MCP_PROP_END            {NULL, MCP_PROP_INT_T, 0, 0, false}

typedef struct {
    bool is_error;
    char text[192];
} mcp_result_t;

mcp_result_t mcp_ok_text(const char *fmt, ...);
mcp_result_t mcp_err(const char *fmt, ...);

// args_json points at the "arguments" object's '{', or "" if the tool takes
// no arguments (never NULL).
typedef mcp_result_t (*mcp_tool_fn_t)(const char *args_json);

typedef struct {
    const char *name;          // e.g. "self.audio.set_volume"
    const char *description;
    const mcp_prop_t *props;   // NULL or MCP_PROP_END-terminated array
    bool requires_confirm;
    mcp_tool_fn_t fn;
} mcp_tool_desc_t;

int mcp_arg_int(const char *args_json, const char *name, int fallback);
int mcp_arg_bool(const char *args_json, const char *name, int fallback);
void mcp_arg_string(const char *args_json, const char *name, char *out, size_t cap);

// Handle one JSON-RPC request found in mcp_payload (the value pointed to by
// lugo_event_t.mcp_payload). Writes the full JSON-RPC response into out_buf.
// Returns the response length, or -1 if it doesn't fit in out_cap.
int mcp_dispatch(const mcp_tool_desc_t *const *tools, int n_tools,
                 const char *mcp_payload, char *out_buf, int out_cap);
```

- [ ] **Step 5: Implement `mcp_server.c`**

```c
// esp32-assistant/components/mcp_server/mcp_server.c
#include "mcp_server.h"
#include "lugo_protocol.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>

int mcp_arg_int(const char *args_json, const char *name, int fallback) {
    const char *p = lugo_json_find(args_json, name);
    return p ? lugo_json_get_int(args_json, name) : fallback;
}

int mcp_arg_bool(const char *args_json, const char *name, int fallback) {
    return lugo_json_get_bool(args_json, name, fallback);
}

void mcp_arg_string(const char *args_json, const char *name, char *out, size_t cap) {
    lugo_json_get_string(args_json, name, out, cap);
}

static mcp_result_t make_result(bool is_error, const char *fmt, va_list ap) {
    mcp_result_t r;
    r.is_error = is_error;
    vsnprintf(r.text, sizeof r.text, fmt, ap);
    return r;
}

mcp_result_t mcp_ok_text(const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    mcp_result_t r = make_result(false, fmt, ap);
    va_end(ap);
    return r;
}

mcp_result_t mcp_err(const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    mcp_result_t r = make_result(true, fmt, ap);
    va_end(ap);
    return r;
}

// Append src, JSON-escaping " and \. Returns false on overflow.
static bool append_escaped_str(char *buf, int cap, int *o, const char *src) {
    for (; *src; src++) {
        if (*src == '"' || *src == '\\') {
            if (*o + 2 >= cap) return false;
            buf[(*o)++] = '\\'; buf[(*o)++] = *src;
        } else {
            if (*o + 1 >= cap) return false;
            buf[(*o)++] = *src;
        }
    }
    return true;
}

static int find_tool(const mcp_tool_desc_t *const *tools, int n, const char *name) {
    for (int i = 0; i < n; i++) if (!strcmp(tools[i]->name, name)) return i;
    return -1;
}

// Validate args_json against a tool's declared props. Returns NULL if valid,
// else a static description of the first violation.
static const char *validate_args(const mcp_tool_desc_t *tool, const char *args_json) {
    if (!tool->props) return NULL;
    for (const mcp_prop_t *p = tool->props; p->name; p++) {
        const char *v = lugo_json_find(args_json, p->name);
        if (!v) { if (p->required) return "missing required argument"; continue; }
        if (p->type == MCP_PROP_INT_T) {
            int val = lugo_json_get_int(args_json, p->name);
            if ((p->min != 0 || p->max != 0) && (val < p->min || val > p->max))
                return "argument out of range";
        }
    }
    return NULL;
}

static int write_error(char *out, int cap, int id, const char *message) {
    int o = 0;
    o += snprintf(out + o, cap - o, "{\"jsonrpc\":\"2.0\",\"id\":%d,\"error\":{\"code\":-1,\"message\":\"", id);
    if (o >= cap) return -1;
    if (!append_escaped_str(out, cap, &o, message)) return -1;
    int n = snprintf(out + o, cap - o, "\"}}");
    if (n < 0 || o + n >= cap) return -1;
    return o + n;
}

static int write_tool_call_result(char *out, int cap, int id, mcp_result_t r) {
    int o = 0;
    o += snprintf(out + o, cap - o,
        "{\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"isError\":%s,\"content\":[{\"type\":\"text\",\"text\":\"",
        id, r.is_error ? "true" : "false");
    if (o >= cap) return -1;
    if (!append_escaped_str(out, cap, &o, r.text)) return -1;
    int n = snprintf(out + o, cap - o, "\"}]}}");
    if (n < 0 || o + n >= cap) return -1;
    return o + n;
}

static const char *prop_type_name(mcp_prop_type_t t) {
    switch (t) {
        case MCP_PROP_INT_T: return "integer";
        case MCP_PROP_BOOL_T: return "boolean";
        default: return "string";
    }
}

static int write_tools_list(char *out, int cap, int id,
                            const mcp_tool_desc_t *const *tools, int n) {
    int o = snprintf(out, cap, "{\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"tools\":[", id);
    if (o < 0 || o >= cap) return -1;
    for (int i = 0; i < n; i++) {
        const mcp_tool_desc_t *t = tools[i];
        int w = snprintf(out + o, cap - o,
            "%s{\"name\":\"%s\",\"description\":\"%s\","
            "\"inputSchema\":{\"type\":\"object\",\"properties\":{",
            i ? "," : "", t->name, t->description ? t->description : "");
        if (w < 0 || o + w >= cap) return -1;
        o += w;
        bool first = true;
        for (const mcp_prop_t *p = t->props; p && p->name; p++) {
            w = snprintf(out + o, cap - o, "%s\"%s\":{\"type\":\"%s\"}",
                        first ? "" : ",", p->name, prop_type_name(p->type));
            if (w < 0 || o + w >= cap) return -1;
            o += w; first = false;
        }
        w = snprintf(out + o, cap - o, "}},\"annotations\":{\"requiresConfirm\":%s}}",
                    t->requires_confirm ? "true" : "false");
        if (w < 0 || o + w >= cap) return -1;
        o += w;
    }
    int w = snprintf(out + o, cap - o, "]}}");
    if (w < 0 || o + w >= cap) return -1;
    return o + w;
}

int mcp_dispatch(const mcp_tool_desc_t *const *tools, int n_tools,
                 const char *mcp_payload, char *out_buf, int out_cap) {
    int id = lugo_json_get_int(mcp_payload, "id");
    char method[32];
    lugo_json_get_string(mcp_payload, "method", method, sizeof method);

    if (!strcmp(method, "initialize")) {
        int n = snprintf(out_buf, out_cap,
            "{\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"serverInfo\":"
            "{\"name\":\"LugoDevice\",\"version\":\"1.0.0\"}}}", id);
        return (n < 0 || n >= out_cap) ? -1 : n;
    }
    if (!strcmp(method, "tools/list")) {
        return write_tools_list(out_buf, out_cap, id, tools, n_tools);
    }
    if (!strcmp(method, "tools/call")) {
        const char *params = lugo_json_find(mcp_payload, "params");
        char name[64] = "";
        const char *args = "";
        if (params) {
            lugo_json_get_string(params, "name", name, sizeof name);
            const char *a = lugo_json_find(params, "arguments");
            if (a) args = a;
        }
        int idx = find_tool(tools, n_tools, name);
        if (idx < 0) return write_error(out_buf, out_cap, id, "unknown tool");
        const char *bad = validate_args(tools[idx], args);
        if (bad) return write_error(out_buf, out_cap, id, bad);
        mcp_result_t r = tools[idx]->fn(args);
        return write_tool_call_result(out_buf, out_cap, id, r);
    }
    return write_error(out_buf, out_cap, id, "unknown method");
}
```

- [ ] **Step 6: Create `CMakeLists.txt`**

```
# esp32-assistant/components/mcp_server/CMakeLists.txt
idf_component_register(SRCS "mcp_server.c"
                       INCLUDE_DIRS "include"
                       REQUIRES lugo_protocol)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd esp32-assistant/test && make test_mcp_server && ./test_mcp_server`
Expected: `OK` (7 tests pass).

- [ ] **Step 8: Run the full host suite (no regressions)**

Run: `cd esp32-assistant/test && make test`
Expected: all 7 test binaries pass.

- [ ] **Step 9: Commit**

```bash
cd esp32-assistant
git add components/mcp_server test/test_mcp_server.c test/Makefile
git commit -m "feat(mcp_server): JSON-RPC dispatch engine, property validation, host-tested"
cd ..
git add esp32-assistant
git commit -m "chore: bump esp32-assistant (mcp_server engine)"
```

---

### Task 3: `mcp_tools` — WHOLE_ARCHIVE registrants + linker section (target-verified)

**Files:**
- Create: `esp32-assistant/components/mcp_tools/CMakeLists.txt`
- Create: `esp32-assistant/components/mcp_tools/linker.lf`
- Create: `esp32-assistant/components/mcp_tools/include/mcp_tools.h`
- Create: `esp32-assistant/components/mcp_tools/registry.c`

**Interfaces:**
- Consumes: `mcp_tool_desc_t`, `mcp_dispatch` (Task 2).
- Produces:
  - `#define LUGO_MCP_TOOL(sym) static const mcp_tool_desc_t sym; static const mcp_tool_desc_t *const sym##_ref __attribute__((used, section("mcp_tool"))) = &sym; static const mcp_tool_desc_t sym =` — registration macro, exact mirror of `LUGO_BOARD_REGISTER` in `board.h`.
  - `void mcp_tools_init(void);` — no-op placeholder for future startup logic (kept for symmetry with `board_detect_and_select`; the registry itself needs no init since `mcp_tools_dispatch` walks the section directly).
  - `int mcp_tools_dispatch(const char *mcp_payload, char *out_buf, int out_cap);` — target-only wrapper: walks the `mcp_tool` linker section into a local array and calls `mcp_dispatch`. This is the one function `ws_client`'s event handler (Task 4) calls; it is **not host-tested** (same as `board_detect_and_select`) because its correctness depends on the real linker section, which only exists in an ESP-IDF build.

This task has **no tool definitions yet** — it's the empty scaffold + linker plumbing, proven with a single throwaway tool so the section-walk itself is verified before Task 4/5 add the real 7 tools. Splitting it this way isolates the "does WHOLE_ARCHIVE + linker.lf actually work" risk from the "are the 7 tools individually correct" risk (already covered by Task 2's host tests against `mcp_dispatch`).

- [ ] **Step 1: Create the linker fragment**

```
# esp32-assistant/components/mcp_tools/linker.lf
# Keeps the auto-registration "mcp_tool" section (see LUGO_MCP_TOOL in
# mcp_tools.h) under --gc-sections and brackets it with boundary symbols
# registry.c counts from. Mirrors components/boards/linker.lf exactly.

[sections:mcp_tool]
entries:
    mcp_tool+

[scheme:mcp_tool_scheme]
entries:
    mcp_tool -> flash_rodata

[mapping:mcp_tool_mapping]
archive: *
entries:
    * (mcp_tool_scheme);
        mcp_tool -> flash_rodata KEEP() SURROUND(mcp_tool)
```

- [ ] **Step 2: Create `mcp_tools.h`**

```c
// esp32-assistant/components/mcp_tools/include/mcp_tools.h
#pragma once
#include "mcp_server.h"

// Define a hardware tool and auto-register it into the linker "mcp_tool"
// section (mirrors LUGO_BOARD_REGISTER in components/board/include/board.h):
//   LUGO_MCP_TOOL(tool_my_thing) { .name = "self.my.thing", ... };
#define LUGO_MCP_TOOL(sym)                                                \
    static const mcp_tool_desc_t sym;                                    \
    static const mcp_tool_desc_t *const sym##_ref                        \
        __attribute__((used, section("mcp_tool"))) = &sym;               \
    static const mcp_tool_desc_t sym =

// Handle one mcp payload against every LUGO_MCP_TOOL-registered tool.
// Target-only (walks the real linker section) — not host-tested.
int mcp_tools_dispatch(const char *mcp_payload, char *out_buf, int out_cap);
```

- [ ] **Step 3: Create `registry.c` with one throwaway proof tool**

```c
// esp32-assistant/components/mcp_tools/registry.c
#include "mcp_tools.h"

// Boundary symbols of the "mcp_tool" section (see linker.lf).
extern const mcp_tool_desc_t *const _mcp_tool_start[];
extern const mcp_tool_desc_t *const _mcp_tool_end[];

// Proof-of-registration tool for this task; Task 4/5 add the real ones and
// this one can stay (it's a harmless, always-available diagnostic) or be
// deleted once real tools exist — implementer's call, not load-bearing.
static mcp_result_t ping_fn(const char *args) {
    (void)args;
    return mcp_ok_text("pong");
}
LUGO_MCP_TOOL(tool_ping) {
    .name = "self.ping", .description = "Diagnostic: returns pong",
    .props = NULL, .requires_confirm = false, .fn = ping_fn,
};

int mcp_tools_dispatch(const char *mcp_payload, char *out_buf, int out_cap) {
    int n = (int)(_mcp_tool_end - _mcp_tool_start);
    return mcp_dispatch(_mcp_tool_start, n, mcp_payload, out_buf, out_cap);
}
```

- [ ] **Step 4: Create `CMakeLists.txt` with WHOLE_ARCHIVE**

```
# esp32-assistant/components/mcp_tools/CMakeLists.txt
# WHOLE_ARCHIVE is required, not optional — see components/boards/CMakeLists.txt
# for the full explanation. Each tool file registers purely through a static
# "mcp_tool"-section pointer (LUGO_MCP_TOOL) that nothing references by name;
# without WHOLE_ARCHIVE the linker never pulls that object out of
# libmcp_tools.a, and the section stays empty at runtime.
idf_component_register(SRCS "registry.c"
                       INCLUDE_DIRS "include"
                       REQUIRES mcp_server
                       LDFRAGMENTS "linker.lf"
                       WHOLE_ARCHIVE)
```

- [ ] **Step 5: Register the component with the build**

`esp32-assistant/main/CMakeLists.txt` — check its current `REQUIRES`/`PRIV_REQUIRES` list (`cat esp32-assistant/main/CMakeLists.txt`) and add `mcp_tools` (and `ws_client` already present) so `main.c` can call `mcp_tools_dispatch`. ESP-IDF auto-discovers components under `components/`, so no top-level registration beyond `main`'s `REQUIRES` is needed.

- [ ] **Step 6: Build on target and verify the registry is non-empty**

This step cannot be host-tested (WHOLE_ARCHIVE + linker sections are ESP-IDF-toolchain-specific). Run:

```bash
cd esp32-assistant
source ~/esp/esp-idf/export.sh
idf.py build
```

Expected: clean build. Then, temporarily add a boot-time log (or reuse an existing log point once Task 4 wires the dispatch call) to print the registered tool count and confirm it is `1` (just `self.ping`), not `0`. If it's `0`, the WHOLE_ARCHIVE/linker.lf wiring is broken — re-check Steps 1/4 against `components/boards`'s working example before proceeding to Task 4.

- [ ] **Step 7: Commit**

```bash
cd esp32-assistant
git add components/mcp_tools main/CMakeLists.txt
git commit -m "feat(mcp_tools): WHOLE_ARCHIVE registrant scaffold + linker section (proof tool)"
cd ..
git add esp32-assistant
git commit -m "chore: bump esp32-assistant (mcp_tools scaffold)"
```

---

### Task 4: Wire MCP into `ws_client` + `main.c`, add `display_ops_t.set_backlight`

**Files:**
- Modify: `esp32-assistant/components/board/include/board_types.h`
- Modify: `esp32-assistant/components/display/include/display.h`
- Modify: `esp32-assistant/components/display/display.c`
- Modify: `esp32-assistant/components/display/drivers/st7789.c`
- Modify: `esp32-assistant/components/ws_client/include/ws_client.h` (check exact filename with `ls esp32-assistant/components/ws_client/include`)
- Modify: `esp32-assistant/components/ws_client/ws_client.c`
- Modify: `esp32-assistant/main/main.c`
- Test: `esp32-assistant/test/test_board_facades.c` (append — `set_backlight` facade case)

**Interfaces:**
- Consumes: `mcp_tools_dispatch` (Task 3).
- Produces:
  - `display_ops_t.set_backlight(bool on)` — new vtable slot; `display_set_backlight(bool on)` facade function in `display.h`/`display.c`.
  - `ws_client_send_mcp(const char *json_payload)` — sends `{"type":"mcp","payload":<json_payload>}` as a text frame (json_payload is already a complete JSON-RPC response object built by `mcp_tools_dispatch`, so this just wraps it, mirroring `lugo_build_wakeup`'s snprintf-into-buffer style).
  - `main.c`'s `LUGO_EV_MCP` case (currently `default: break;`) calls `mcp_tools_dispatch(ev->mcp_payload, buf, sizeof buf)` then `ws_client_send_mcp(buf)`.

- [ ] **Step 1: Add `set_backlight` to the display vtable**

In `board_types.h`, extend `display_ops_t`:

```c
typedef struct {
    esp_err_t (*init)(const void *cfg);
    void (*show)(const char *line1, const char *line2);
    void (*set_backlight)(bool on);
} display_ops_t;
```

- [ ] **Step 2: Write the failing facade test**

Append to `esp32-assistant/test/test_board_facades.c` (check its existing mock-ops pattern first with `grep -n "mock_display\|display_ops_t" test/test_board_facades.c` — follow the same style: a static mock op table installed via `board_set`):

```c
static bool s_backlight_on = false;
static void mock_display_set_backlight(bool on) { s_backlight_on = on; }
// (add set_backlight to whatever mock_display_ops struct literal already
// exists in this file, alongside its existing init/show mocks)

static void test_display_set_backlight_calls_through(void) {
    display_init();
    display_set_backlight(true);
    CHECK(s_backlight_on == true);
    display_set_backlight(false);
    CHECK(s_backlight_on == false);
}
```

Add `test_display_set_backlight_calls_through();` to this file's `main()`.

- [ ] **Step 3: Run test to verify it fails**

Run: `cd esp32-assistant/test && make test_board_facades`
Expected: FAIL — `display_set_backlight` undeclared / mock ops struct missing a `set_backlight` initializer (compile error).

- [ ] **Step 4: Implement the facade**

`display.h` — add:

```c
// Turn the panel backlight on/off (GPIO, no PWM — see st7789 driver comment).
void display_set_backlight(bool on);
```

`display.c` — add:

```c
void display_set_backlight(bool on) { s_ops->set_backlight(on); }
```

`st7789.c` — add the driver function and wire it into `display_st7789_ops`:

```c
static void st7789_set_backlight(bool on) {
    // c->bl was captured at init as a GPIO output; re-derive is unnecessary —
    // reuse the same pin the init path configured. Since ops functions are
    // cfg-less by signature, cache the pin at init time.
    extern int g_st7789_bl_pin;  // set in st7789_init, see below
    gpio_set_level(g_st7789_bl_pin, on ? 1 : 0);
}
```

Add `int g_st7789_bl_pin;` near the top of `st7789.c` (file-scope, non-static so the extern above resolves) and set it in `st7789_init`: `g_st7789_bl_pin = c->bl;` right after the existing `gpio_set_level(c->bl, 1);` line.

Find the `display_st7789_ops` struct literal in `st7789.c` (`grep -n "display_st7789_ops" components/display/drivers/st7789.c`) and add `.set_backlight = st7789_set_backlight,` to it.

- [ ] **Step 5: Run facade test to verify it passes**

Run: `cd esp32-assistant/test && make test_board_facades && ./test_board_facades`
Expected: pass.

- [ ] **Step 6: Add `ws_client_send_mcp`**

In `ws_client`'s header (check the exact path — likely `components/ws_client/include/ws_client.h`), add:

```c
// Send a pre-built JSON-RPC response object as an mcp frame:
// {"type":"mcp","payload":<json_payload>}. json_payload must already be a
// complete, valid JSON value (mcp_tools_dispatch's output).
int ws_client_send_mcp(const char *json_payload);
```

In `ws_client.c`, add alongside `ws_client_send_abort`:

```c
int ws_client_send_mcp(const char *json_payload) {
    if (!s_connected) return -1;
    static char buf[640];
    int n = snprintf(buf, sizeof buf, "{\"type\":\"mcp\",\"payload\":%s}", json_payload);
    if (n < 0 || n >= (int)sizeof buf) return -1;
    return esp_websocket_client_send_text(s_client, buf, n, portMAX_DELAY);
}
```

(640 bytes covers the 7 v1 tools' `tools/list` response with headroom; if Task 5's tool set grows this needs revisiting — not a concern for this task's scope of 1 proof tool.)

- [ ] **Step 7: Wire the `LUGO_EV_MCP` case in `main.c`**

In `main.c`, find the `switch (ev->type)` block (around line 189-267 per the existing structure) and replace the `default: break;  // LUGO_EV_MCP / LUGO_EV_UNKNOWN` handling by adding an explicit case before `default`:

```c
    case LUGO_EV_MCP: {
        if (ev->mcp_payload) {
            static char resp[640];
            int n = mcp_tools_dispatch(ev->mcp_payload, resp, sizeof resp);
            if (n > 0) ws_client_send_mcp(resp);
        }
        break;
    }
```

Add `#include "mcp_tools.h"` to `main.c`'s includes. Update `default: break;` comment to just `// LUGO_EV_UNKNOWN`.

Add `mcp_tools` to `esp32-assistant/main/CMakeLists.txt`'s `REQUIRES` (if not already added in Task 3 Step 5).

- [ ] **Step 8: Build on target**

Run:
```bash
cd esp32-assistant
source ~/esp/esp-idf/export.sh
idf.py build
```
Expected: clean build (this step has no host-testable equivalent — `main.c`'s FSM and `ws_client`'s event loop are target-only, matching the existing pattern where `main.c`/`ws_client.c` are verified by build, not host tests).

- [ ] **Step 9: Run the full host suite (no regressions)**

Run: `cd esp32-assistant/test && make test`
Expected: all binaries pass, including the new `test_board_facades` backlight case.

- [ ] **Step 10: Commit**

```bash
cd esp32-assistant
git add components/board/include/board_types.h components/display components/ws_client main/main.c main/CMakeLists.txt test/test_board_facades.c
git commit -m "feat(esp32): wire MCP dispatch into ws_client/main.c FSM; add backlight vtable op"
cd ..
git add esp32-assistant
git commit -m "chore: bump esp32-assistant (mcp wiring + backlight op)"
```

---

### Task 5: The 7 v1 tools + `wakeup` `features.mcp` flag + template README

**Files:**
- Create: `esp32-assistant/components/mcp_tools/audio_tools.c`
- Create: `esp32-assistant/components/mcp_tools/display_tools.c`
- Create: `esp32-assistant/components/mcp_tools/gpio_tools.c`
- Create: `esp32-assistant/components/mcp_tools/device_tools.c`
- Create: `esp32-assistant/components/mcp_tools/README.md`
- Modify: `esp32-assistant/components/mcp_tools/CMakeLists.txt` (add new SRCS)
- Modify: `esp32-assistant/components/lugo_protocol/lugo_protocol.c` (`lugo_build_wakeup` — add `features.mcp`)
- Modify: `esp32-assistant/test/test_lugo_protocol.c` (wakeup builder now advertises mcp)

**Interfaces:**
- Consumes: `LUGO_MCP_TOOL`, `mcp_arg_int`/`mcp_arg_bool`/`mcp_arg_string`, `mcp_ok_text`/`mcp_err` (Tasks 2/3); `audio_set_volume`/`audio_get_volume` (existing `audio.h`); `display_show`/`display_set_backlight` (Task 4); `board_active()` (existing `board.h`).
- Produces: the 7 tools from the spec's table, each a `LUGO_MCP_TOOL` definition; `lugo_build_wakeup` now emits `"features":{"mcp":true}` so the gateway's discovery only runs against firmware that actually has this component built in.

- [ ] **Step 1: Write the failing test for the wakeup feature flag**

In `esp32-assistant/test/test_lugo_protocol.c`, find the existing wakeup-builder test (`grep -n "lugo_build_wakeup" test/test_lugo_protocol.c`) and add:

```c
static void test_wakeup_advertises_mcp_feature(void) {
    char buf[256];
    int n = lugo_build_wakeup(buf, sizeof buf, "dev", 16000, 24000, 60);
    CHECK(n > 0);
    CHECK(strstr(buf, "\"features\"") != NULL);
    CHECK(strstr(buf, "\"mcp\":true") != NULL);
}
```
Add it to `main()`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd esp32-assistant/test && make test_lugo_protocol && ./test_lugo_protocol`
Expected: FAIL — no `"features"` substring in the built wakeup JSON.

- [ ] **Step 3: Update `lugo_build_wakeup`**

In `esp32-assistant/components/lugo_protocol/lugo_protocol.c`:

```c
int lugo_build_wakeup(char *buf, int buflen, const char *profile,
                      int in_sr, int out_sr, int frame_ms) {
    int n = snprintf(buf, buflen,
        "{\"type\":\"wakeup\",\"profile\":\"%s\",\"trigger\":\"button\","
        "\"audio_params\":{\"format\":\"opus\",\"sample_rate\":%d,"
        "\"output_sample_rate\":%d,\"frame_duration\":%d},"
        "\"features\":{\"mcp\":true}}",
        profile ? profile : "", in_sr, out_sr, frame_ms);
    if (n < 0 || n >= buflen) return -1;
    return n;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd esp32-assistant/test && make test_lugo_protocol && ./test_lugo_protocol`
Expected: pass. Then `make test` for the full suite (no regressions — the wakeup string grew but nothing else parses its own output).

- [ ] **Step 5: Commit the feature flag**

```bash
cd esp32-assistant
git add components/lugo_protocol/lugo_protocol.c test/test_lugo_protocol.c
git commit -m "feat(lugo_protocol): advertise features.mcp:true in wakeup"
```

- [ ] **Step 6: Implement `audio_tools.c`**

```c
// esp32-assistant/components/mcp_tools/audio_tools.c
#include "mcp_tools.h"
#include "audio.h"

static mcp_result_t set_volume_fn(const char *args) {
    int v = mcp_arg_int(args, "volume", -1);
    if (v < 0) return mcp_err("missing volume");
    audio_set_volume(v);
    return mcp_ok_text("volume set to %d", v);
}
static const mcp_prop_t set_volume_props[] = { MCP_PROP_INT("volume", 0, 100), MCP_PROP_END };
LUGO_MCP_TOOL(tool_set_volume) {
    .name = "self.audio.set_volume", .description = "Set speaker volume (0-100)",
    .props = set_volume_props, .requires_confirm = false, .fn = set_volume_fn,
};
```

- [ ] **Step 7: Implement `display_tools.c`**

```c
// esp32-assistant/components/mcp_tools/display_tools.c
#include "mcp_tools.h"
#include "display.h"

static mcp_result_t show_text_fn(const char *args) {
    char line1[64] = "", line2[64] = "";
    mcp_arg_string(args, "line1", line1, sizeof line1);
    mcp_arg_string(args, "line2", line2, sizeof line2);
    display_show(line1, line2[0] ? line2 : NULL);
    return mcp_ok_text("shown");
}
static const mcp_prop_t show_text_props[] = {
    MCP_PROP_STRING("line1"),
    { "line2", MCP_PROP_STRING_T, 0, 0, false },  // optional second line
    MCP_PROP_END,
};
LUGO_MCP_TOOL(tool_show_text) {
    .name = "self.screen.show_text", .description = "Show up to two lines of text on the screen",
    .props = show_text_props, .requires_confirm = false, .fn = show_text_fn,
};

static mcp_result_t set_backlight_fn(const char *args) {
    int on = mcp_arg_bool(args, "on", -1);
    if (on < 0) return mcp_err("missing on");
    display_set_backlight(on != 0);
    return mcp_ok_text(on ? "backlight on" : "backlight off");
}
static const mcp_prop_t set_backlight_props[] = { MCP_PROP_BOOL("on"), MCP_PROP_END };
LUGO_MCP_TOOL(tool_set_backlight) {
    .name = "self.screen.set_backlight", .description = "Turn the screen backlight on or off",
    .props = set_backlight_props, .requires_confirm = false, .fn = set_backlight_fn,
};
```

- [ ] **Step 8: Implement `gpio_tools.c`**

```c
// esp32-assistant/components/mcp_tools/gpio_tools.c
#include "mcp_tools.h"
#include "sdkconfig.h"
#include "driver/gpio.h"

// Pins already owned by mic/speaker/display/buttons on this board (see
// board_def.c for lugo-s3-st7789). A tool-driven GPIO write must never touch
// these — reconfiguring, say, the I2S BCLK pin as a generic output would
// desync audio. Kconfig-configurable mic/speaker pins are read as their
// #define'd values; display/button pins are the literal ints in board_def.c.
static const int RESERVED_PINS[] = {
    42, 41, 1, 2, 17,        // display: sclk, mosi, dc, rst, bl
    47, 40, 39,              // buttons: wake, vol_up, vol_down
    CONFIG_AA_MIC_WS, CONFIG_AA_MIC_SCK, CONFIG_AA_MIC_SD,
    CONFIG_AA_SPK_BCLK, CONFIG_AA_SPK_LRC, CONFIG_AA_SPK_DIN,
};
#define N_RESERVED (int)(sizeof(RESERVED_PINS) / sizeof(RESERVED_PINS[0]))

static bool pin_is_reserved(int pin) {
    for (int i = 0; i < N_RESERVED; i++) if (RESERVED_PINS[i] == pin) return true;
    return false;
}

static mcp_result_t gpio_set_fn(const char *args) {
    int pin = mcp_arg_int(args, "pin", -1);
    int value = mcp_arg_int(args, "value", -1);
    if (pin < 0 || value < 0) return mcp_err("missing pin or value");
    if (pin_is_reserved(pin)) return mcp_err("pin %d is reserved by existing hardware", pin);
    gpio_config_t cfg = { .pin_bit_mask = 1ULL << pin, .mode = GPIO_MODE_OUTPUT };
    if (gpio_config(&cfg) != ESP_OK) return mcp_err("failed to configure pin %d", pin);
    gpio_set_level(pin, value ? 1 : 0);
    return mcp_ok_text("pin %d set to %d", pin, value ? 1 : 0);
}
static const mcp_prop_t gpio_set_props[] = {
    MCP_PROP_INT("pin", 0, 48), MCP_PROP_INT("value", 0, 1), MCP_PROP_END,
};
LUGO_MCP_TOOL(tool_gpio_set) {
    .name = "self.gpio.set", .description = "Set a GPIO pin high or low (rejects pins used by existing hardware)",
    .props = gpio_set_props, .requires_confirm = true, .fn = gpio_set_fn,
};
```

- [ ] **Step 9: Implement `device_tools.c`**

```c
// esp32-assistant/components/mcp_tools/device_tools.c
#include "mcp_tools.h"
#include "audio.h"
#include "esp_system.h"
#include "esp_sleep.h"
#include "esp_wifi.h"

static mcp_result_t status_fn(const char *args) {
    (void)args;
    return mcp_ok_text(
        "heap=%lu volume=%d",
        (unsigned long)esp_get_free_heap_size(), audio_get_volume());
}
LUGO_MCP_TOOL(tool_get_status) {
    .name = "self.get_device_status", .description = "Read free heap and current volume",
    .props = NULL, .requires_confirm = false, .fn = status_fn,
};

static mcp_result_t idle_fn(const char *args) {
    (void)args;
    // Phase 1: no direct FSM hook exists yet from a tool callback context;
    // WS idle-timeout already drives the sleep transition (see
    // [[lugo-device-protocol]] connect-on-wake lifecycle). This tool answers
    // affirmatively so the LLM can say "okay, resting" — the actual
    // WS-level idle/goodbye still governs the real disconnect. Revisit if a
    // direct main.c FSM hook becomes necessary.
    return mcp_ok_text("going idle");
}
LUGO_MCP_TOOL(tool_idle) {
    .name = "self.device.idle", .description = "Tell the device to go idle/rest",
    .props = NULL, .requires_confirm = false, .fn = idle_fn,
};

static mcp_result_t shutdown_fn(const char *args) {
    (void)args;
    // esp_restart() is used instead of true power-off (no PMIC/latch on this
    // board to cut power from software) — closest available "shut down"
    // primitive. Revisit if a board gains a power-latch GPIO.
    esp_restart();
    return mcp_ok_text("restarting");  // unreachable; kept for a valid return path
}
LUGO_MCP_TOOL(tool_shutdown) {
    .name = "self.device.shutdown", .description = "Power off / restart the device",
    .props = NULL, .requires_confirm = true, .fn = shutdown_fn,
};
```

- [ ] **Step 10: Update `mcp_tools`'s CMakeLists SRCS**

```
# esp32-assistant/components/mcp_tools/CMakeLists.txt
idf_component_register(SRCS "registry.c" "audio_tools.c" "display_tools.c" "gpio_tools.c" "device_tools.c"
                       INCLUDE_DIRS "include"
                       REQUIRES mcp_server audio display driver esp_system esp_hw_support
                       LDFRAGMENTS "linker.lf"
                       WHOLE_ARCHIVE)
```

(`REQUIRES` list: `driver` for `gpio.h`, `esp_system` for `esp_restart`/`esp_get_free_heap_size`, `esp_hw_support` for `esp_sleep.h` if used — drop `esp_hw_support`/`esp_sleep.h` from `device_tools.c` if unused after Step 9's final form, since `idle_fn` ended up not calling any sleep API.)

- [ ] **Step 11: Build on target**

```bash
cd esp32-assistant
source ~/esp/esp-idf/export.sh
idf.py build
```
Expected: clean build with all 7 tools (`self.ping` + 6 real ones — or remove `self.ping` now that real tools exist, per Task 3 Step 3's note) compiled into the `mcp_tool` section.

- [ ] **Step 12: On-target verification (human gate)**

```bash
idf.py flash monitor
```
Manually confirm via the gateway (once the gateway plan is deployed) or a local test client that:
1. `wakeup` includes `features.mcp: true`.
2. Gateway's `initialize`/`tools/list` round-trip returns 6 tools (or 7 with `self.ping`).
3. A `self.audio.set_volume` call actually changes the device's volume audibly.
4. `self.device.shutdown` requires `confirm:true` before the device restarts.

This is the plan's final human verification gate — host tests already covered `mcp_dispatch`'s logic (Task 2) and the WHOLE_ARCHIVE plumbing (Task 3); this step confirms the 7 real tools wired to real hardware behave correctly together.

- [ ] **Step 13: Write the template README**

```markdown
# esp32-assistant/components/mcp_tools/README.md

# Adding a hardware tool

A tool is one `LUGO_MCP_TOOL` definition — no other file needs to change.

    static mcp_result_t my_fn(const char *args) {
        int v = mcp_arg_int(args, "some_int", -1);
        if (v < 0) return mcp_err("missing some_int");
        // ... touch hardware here, via board_active()->X for anything that
        // differs per board, or a direct ESP-IDF call for generic MCU
        // peripherals (like gpio_tools.c does) ...
        return mcp_ok_text("did the thing with %d", v);
    }
    static const mcp_prop_t my_props[] = {
        MCP_PROP_INT("some_int", 0, 100), MCP_PROP_END,
    };
    LUGO_MCP_TOOL(tool_my_thing) {
        .name = "self.my.thing",
        .description = "One sentence the LLM sees when deciding whether to call this",
        .props = my_props,
        .requires_confirm = false,
        .fn = my_fn,
    };

Add the new .c file to `mcp_tools/CMakeLists.txt`'s `SRCS` list (not globbed —
see the CMakeLists comment on WHOLE_ARCHIVE for why explicit SRCS, not a glob
across multiple directories, was chosen here unlike `components/boards`).

## Property types

`MCP_PROP_INT(name, min, max)`, `MCP_PROP_BOOL(name)`, `MCP_PROP_STRING(name)`
— all required by default. For an optional property, write the struct literal
directly with `.required = false` (see `display_tools.c`'s `line2` for an
example). `mcp_dispatch` rejects a `tools/call` with a missing required
property or an out-of-range int **before** calling your `fn` — you never need
to re-validate what your `props` array already declares.

## When to set `requires_confirm = true`

Set it when the action is destructive, hard to reverse, or safety-relevant —
`self.device.shutdown` (powers off), `self.gpio.set` (could physically drive
something unexpected). Do **not** set it for read-only or easily-reversible
actions — `self.get_device_status`, `self.audio.set_volume`,
`self.screen.set_backlight`, `self.device.idle` ("go rest" is not destructive).

When `requires_confirm` is true, the **gateway** (not this firmware) injects a
`confirm` boolean into the tool's schema and blocks the first call until the
LLM re-calls with `confirm:true` after asking the user out loud — see
`docs/superpowers/specs/2026-07-09-device-mcp-hardware-tools-design.md`. This
firmware never sees an unconfirmed call; by the time `fn` runs, confirmation
already happened.

## Reserved GPIO pins

If your tool drives a raw GPIO (not through an existing board op), check it
against the reserved-pin list in `gpio_tools.c` first — mic/speaker/display/
button pins must never be reconfigured by a tool call.
```

- [ ] **Step 14: Run the full host suite one more time**

Run: `cd esp32-assistant/test && make test`
Expected: all binaries pass (this task added no new host-testable logic beyond Step 1-4's wakeup flag — the 7 tools are target-build-verified only, consistent with Task 3/4's precedent for anything touching real hardware or the linker section).

- [ ] **Step 15: Commit**

```bash
cd esp32-assistant
git add components/mcp_tools
git commit -m "feat(mcp_tools): ship v1 hardware tools (status/volume/screen/gpio/idle/shutdown) + template README"
cd ..
git add esp32-assistant
git commit -m "chore: bump esp32-assistant (v1 mcp tools)"
```

---

## Self-Review

**Spec coverage:**
- `LUGO_MCP_TOOL` self-registration template mirroring `LUGO_BOARD_REGISTER` → Task 3. ✓
- Property system + typed validation + auto `inputSchema` emission → Task 2. ✓
- `initialize`/`tools/list`/`tools/call` JSON-RPC handling → Task 2 (`mcp_dispatch`). ✓
- WHOLE_ARCHIVE + linker.lf gotcha carried forward → Task 3, documented inline exactly like `components/boards`. ✓
- Hardware routed through board vtable where board-specific (audio, display) → Tasks 1/4/5. ✓
- 7 v1 tools with the confirm split (`shutdown`/`gpio.set` = confirm; rest = not) → Task 5. ✓
- `features.mcp` handshake flag → Task 5 Steps 1-5. ✓
- `mcp` frame envelope match with the gateway plan → Task 4 Step 6 (`ws_client_send_mcp`), Task 1 (payload parsing). ✓
- Template README ("add a tool in 3 lines" + confirm guidance) → Task 5 Step 13. ✓
- Host-testable vs target-only split, matching existing `board`/`boards` precedent → explicit throughout (Task 2 fully host-tested; Task 3/4/5's hardware-touching code build-verified only). ✓
- `screen.set_brightness` → `set_backlight` rename (hardware-grounded deviation from the original spec wording, per user decision) → Task 4/5, called out in Global Constraints. ✓
- `gpio.set` reserved-pin guard (per user decision, no hardcoded LED pin) → Task 5 Step 8. ✓
- RPi / vision / wake-word / dynamic re-discovery — out of scope, not planned. ✓ (matches spec's scope boundary)

**Placeholder scan:** No TBD/TODO. Two spots are explicitly flagged as engineer judgment calls rather than hidden gaps: (1) Task 3 Step 3's throwaway `self.ping` tool — its removal-or-keep is explicitly left open and harmless either way; (2) Task 5 Step 10's `REQUIRES` list has an explicit note to drop `esp_hw_support` if `device_tools.c`'s final form doesn't need `esp_sleep.h` (it doesn't, per Step 9's actual code — the comment flags this as a concrete cleanup, not an open question).

**Type consistency:** `mcp_tool_desc_t`, `mcp_prop_t`, `mcp_result_t`, `mcp_tool_fn_t` defined in Task 2's `mcp_server.h` are used identically in Task 3's `LUGO_MCP_TOOL` macro and Task 5's four tool files. `mcp_dispatch(tools, n_tools, mcp_payload, out_buf, out_cap)` signature matches between Task 2's definition, its own tests, and Task 3's `mcp_tools_dispatch` wrapper call. `lugo_event_t.mcp_payload` (Task 1) is the exact value passed to `mcp_tools_dispatch` in Task 4's `main.c` wiring. `ws_client_send_mcp(const char *json_payload)` (Task 4) receives exactly the `out_buf` that `mcp_tools_dispatch` fills — no format mismatch between producer and consumer.
