# Relative Volume (Delta) for self.audio.set_volume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `self.audio.set_volume` accept a relative `delta` (e.g. +10/-10) as an alternative to the existing absolute `volume`, on both the RPi client and the ESP32 firmware, so voice commands like "turn the volume up" map to one tool call instead of requiring the LLM to read-then-write.

**Architecture:** Same shape on both clients: the tool's schema gains an optional `delta` field alongside the now-optional `volume`; the handler enforces "exactly one of the two" itself (not via JSON-Schema `required`, since that can't express mutual exclusion); a `delta` call reads the current volume, adds the delta, clamps 0-100, and reports the resulting absolute percentage. ESP32 already has the underlying clamped-adjust primitive (`audio_adjust_volume`, used today by the physical Vol+/− buttons) — this only exposes it through MCP.

**Tech Stack:** RPi client: Python 3, pytest. ESP32: C11, ESP-IDF, host-testable via `test/Makefile` (though this specific tool has no dedicated test file, matching the current state of the codebase).

## Global Constraints

- No change to `requires_confirm` on either client — stays `false` (non-destructive).
- No new tools (`volume_up`/`volume_down`) — one tool, two ways to call it.
- No change to the physical Vol+/Vol− button path on ESP32 — it already calls `audio_adjust_volume` directly in `main.c` and is untouched by this plan.
- Response text always reports the resulting absolute percentage, never just the requested delta.
- ESP32 side: `mcp_server.c:67` (`if (!v) { if (p->required) return "missing required argument"; continue; }`) confirms the dispatch framework only enforces a missing prop when `required=true` — both `volume` and `delta` must be `required=false` so the handler's own "exactly one" check actually runs instead of the framework short-circuiting first.

---

### Task 1: RPi client — `self.audio.set_volume` delta support

**Files:**
- Modify: `agent-assistant/a2a_client/mcp_tools.py:12-20` (the `self.audio.set_volume` `TOOL_DEFS` entry), `agent-assistant/a2a_client/mcp_tools.py:70-73` (the `_call_tool` branch)
- Test: `agent-assistant/tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `McpToolContext.get_volume_pct: Callable[[], int]` and `set_volume_pct: Callable[[int], None]` (both already exist, unchanged).
- Produces: no new public interface — `handle_mcp_request`'s behavior for `self.audio.set_volume` changes; nothing else in the file changes shape.

- [ ] **Step 1: Write the failing tests**

Add to `agent-assistant/tests/test_mcp_tools.py` (after `test_set_volume_missing_argument_error_has_error_key`, before `test_device_idle_calls_context`):

```python
def test_set_volume_delta_increases_from_current_volume():
    ctx, fake = _ctx()
    fake.volume = 50
    resp = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 20, "method": "tools/call",
         "params": {"name": "self.audio.set_volume", "arguments": {"delta": 10}}},
        ctx,
    )
    assert fake.volume == 60
    assert "60" in resp["result"]["content"][0]["text"]
    assert not resp["result"].get("isError")


def test_set_volume_delta_decreases_from_current_volume():
    ctx, fake = _ctx()
    fake.volume = 50
    resp = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
         "params": {"name": "self.audio.set_volume", "arguments": {"delta": -20}}},
        ctx,
    )
    assert fake.volume == 30
    assert "30" in resp["result"]["content"][0]["text"]


def test_set_volume_delta_clamps_at_100():
    ctx, fake = _ctx()
    fake.volume = 95
    handle_mcp_request(
        {"jsonrpc": "2.0", "id": 22, "method": "tools/call",
         "params": {"name": "self.audio.set_volume", "arguments": {"delta": 10}}},
        ctx,
    )
    assert fake.volume == 100


def test_set_volume_delta_clamps_at_0():
    ctx, fake = _ctx()
    fake.volume = 5
    handle_mcp_request(
        {"jsonrpc": "2.0", "id": 23, "method": "tools/call",
         "params": {"name": "self.audio.set_volume", "arguments": {"delta": -20}}},
        ctx,
    )
    assert fake.volume == 0


def test_set_volume_both_volume_and_delta_returns_error():
    ctx, fake = _ctx()
    resp = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 24, "method": "tools/call",
         "params": {"name": "self.audio.set_volume", "arguments": {"volume": 50, "delta": 10}}},
        ctx,
    )
    assert resp["result"]["isError"] is True
    assert fake.volume == 100  # unchanged


def test_set_volume_neither_volume_nor_delta_returns_error():
    ctx, fake = _ctx()
    resp = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 25, "method": "tools/call",
         "params": {"name": "self.audio.set_volume", "arguments": {}}},
        ctx,
    )
    assert resp["result"]["isError"] is True
    assert fake.volume == 100  # unchanged
