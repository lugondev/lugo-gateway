# Relative volume (delta) for `self.audio.set_volume`

Date: 2026-07-12
Repos touched: `agent-assistant` (RPi client), `esp32-assistant` (ESP32 firmware)

## Context

Follow-up to the RPi MCP device-tools plan (`2026-07-12-rpi-mcp-tools-design.md`, now merged).
`self.audio.set_volume` currently only accepts an absolute `volume` (0-100) on both clients. A
voice command like "turn the volume up" requires the LLM to first call
`self.get_device_status`, compute a new absolute value, then call `set_volume` — a two-step
inference the LLM isn't guaranteed to perform. This adds a `delta` parameter so "up"/"down"
style commands map directly to one tool call.

ESP32 firmware has the identical limitation (`components/mcp_tools/audio_tools.c`) and is
updated for parity in the same pass, since the fix is small on that side: `audio_adjust_volume(int delta)`
already exists in `components/audio/include/audio.h` (used by the physical Vol+/Vol− buttons,
±10, already clamped 0-100) — the firmware change is exposing it via MCP, not building new
volume logic.

## Design

### RPi client — `agent-assistant/a2a_client/mcp_tools.py`

`self.audio.set_volume`'s `inputSchema` becomes:
```json
{"type": "object", "properties": {
  "volume": {"type": "integer", "minimum": 0, "maximum": 100},
  "delta": {"type": "integer", "minimum": -100, "maximum": 100}
}}
```
No `required` list — exactly one of `volume`/`delta` must be present, enforced in the handler,
not the schema (mirrors how ESP32's framework only enforces JSON-Schema-level `required`, not
mutual exclusion). Description: "Set the speaker volume. Provide either `volume` (absolute
percentage 0-100) or `delta` (relative change, e.g. +10 or -10) — not both."

Handler logic in `_call_tool`'s `self.audio.set_volume` branch:
- both present → error `"provide either volume or delta, not both"`
- neither present → error `"missing volume or delta"`
- `volume` present → `new_volume = clamp(int(args["volume"]), 0, 100)`
- `delta` present → `new_volume = clamp(ctx.get_volume_pct() + int(args["delta"]), 0, 100)`
- either way: `ctx.set_volume_pct(new_volume)`, response text always reports the resulting
  absolute percentage (`f"volume set to {new_volume}%"`), never just the requested delta — so
  the LLM/user learns the real outcome, not an unclamped intention.

No changes to `McpToolContext`, `audio_io.py`, or `service.py` — `get_volume_pct`/`set_volume_pct`
already exist and already do everything this needs.

### ESP32 firmware — `esp32-assistant/components/mcp_tools/audio_tools.c`

`set_volume_props` gains a second, non-required entry for `delta` (range -100..100); `volume`'s
existing `MCP_PROP_INT` macro (which hardcodes `required=true`) is replaced with a raw
`mcp_prop_t` struct literal setting `required=false` for both props — scoped to this file only,
no change to the shared `MCP_PROP_INT` macro or `mcp_server.h` (avoids widening blast radius for
a need only this one tool currently has). Confirmed via `mcp_server.c:67`
(`if (!v) { if (p->required) return "missing required argument"; continue; }`) that the
dispatch framework only rejects a missing prop when `required=true` is set — making both optional
here shifts the "at least one" rule into `set_volume_fn` itself, same division of responsibility
already used for other validation in this codebase.

`set_volume_fn` becomes:
```c
static mcp_result_t set_volume_fn(const char *args) {
    int v = mcp_arg_int(args, "volume", -1);   // -1 sentinel = not provided
    int d = mcp_arg_int(args, "delta", 0);     // 0 sentinel = not provided (delta=0 is a no-op anyway)
    int new_v;
    if (v >= 0 && d != 0) return mcp_err("provide either volume or delta, not both");
    if (v >= 0) { audio_set_volume(v); new_v = v; }
    else if (d != 0) { new_v = audio_adjust_volume(d); }
    else return mcp_err("missing volume or delta");
    if (s_volume_hook) s_volume_hook(new_v);
    return mcp_ok_text("volume set to %d", new_v);
}
```
Description updated to: "Set speaker volume: pass volume (0-100 absolute) or delta (e.g.
+10/-10 relative) — not both."

## Testing

- RPi: extend `tests/test_mcp_tools.py` — delta increases/decreases volume correctly, delta
  clamps at 0/100 boundaries, error when both `volume` and `delta` given, error when neither
  given. All exercised via the existing `_FakeCtx` pattern already in the file.
- ESP32: **no new C test file.** `audio_tools.c`'s handlers aren't covered by any of the
  repo's existing 10 host-testable C suites today (confirmed: no `test_audio_tools.c` exists),
  and building a fresh hardware-mocking harness for a two-branch conditional is disproportionate
  to this change's size. Verification is a full re-run of `cd test && make test` (regression
  check on the shared `mcp_server` dispatch/validation code this change relies on) plus manual
  code review against `mcp_server.c`'s validation flow (already done during design, see above).

## Non-goals

- No change to `requires_confirm` — volume changes stay non-destructive/no-confirm on both
  clients, same as before.
- No new dedicated `volume_up`/`volume_down` tools — one tool, two ways to call it (per the
  approved design decision).
- No change to the physical Vol+/Vol− button path on ESP32 (`main.c`'s button handler already
  calls `audio_adjust_volume` directly) — this only adds the MCP-facing route to the same
  existing function.