```

Note: `test_set_volume_missing_argument_returns_error` (already in the file, unchanged) exercises
the same "neither provided" path with a different assertion — both continue to pass since the
error behavior for "no arguments at all" doesn't change, only its cause (was "no `required`
field", now "handler's own exactly-one-of check") is implemented differently.

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && DYLD_LIBRARY_PATH=/opt/homebrew/lib ../.venv/bin/python -m pytest tests/test_mcp_tools.py -v`
Expected: the 6 new tests FAIL — `test_set_volume_delta_increases_from_current_volume` etc. fail
because `args["volume"]` (accessed unconditionally today) raises `KeyError` for a delta-only
call, which the existing `except (KeyError, ValueError, TypeError)` catches and turns into an
`isError` response instead of actually adjusting the volume — so `fake.volume` stays `100`
instead of becoming `60`. `test_set_volume_both_volume_and_delta_returns_error` FAILS because
today `volume=50` alone is honored (no mutual-exclusion check exists yet) — `fake.volume` becomes
`50`, not staying `100`, and `isError` is not set.

- [ ] **Step 3: Implement**

In `agent-assistant/a2a_client/mcp_tools.py`, replace the `self.audio.set_volume` `TOOL_DEFS`
entry (lines 12-20):

```python
    {
        "name": "self.audio.set_volume",
        "description": (
            "Set the speaker volume. Provide either volume (absolute percentage 0-100) "
            "or delta (relative change, e.g. +10 or -10) — not both."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "volume": {"type": "integer", "minimum": 0, "maximum": 100},
                "delta": {"type": "integer", "minimum": -100, "maximum": 100},
            },
        },
    },
```

Replace the `_call_tool` branch (lines 70-73):

```python
        elif name == "self.audio.set_volume":
            has_volume = "volume" in args
            has_delta = "delta" in args
            if has_volume and has_delta:
                return _error_result("provide either volume or delta, not both")
            if not has_volume and not has_delta:
                return _error_result("missing volume or delta")
            if has_volume:
                new_volume = max(0, min(100, int(args["volume"])))
            else:
                new_volume = max(0, min(100, ctx.get_volume_pct() + int(args["delta"])))
            ctx.set_volume_pct(new_volume)
            text = f"volume set to {new_volume}%"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && DYLD_LIBRARY_PATH=/opt/homebrew/lib ../.venv/bin/python -m pytest tests/test_mcp_tools.py -v`
Expected: all tests PASS (13 pre-existing + 6 new = 19).

- [ ] **Step 5: Run the full client test suite to check for regressions**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && DYLD_LIBRARY_PATH=/opt/homebrew/lib ../.venv/bin/python -m pytest tests/ -v`
Expected: all PASS (54 total: 48 pre-existing + 6 new).

- [ ] **Step 6: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
git add a2a_client/mcp_tools.py tests/test_mcp_tools.py
git commit -m "feat: support relative volume delta in self.audio.set_volume"
```

---

### Task 2: ESP32 firmware — `self.audio.set_volume` delta support

**Files:**
- Modify: `esp32-assistant/components/mcp_tools/audio_tools.c` (full file — small, shown below)

**Interfaces:**
- Consumes: `audio_adjust_volume(int delta) -> int` (already exists, `components/audio/include/audio.h:18` — clamped 0-100, returns the new volume, already used by the physical button handler in `main.c`). `audio_set_volume(int pct)` (already exists, unchanged use).
- Produces: no new public interface — this is a leaf firmware file; nothing else in the codebase calls into it directly (registration is via the `LUGO_MCP_TOOL` linker-section macro, unchanged).

There is no automated test for this file (no `test_audio_tools.c` exists in this repo's 10
host-testable C suites, and building a new hardware-mocking harness for a two-branch conditional
is disproportionate to this change — see the design spec's Testing section for the rationale).
Verification is a full re-run of `cd test && make test` (regression check on the shared
`mcp_server` dispatch/validation code this change relies on) plus the manual trace in Step 2
below.

- [ ] **Step 1: Implement**

Replace the entire contents of `esp32-assistant/components/mcp_tools/audio_tools.c` with:

```c
// esp32-assistant/components/mcp_tools/audio_tools.c
#include "mcp_tools.h"
#include "audio.h"

// Set by main.c at startup (mcp_tools_set_volume_hook) so a voice-driven
// volume change shows the same "Volume NN%" overlay + auto-revert as the
// physical Vol +/- buttons (main.c:on_button), instead of changing the level
// silently. NULL until main.c registers it, and in host tests.
static void (*s_volume_hook)(int) = NULL;

void mcp_tools_set_volume_hook(void (*cb)(int)) { s_volume_hook = cb; }

static mcp_result_t set_volume_fn(const char *args) {
    int v = mcp_arg_int(args, "volume", -1);  // -1 sentinel = not provided
    int d = mcp_arg_int(args, "delta", 0);    // 0 sentinel = not provided (delta=0 is a no-op anyway)
    int new_v;
    if (v >= 0 && d != 0) return mcp_err("provide either volume or delta, not both");
    if (v >= 0) {
        audio_set_volume(v);
        new_v = v;
    } else if (d != 0) {
        new_v = audio_adjust_volume(d);
    } else {
        return mcp_err("missing volume or delta");
    }
    if (s_volume_hook) s_volume_hook(new_v);
    return mcp_ok_text("volume set to %d", new_v);
}
static const mcp_prop_t set_volume_props[] = {
    {"volume", MCP_PROP_INT_T, 0, 100, false},
    {"delta", MCP_PROP_INT_T, -100, 100, false},
    MCP_PROP_END,
};
LUGO_MCP_TOOL(tool_set_volume) {
    .name = "self.audio.set_volume",
    .description = "Set speaker volume: pass volume (0-100 absolute) or delta (e.g. +10/-10 relative) - not both",
    .props = set_volume_props, .requires_confirm = false, .fn = set_volume_fn,
};
```

Note: the two `mcp_prop_t` entries are written as raw struct literals (not the `MCP_PROP_INT(...)`
macro) because that macro hardcodes `required=true` — this file is the only current caller
needing an optional int prop, so a raw literal keeps the change local instead of adding a new
macro to the shared `mcp_server.h` for a one-off need.

- [ ] **Step 2: Manually trace validation for each case**

No test file exists for this handler (see above), so trace these five cases by reading the code
before moving on — this replaces the RED/GREEN cycle used elsewhere in this plan:

1. `{"volume": 70}` → `v=70, d=0` → `v>=0` branch → `audio_set_volume(70)`, `new_v=70` → `"volume set to 70"`.
2. `{"delta": 10}` → `v=-1, d=10` → `v>=0` is false, `d!=0` is true → `audio_adjust_volume(10)` (already clamps 0-100 internally) → `new_v` = its return value.
3. `{"volume": 70, "delta": 10}` → `v=70, d=10` → `v>=0 && d!=0` → `mcp_err("provide either volume or delta, not both")`, no audio call made.
4. `{}` → `v=-1, d=0` → neither branch taken → falls to `else` → `mcp_err("missing volume or delta")`.
5. `{"delta": 0}` → `v=-1, d=0` → indistinguishable from case 4 (documented sentinel collision, accepted per the design spec — a delta of exactly 0 is a no-op regardless of whether it means "explicitly zero" or "not provided", so returning the same error is harmless).

- [ ] **Step 3: Compile-check via the host test build (regression on the shared framework)**

Run: `cd /Users/lugon/code/speech-text-transformer/esp32-assistant/test && make test`
Expected: `ALL PASS` (or equivalent per-suite pass output) — this doesn't compile
`audio_tools.c` itself (it's not part of any host-testable suite), but confirms the
`mcp_server` dispatch/validation code (`test_mcp_server`) this change depends on for the
optional-prop behavior is unaffected.

Also run a real ESP-IDF build to confirm `audio_tools.c` itself compiles (it's not exercised by
the host test suite at all): `idf.py build` from `/Users/lugon/code/speech-text-transformer/esp32-assistant`
(uses the existing `build/` directory's configured target — do not run this against `build-wokwi/`,
which has audio init skipped and isn't the right config to validate this file against).

- [ ] **Step 4: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/esp32-assistant
git add components/mcp_tools/audio_tools.c
git commit -m "feat(mcp): support relative volume delta in self.audio.set_volume"
```

---

## Self-Review Notes

- **Spec coverage:** Design spec's RPi section → Task 1. ESP32 section → Task 2. Testing
  section's "no new C test file" rationale → Task 2's own note, matching verbatim.
- **Placeholder scan:** no TBD/TODO; every step shows exact code or exact command.
- **Type/name consistency:** `McpToolContext.get_volume_pct`/`set_volume_pct` names match
  between Task 1's usage and the pre-existing dataclass (unchanged, not redefined here).
  `audio_adjust_volume`/`audio_set_volume` names match between Task 2's usage and their existing
  declarations in `components/audio/include/audio.h` (not modified by this plan).
