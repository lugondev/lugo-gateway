# RPi MCP Device Tools via Lugo Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `agent-assistant` (Raspberry Pi) voice client the same MCP tool-calling capability `esp32-assistant` already has, by switching it onto the gateway's `/v1/lugo/stream` endpoint, without regressing the RPi client's existing session-resume and cold-start-warming UX.

**Architecture:** Two independently-deployed repos change. (1) `speech-text-transformer/apps/api_gateway/app/api/routes/lugo.py` gets four small additive changes so its wire adapter stops discarding data the core `ConversationSession` already produces (resume id, `stt_ready`/`tts_ready`, `engines_ready`, and turn-detail events) — safe for the existing ESP32 fleet, which ignores unrecognized message types. (2) `agent-assistant` switches its WebSocket client from `/v1/conversation/stream` (JSON control + raw Opus) to `/v1/lugo/stream` (client-speaks-first handshake, v3-framed downlink audio), and gains a new on-device MCP JSON-RPC dispatcher exposing 4 tools (status, volume, idle, screen text) backed by a software playback-gain volume control and a repurposed RMS detector for idle-wake.

**Tech Stack:** Gateway: Python 3, FastAPI, pytest (`TestClient(...).websocket_connect`). Client: Python 3, `websockets`, `sounddevice`, `opuslib`, `numpy`, pytest.

## Global Constraints

- Gateway changes touch only `lugo.py`'s wire-translation layer — no changes to `ConversationSession`/`session.py` (the core already emits everything needed).
- Every new/changed lugo wire message type must be one ESP32 firmware safely ignores if unhandled (confirmed: `main.c:516` — `default: break; // LUGO_EV_UNKNOWN`). Do not change any message type ESP32 firmware already parses (`welcome`, `stt`, `tts{state}`, `mcp`, `goodbye`, `error` — see `components/lugo_protocol/lugo_protocol.c:126-148`).
- `agent-assistant` fully replaces `/v1/conversation/stream` support — no dual-protocol config flag, no `audio_out=url`/WAV fallback path kept.
- The 4 RPi MCP tools (`self.get_device_status`, `self.audio.set_volume`, `self.device.idle`, `self.screen.show_text`) never require `annotations.requiresConfirm` — no `shutdown` or raw `gpio.set` tool in this pass.
- Rollout order: gateway tasks (1-4) must be deployable before client tasks (5-13) ship, since the new client hard-depends on lugo's resume/engines_ready additions and no longer speaks the old protocol at all.
- Run gateway tests from `speech-text-transformer/` with the repo's venv: `.venv/bin/python -m pytest tests/unit/test_lugo_stream.py -v` (adjust path per task).
- Run client tests from `agent-assistant/` with the shared root venv: `../.venv/bin/python -m pytest tests/test_x.py -v` (adjust path per task).

---

## Part A — Gateway (`speech-text-transformer` repo)

### Task 1: Thread `wakeup.session_id` through to `resume_sid`

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py:91` (the `session_id = str(uuid.uuid4())` line and the `cfg = SessionRuntimeConfig(...)` block at lines 100-107)
- Test: `tests/unit/test_lugo_stream.py`

**Interfaces:**
- Consumes: `ConversationSession`/`SessionRuntimeConfig(resume_sid=...)` (existing, in `app/services/conversation/session.py`) — already reads `cfg.resume_sid` and calls `session_store.exists(cfg.resume_sid)` / `session_store.get_messages(cfg.resume_sid)` to seed history. No change needed there.
- Produces: nothing new consumed by later tasks — this is a leaf change.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_lugo_stream.py` (same file, same `_hermetic` fixture already present):

```python
def test_wakeup_with_session_id_resumes_and_echoes_same_id():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev", "session_id": "resume-me-123",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] == "resume-me-123"


def test_wakeup_without_session_id_gets_a_fresh_uuid():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["session_id"] != "resume-me-123"
        assert len(msg["session_id"]) == 36  # uuid4 string form
```

- [ ] **Step 2: Run tests to verify the first one fails**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_lugo_stream.py -k "session_id" -v`
Expected: `test_wakeup_with_session_id_resumes_and_echoes_same_id` FAILS (`msg["session_id"] != "resume-me-123"`, it's a fresh uuid instead); `test_wakeup_without_session_id_gets_a_fresh_uuid` PASSES already (current behavior).

- [ ] **Step 3: Implement**

In `apps/api_gateway/app/api/routes/lugo.py`, replace line 91:

```python
    session_id = str(uuid.uuid4())
```

with:

```python
    requested_sid = hello.get("session_id")
    if not isinstance(requested_sid, str) or not requested_sid:
        requested_sid = None
    session_id = requested_sid or str(uuid.uuid4())
```

and in the `cfg = SessionRuntimeConfig(...)` call (line 106), replace:

```python
        denoise=False, resume_sid=None, stt_model=stt_model,
```

with:

```python
        denoise=False, resume_sid=requested_sid, stt_model=stt_model,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_lugo_stream.py -v`
Expected: all tests in the file PASS, including both new ones.

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer
git add apps/api_gateway/app/api/routes/lugo.py tests/unit/test_lugo_stream.py
git commit -m "feat(lugo): resume session from wakeup.session_id instead of always minting a new one"
```

---

### Task 2: Surface `stt_ready`/`tts_ready` in `welcome`

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py` (the `emit()` closure at lines 116-139, and the `welcome` send at lines 146-149)
- Test: `tests/unit/test_lugo_stream.py`

**Interfaces:**
- Consumes: the `session_started` core event's `stt_ready`/`tts_ready` kwargs, emitted synchronously inside `await session.start()` (confirmed in `app/services/conversation/session.py`: `await self.emit("session_started", ..., stt_ready=stt_ready, tts_ready=tts_ready)` — this completes before `session.start()` returns).
- Produces: `welcome` message now includes `"stt_ready": bool, "tts_ready": bool` — Task 3's `engines_ready` forwarding and the client (Task 10) both depend on this field existing.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_lugo_stream.py`:

```python
def test_welcome_includes_engine_ready_flags():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["stt_ready"] is True
        assert msg["tts_ready"] is True
```

(The `_hermetic` fixture's stub STT/TTS providers are ready immediately, so both flags are `True` here — this test only checks the fields exist and propagate, not the cold-load timing itself.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_lugo_stream.py -k engine_ready_flags -v`
Expected: FAIL with `KeyError: 'stt_ready'`.

- [ ] **Step 3: Implement**

In `apps/api_gateway/app/api/routes/lugo.py`, just above the `async def emit(...)` definition (before line 116), add a mutable holder:

```python
    engine_status = {"stt_ready": True, "tts_ready": True}
```

Inside `emit()`, add a branch that captures `session_started`'s flags (insert before the final comment line, i.e. replace line 139's comment line with a real branch — keep the comment for the events still genuinely not forwarded):

```python
        elif event == "session_started":
            engine_status["stt_ready"] = bool(payload.get("stt_ready", True))
            engine_status["tts_ready"] = bool(payload.get("tts_ready", True))
        # processing / audio_end / reset: not on the wire (speech_start/speech_end/
        # processing/engines_ready are handled in Task 3/4 below)
```

Then change the `welcome` send (lines 146-149) from:

```python
    await websocket.send_json({
        "type": "welcome", "session_id": session_id, "transport": "websocket",
        "audio_params": {"sample_rate": out_sr}, "idle_timeout_s": idle,
    })
```

to:

```python
    await websocket.send_json({
        "type": "welcome", "session_id": session_id, "transport": "websocket",
        "audio_params": {"sample_rate": out_sr}, "idle_timeout_s": idle,
        "stt_ready": engine_status["stt_ready"], "tts_ready": engine_status["tts_ready"],
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_lugo_stream.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer
git add apps/api_gateway/app/api/routes/lugo.py tests/unit/test_lugo_stream.py
git commit -m "feat(lugo): include stt_ready/tts_ready in welcome"
```

---

### Task 3: Forward `engines_ready`

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py` (`emit()` closure)
- Test: `tests/unit/test_lugo_stream.py`

**Interfaces:**
- Consumes: the core `engines_ready` event (`app/services/conversation/session.py`: `await self.emit("engines_ready")`, fired later by the background `_warm_and_notify` task once both providers are actually ready, only if they weren't ready already at `session_started` time).
- Produces: a new `{"type": "engines_ready"}` wire message the client (Task 11) listens for.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_lugo_stream.py`. This needs providers that start out NOT ready so `engines_ready` actually fires; reuse the pattern from `is_ready`/`warm_providers` — simplest is to monkeypatch `app.services.conversation.session.is_ready` to return `False` once then patch nothing else (the real stub providers warm instantly, so `engines_ready` fires almost immediately after `welcome`):

```python
def test_engines_ready_is_forwarded_when_initially_cold(monkeypatch):
    monkeypatch.setattr("app.services.conversation.session.is_ready", lambda _provider: False)
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        welcome = ws.receive_json()
        assert welcome["stt_ready"] is False
        assert welcome["tts_ready"] is False
        msg = ws.receive_json()
        assert msg["type"] == "engines_ready"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_lugo_stream.py -k engines_ready_is_forwarded -v`
Expected: FAIL — either `welcome["stt_ready"]` isn't `False` (if `is_ready` patch point is wrong) or the second `ws.receive_json()` call times out/errors (no `engines_ready` message is ever sent today).

If the `is_ready` patch target is wrong (check by running with `-s` and looking for import errors), grep first: `grep -n "^from\|^import" apps/api_gateway/app/services/conversation/session.py | grep is_ready` to confirm the exact import path before adjusting the monkeypatch target — `session.py` must import `is_ready` by name (`from ... import is_ready`) for patching it on the `session` module to take effect at call time, same reasoning as the `opus_available` patch already used in `test_lugo_stream.py:154`.

- [ ] **Step 3: Implement**

In `apps/api_gateway/app/api/routes/lugo.py`'s `emit()`, add:

```python
        elif event == "engines_ready":
            await websocket.send_json({"type": "engines_ready"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_lugo_stream.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer
git add apps/api_gateway/app/api/routes/lugo.py tests/unit/test_lugo_stream.py
git commit -m "feat(lugo): forward engines_ready to the device"
```

---

### Task 4: Forward `speech_start`/`speech_end`/`processing` and the `aborted` reason

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py` (`emit()` closure)
- Test: `tests/unit/test_lugo_stream.py`

**Interfaces:**
- Consumes: core events `speech_start` (no payload), `speech_end` (`speech_ms: int`), `processing` (`turn: int`), `aborted` (`reason: str`) — all confirmed emitted in `app/services/conversation/session.py` (`emit("speech_start")`, `emit("speech_end", speech_ms=...)`, `emit("processing", turn=...)`, `emit("aborted", reason=...)`).
- Produces: new wire messages `{"type":"speech_start"}`, `{"type":"speech_end","speech_ms":int}`, `{"type":"processing","turn":int}`, and an added `"reason"` key on the existing `{"type":"tts","state":"stop"}` message when the stop was caused by an abort. The client (Task 11) branches on `"reason"` presence to distinguish a normal turn end from a barge-in/abort.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_lugo_stream.py`:

```python
def test_speech_and_processing_events_are_forwarded():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        types_seen = []
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            types_seen.append(m["type"])
            if m["type"] == "tts" and m.get("state") == "stop":
                break
        # A text turn has no speech_start/speech_end (those are audio-VAD-driven),
        # but "processing" fires for every turn regardless of input modality.
        assert "processing" in types_seen


def test_aborted_reason_is_included_on_tts_stop():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        # Wait for the turn to actually start speaking, then abort mid-reply.
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            if m["type"] == "tts" and m.get("state") == "start":
                break
        ws.send_json({"type": "abort"})
        for _ in range(30):
            message = ws.receive()
            if message.get("bytes") is not None:
                continue
            m = json.loads(message["text"])
            if m["type"] == "tts" and m.get("state") == "stop":
                assert m.get("reason") == "barge-in"
                return
        raise AssertionError("never saw tts stop after abort")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_lugo_stream.py -k "speech_and_processing or aborted_reason" -v`
Expected: `test_speech_and_processing_events_are_forwarded` FAILS (`"processing" not in types_seen`); `test_aborted_reason_is_included_on_tts_stop` FAILS (`m.get("reason")` is `None`).

- [ ] **Step 3: Implement**

In `apps/api_gateway/app/api/routes/lugo.py`'s `emit()`, add three new branches and adjust the existing `turn_done`/`aborted` branch. Replace:

```python
        elif event in ("turn_done", "aborted"):
            if speaking:
                speaking = False
                await websocket.send_json({"type": "tts", "state": "stop"})
        elif event == "command":
```

with:

```python
        elif event in ("turn_done", "aborted"):
            if speaking:
                speaking = False
                stop_msg = {"type": "tts", "state": "stop"}
                if event == "aborted" and payload.get("reason"):
                    stop_msg["reason"] = payload["reason"]
                await websocket.send_json(stop_msg)
        elif event == "speech_start":
            await websocket.send_json({"type": "speech_start"})
        elif event == "speech_end":
            await websocket.send_json({"type": "speech_end", "speech_ms": payload.get("speech_ms", 0)})
        elif event == "processing":
            await websocket.send_json({"type": "processing", "turn": payload.get("turn", 0)})
        elif event == "command":
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/test_lugo_stream.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full gateway unit test suite once to check for regressions**

Run: `cd /Users/lugon/code/speech-text-transformer && .venv/bin/python -m pytest tests/unit/ -v`
Expected: all PASS (in particular `test_lugo_idle_timeout.py` and `test_lugo_device_mcp.py`, which exercise the same `emit()` closure and must be unaffected by these additions).

- [ ] **Step 6: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer
git add apps/api_gateway/app/api/routes/lugo.py tests/unit/test_lugo_stream.py
git commit -m "feat(lugo): forward speech_start/speech_end/processing and abort reason"
```

**Gateway work is now deployable.** Tasks 5-13 (below) implement the RPi client against this behavior.

---

## Part B — RPi client (`agent-assistant` repo)

### Task 5: `lugo_frame.py` — v3 binary frame codec

**Files:**
- Create: `a2a_client/lugo_frame.py`
- Test: `tests/test_lugo_frame.py`

**Interfaces:**
- Produces: `LUGO_FRAME_OPUS = 0`, `LUGO_FRAME_JSON = 1`, `encode_frame(frame_type: int, payload: bytes) -> bytes`, `decode_frame(data: bytes) -> tuple[int, bytes]`. Task 10 (`service.py` binary-frame handling) imports `LUGO_FRAME_OPUS` and `decode_frame` from here.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lugo_frame.py`:

```python
import pytest

from a2a_client.lugo_frame import LUGO_FRAME_OPUS, LUGO_FRAME_JSON, decode_frame, encode_frame


def test_encode_frame_produces_expected_bytes():
    assert encode_frame(LUGO_FRAME_OPUS, b"ab") == b"\x00\x00\x00\x02ab"


def test_encode_decode_round_trip():
    frame_type, payload = decode_frame(encode_frame(LUGO_FRAME_OPUS, b"hello opus"))
    assert frame_type == LUGO_FRAME_OPUS
    assert payload == b"hello opus"


def test_encode_decode_round_trip_json_type():
    frame_type, payload = decode_frame(encode_frame(LUGO_FRAME_JSON, b"{}"))
    assert frame_type == LUGO_FRAME_JSON
    assert payload == b"{}"


def test_encode_decode_empty_payload():
    frame_type, payload = decode_frame(encode_frame(LUGO_FRAME_OPUS, b""))
    assert frame_type == LUGO_FRAME_OPUS
    assert payload == b""


def test_decode_frame_shorter_than_header_raises():
    with pytest.raises(ValueError, match="shorter than header"):
        decode_frame(b"\x00\x00")


def test_decode_frame_payload_size_mismatch_raises():
    # Header claims 5 bytes of payload but only 2 are present.
    with pytest.raises(ValueError, match="size mismatch"):
        decode_frame(b"\x00\x00\x00\x05ab")


def test_encode_frame_rejects_oversized_payload():
    with pytest.raises(ValueError, match="too large"):
        encode_frame(LUGO_FRAME_OPUS, b"x" * 65536)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/test_lugo_frame.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'a2a_client.lugo_frame'`.

- [ ] **Step 3: Implement**

Create `a2a_client/lugo_frame.py`:

```python
from __future__ import annotations

import struct

LUGO_FRAME_OPUS = 0
LUGO_FRAME_JSON = 1

_HEADER = struct.Struct(">BBH")  # type, reserved, payload_size
_MAX_PAYLOAD = 0xFFFF


def encode_frame(frame_type: int, payload: bytes) -> bytes:
    if len(payload) > _MAX_PAYLOAD:
        raise ValueError(f"payload too large: {len(payload)} > {_MAX_PAYLOAD}")
    return _HEADER.pack(frame_type & 0xFF, 0, len(payload)) + payload


def decode_frame(data: bytes) -> tuple[int, bytes]:
    if len(data) < _HEADER.size:
        raise ValueError("frame shorter than header")
    frame_type, _reserved, size = _HEADER.unpack(data[: _HEADER.size])
    payload = data[_HEADER.size :]
    if len(payload) != size:
        raise ValueError(f"payload size mismatch: header={size} actual={len(payload)}")
    return frame_type, payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/test_lugo_frame.py -v`
Expected: all 7 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
git add a2a_client/lugo_frame.py tests/test_lugo_frame.py
git commit -m "feat: add lugo v3 binary frame codec"
```

---

### Task 6: `mcp_tools.py` — JSON-RPC dispatcher and 4 device tools

**Files:**
- Create: `a2a_client/mcp_tools.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: nothing from other tasks (fully self-contained; hardware access is injected via `McpToolContext` callables, not imported directly).
- Produces: `McpToolContext` (dataclass with 5 callable fields: `get_volume_pct: Callable[[], int]`, `set_volume_pct: Callable[[int], None]`, `uptime_seconds: Callable[[], float]`, `go_idle: Callable[[], None]`, `show_text: Callable[[str, str], None]`), `TOOL_DEFS: list[dict]`, `handle_mcp_request(payload: dict, ctx: McpToolContext) -> dict`. Task 12 (`service.py` MCP wiring) imports `McpToolContext` and `handle_mcp_request`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_tools.py`:

```python
from a2a_client.mcp_tools import McpToolContext, handle_mcp_request


class _FakeCtx:
    def __init__(self) -> None:
        self.volume = 100
        self.idle_calls = 0
        self.shown: list[tuple[str, str]] = []

    def get_volume_pct(self) -> int:
        return self.volume

    def set_volume_pct(self, pct: int) -> None:
        self.volume = pct

    def uptime_seconds(self) -> float:
        return 42.0

    def go_idle(self) -> None:
        self.idle_calls += 1

    def show_text(self, line1: str, line2: str) -> None:
        self.shown.append((line1, line2))


def _ctx() -> McpToolContext:
    fake = _FakeCtx()
    return McpToolContext(
        get_volume_pct=fake.get_volume_pct,
        set_volume_pct=fake.set_volume_pct,
        uptime_seconds=fake.uptime_seconds,
        go_idle=fake.go_idle,
        show_text=fake.show_text,
    ), fake


def test_initialize_returns_result_with_matching_id():
    ctx, _ = _ctx()
    resp = handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, ctx)
    assert resp["id"] == 1
    assert "result" in resp


def test_tools_list_returns_all_four_tools():
    ctx, _ = _ctx()
    resp = handle_mcp_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ctx)
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {
        "self.get_device_status",
        "self.audio.set_volume",
        "self.device.idle",
        "self.screen.show_text",
    }


def test_tools_list_tools_have_no_confirm_annotation():
    ctx, _ = _ctx()
    resp = handle_mcp_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ctx)
    for tool in resp["result"]["tools"]:
        assert not (tool.get("annotations") or {}).get("requiresConfirm")


def test_get_device_status_reports_volume_and_uptime():
    ctx, fake = _ctx()
    fake.volume = 77
    resp = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "self.get_device_status", "arguments": {}}},
        ctx,
    )
    text = resp["result"]["content"][0]["text"]
    assert "77" in text
    assert "42" in text


def test_set_volume_clamps_and_updates_context():
    ctx, fake = _ctx()
    resp = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "self.audio.set_volume", "arguments": {"volume": 150}}},
        ctx,
    )
    assert fake.volume == 100
    assert "100" in resp["result"]["content"][0]["text"]
    assert not resp["result"].get("isError")


def test_set_volume_missing_argument_returns_error():
    ctx, fake = _ctx()
    resp = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "self.audio.set_volume", "arguments": {}}},
        ctx,
    )
    assert resp["result"]["isError"] is True
    assert fake.volume == 100  # unchanged


def test_device_idle_calls_context():
    ctx, fake = _ctx()
    handle_mcp_request(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "self.device.idle", "arguments": {}}},
        ctx,
    )
    assert fake.idle_calls == 1


def test_screen_show_text_calls_context_with_both_lines():
    ctx, fake = _ctx()
    handle_mcp_request(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "self.screen.show_text",
                    "arguments": {"line1": "hello", "line2": "world"}}},
        ctx,
    )
    assert fake.shown == [("hello", "world")]


def test_screen_show_text_defaults_line2_to_empty():
    ctx, fake = _ctx()
    handle_mcp_request(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "self.screen.show_text", "arguments": {"line1": "hi"}}},
        ctx,
    )
    assert fake.shown == [("hi", "")]


def test_unknown_tool_returns_is_error():
    ctx, _ = _ctx()
    resp = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "self.nonexistent", "arguments": {}}},
        ctx,
    )
    assert resp["result"]["isError"] is True


def test_unknown_method_returns_json_rpc_error():
    ctx, _ = _ctx()
    resp = handle_mcp_request({"jsonrpc": "2.0", "id": 10, "method": "bogus"}, ctx)
    assert resp["id"] == 10
    assert "error" in resp
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/test_mcp_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'a2a_client.mcp_tools'`.

- [ ] **Step 3: Implement**

Create `a2a_client/mcp_tools.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

TOOL_DEFS: list[dict] = [
    {
        "name": "self.get_device_status",
        "description": "Get current device status: speaker volume percent and uptime in seconds.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "self.audio.set_volume",
        "description": "Set the speaker volume as a percentage (0-100).",
        "inputSchema": {
            "type": "object",
            "properties": {"volume": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["volume"],
        },
    },
    {
        "name": "self.device.idle",
        "description": "Mute the microphone and show an idle indicator until a loud sound wakes the device.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "self.screen.show_text",
        "description": "Show up to two lines of text on the device's screen temporarily.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "line1": {"type": "string"},
                "line2": {"type": "string"},
            },
            "required": ["line1"],
        },
    },
]

_TOOL_NAMES = {t["name"] for t in TOOL_DEFS}


@dataclass
class McpToolContext:
    get_volume_pct: Callable[[], int]
    set_volume_pct: Callable[[int], None]
    uptime_seconds: Callable[[], float]
    go_idle: Callable[[], None]
    show_text: Callable[[str, str], None]


def _error_result(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _ok_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _call_tool(name: str, args: dict, ctx: McpToolContext) -> dict:
    if name not in _TOOL_NAMES:
        return _error_result(f"unknown tool: {name}")
    try:
        if name == "self.get_device_status":
            text = f"volume={ctx.get_volume_pct()}% uptime={ctx.uptime_seconds():.0f}s"
        elif name == "self.audio.set_volume":
            volume = max(0, min(100, int(args["volume"])))
            ctx.set_volume_pct(volume)
            text = f"volume set to {volume}%"
        elif name == "self.device.idle":
            ctx.go_idle()
            text = "device is now idle"
        else:  # self.screen.show_text
            line1 = str(args["line1"])
            line2 = str(args.get("line2", ""))
            ctx.show_text(line1, line2)
            text = "screen updated"
    except (KeyError, ValueError, TypeError) as exc:
        return _error_result(f"bad arguments: {exc}")
    return _ok_result(text)


def handle_mcp_request(payload: dict, ctx: McpToolContext) -> dict:
    mid = payload.get("id")
    method = payload.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-assistant", "version": "1.0.0"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOL_DEFS}}
    if method == "tools/call":
        params = payload.get("params") or {}
        result = _call_tool(params.get("name", ""), params.get("arguments") or {}, ctx)
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method: {method}"}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/test_mcp_tools.py -v`
Expected: all 11 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
git add a2a_client/mcp_tools.py tests/test_mcp_tools.py
git commit -m "feat: add MCP JSON-RPC dispatcher and 4 device tools"
```

---

### Task 7: Software volume gain in `AudioIO`

**Files:**
- Modify: `a2a_client/audio_io.py` (`__init__` around line 50, `on_output_audio` at lines 176-183)
- Test: `tests/test_audio_io_volume.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `AudioIO.get_volume_pct() -> int`, `AudioIO.set_volume_pct(pct: int) -> None` (clamped 0-100). Task 12 wires `ctx.get_volume_pct`/`ctx.set_volume_pct` to these two methods.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_audio_io_volume.py`. Reuse the exact `_config`/`_audio` helpers from `tests/test_audio_io_tone.py` (copy them — the existing file doesn't export them for reuse, and duplicating a 40-line config-builder fixture is the established pattern in this test suite already, matching `test_ws_protocol.py`'s own separate `_base_config`):

```python
import numpy as np

from a2a_client.audio_io import AudioIO
from a2a_client.config import Config


def _config(**overrides) -> Config:
    defaults = dict(
        host="127.0.0.1", port=8000, secure=False, profile=None,
        output="audio,text", input_sample_rate=16000, output_sample_rate=16000,
        uplink_sample_rate=16000, frame_ms=60, input_channels=1, output_channels=1,
        input_device=None, output_device=None, playback_preroll_ms=200,
        allow_barge_in=False, barge_in_rms_threshold=1200.0, barge_in_min_frames=5,
        reconnect_initial_seconds=1.0, reconnect_max_seconds=20.0, log_events=False,
        led_enabled=False, led_yellow_pin=13, led_red_pin=22, led_green_pin=17,
        input_alsa_device=None, output_alsa_device=None, oled_enabled=False,
        oled_i2c_port=1, oled_i2c_address=0x3C, oled_font_path="",
        session_state_path="/tmp/does-not-matter",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _audio() -> AudioIO:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return AudioIO(config=_config(), loop=loop, logger=lambda msg: None)
    finally:
        loop.close()


def test_default_volume_is_100():
    audio = _audio()
    assert audio.get_volume_pct() == 100


def test_set_volume_pct_clamps_above_100():
    audio = _audio()
    audio.set_volume_pct(150)
    assert audio.get_volume_pct() == 100


def test_set_volume_pct_clamps_below_0():
    audio = _audio()
    audio.set_volume_pct(-10)
    assert audio.get_volume_pct() == 0


def test_on_output_audio_at_full_volume_is_unchanged():
    audio = _audio()
    audio.play_buffer.push(np.full(4000, 1234, dtype=np.int16))
    outdata = np.zeros((100, 1), dtype=np.int16)
    audio.on_output_audio(outdata, 100, None, None)
    assert np.array_equal(outdata[:, 0], np.full(100, 1234, dtype=np.int16))


def test_on_output_audio_applies_half_volume_gain():
    audio = _audio()
    audio.set_volume_pct(50)
    audio.play_buffer.push(np.full(4000, 1000, dtype=np.int16))
    outdata = np.zeros((100, 1), dtype=np.int16)
    audio.on_output_audio(outdata, 100, None, None)
    assert np.allclose(outdata[:, 0], 500, atol=1)


def test_on_output_audio_zero_volume_is_silent():
    audio = _audio()
    audio.set_volume_pct(0)
    audio.play_buffer.push(np.full(4000, 1000, dtype=np.int16))
    outdata = np.zeros((100, 1), dtype=np.int16)
    audio.on_output_audio(outdata, 100, None, None)
    assert np.all(outdata[:, 0] == 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/test_audio_io_volume.py -v`
Expected: FAIL with `AttributeError: 'AudioIO' object has no attribute 'get_volume_pct'`.

- [ ] **Step 3: Implement**

In `a2a_client/audio_io.py`, in `__init__`, right after `self.negotiated_sample_rate = self.config.input_sample_rate` (line 50), add:

```python
        self.volume_pct = 100  # software playback gain, 0-100; set via MCP self.audio.set_volume
```

Add two new methods anywhere near `set_negotiated_sample_rate` (e.g. right after it, around line 239):

```python
    def get_volume_pct(self) -> int:
        return self.volume_pct

    def set_volume_pct(self, pct: int) -> None:
        self.volume_pct = max(0, min(100, int(pct)))
```

Change `on_output_audio` (lines 176-183) from:

```python
    def on_output_audio(self, outdata: np.ndarray, frames: int, _time: Any, status: sd.CallbackFlags) -> None:
        """PortAudio pulls a steady block from the jitter buffer (silence if underrun)."""
        mono = self.play_buffer.pull(frames)
        if self.config.output_channels == 1:
            outdata[:, 0] = mono
        else:
            for ch in range(self.config.output_channels):
                outdata[:, ch] = mono
```

to:

```python
    def on_output_audio(self, outdata: np.ndarray, frames: int, _time: Any, status: sd.CallbackFlags) -> None:
        """PortAudio pulls a steady block from the jitter buffer (silence if underrun)."""
        mono = self.play_buffer.pull(frames)
        if self.volume_pct != 100:
            mono = (mono.astype(np.float32) * (self.volume_pct / 100.0)).astype(np.int16)
        if self.config.output_channels == 1:
            outdata[:, 0] = mono
        else:
            for ch in range(self.config.output_channels):
                outdata[:, ch] = mono
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/test_audio_io_volume.py tests/test_audio_io_tone.py -v`
Expected: all PASS (including the pre-existing tone tests, unaffected since they call `play_tone`/`play_buffer.pull` directly, not `on_output_audio`, except where they do — verify no regression).

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
git add a2a_client/audio_io.py tests/test_audio_io_volume.py
git commit -m "feat: add software playback volume gain to AudioIO"
```

---

### Task 8: Drop the unused `session.output` config field

**Files:**
- Modify: `a2a_client/config.py` (the `Config` dataclass and `load_config`)
- Modify: `config.example.yaml`
- Test: `tests/test_audio_io_tone.py`, `tests/test_ws_protocol.py` (remove `output=` from their `_config`/`_base_config` helpers — done as part of Task 9/later, but the dataclass field removal must happen first since those helpers construct `Config(...)` with every field by keyword)

**Interfaces:**
- Consumes: nothing.
- Produces: `Config` with no `output` attribute. Every test helper building a `Config(...)` elsewhere in the suite must drop the `output=` keyword or the dataclass call fails with `TypeError: unexpected keyword argument`.

- [ ] **Step 1: Remove the field from the dataclass and loader**

In `a2a_client/config.py`, delete the line `output: str` from the `Config` dataclass, and delete the line `output=str(session.get("output", "audio,text")),` from `load_config`. lugo hardcodes `want_audio=True, want_text=True` server-side with no client-selectable toggle, so this setting has had no effect since the client switched off `/v1/conversation/stream` (Task 9 onward) — see `apps/api_gateway/app/api/routes/lugo.py:105`.

- [ ] **Step 2: Update `config.example.yaml`**

Remove the line `  output: audio,text` from the `session:` block.

- [ ] **Step 3: Fix existing tests that construct `Config(...)` with `output=`**

In `tests/test_audio_io_tone.py`'s `_config()` helper and `tests/test_ws_protocol.py`'s `_base_config()` helper, delete the `output="audio,text",` line from each `defaults = dict(...)` block. (`tests/test_ws_protocol.py` gets fully rewritten in Task 9 anyway, but fix it now so this task's own test run below is green before that rewrite lands.)

- [ ] **Step 4: Run the full client test suite to verify nothing references `output` anymore**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/ -v`
Expected: all PASS, no `TypeError: unexpected keyword argument 'output'`.

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
git add a2a_client/config.py config.example.yaml tests/test_audio_io_tone.py tests/test_ws_protocol.py
git commit -m "refactor: drop unused session.output config field (lugo has no output toggle)"
```

---

### Task 9: Rewrite `ws_protocol.py` for the lugo endpoint + wakeup message

**Files:**
- Modify: `a2a_client/ws_protocol.py` (full rewrite of its ~24 lines)
- Test: `tests/test_ws_protocol.py` (full rewrite)

**Interfaces:**
- Consumes: `Config` (unchanged shape after Task 8).
- Produces: `build_ws_url(config: Config) -> str` (now takes no `session_id` parameter — the URL has no query params at all), `build_wakeup_message(config: Config, session_id: str | None) -> dict`. Task 11 (`service.py` handshake) calls both.

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `tests/test_ws_protocol.py` with:

```python
from a2a_client.config import Config
from a2a_client.ws_protocol import build_ws_url, build_wakeup_message


def _base_config(**overrides) -> Config:
    defaults = dict(
        host="127.0.0.1", port=8000, secure=False, profile=None,
        input_sample_rate=16000, output_sample_rate=16000, uplink_sample_rate=16000,
        frame_ms=60, input_channels=1, output_channels=2, input_device=None,
        output_device=None, playback_preroll_ms=200, allow_barge_in=False,
        barge_in_rms_threshold=1200.0, barge_in_min_frames=5,
        reconnect_initial_seconds=1.0, reconnect_max_seconds=20.0, log_events=True,
        led_enabled=False, led_yellow_pin=13, led_red_pin=22, led_green_pin=17,
        input_alsa_device=None, output_alsa_device=None, oled_enabled=False,
        oled_i2c_port=1, oled_i2c_address=0x3C, oled_font_path="",
        session_state_path=None,
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_build_ws_url_has_no_query_params():
    url = build_ws_url(_base_config())
    assert url == "ws://127.0.0.1:8000/v1/lugo/stream"


def test_build_ws_url_uses_wss_when_secure():
    url = build_ws_url(_base_config(secure=True))
    assert url.startswith("wss://")


def test_build_wakeup_message_always_enables_mcp():
    msg = build_wakeup_message(_base_config(), session_id=None)
    assert msg["type"] == "wakeup"
    assert msg["features"] == {"mcp": True}


def test_build_wakeup_message_includes_audio_params():
    msg = build_wakeup_message(_base_config(uplink_sample_rate=16000, output_sample_rate=24000), session_id=None)
    assert msg["audio_params"] == {"sample_rate": 16000, "output_sample_rate": 24000}


def test_build_wakeup_message_omits_session_id_when_none():
    msg = build_wakeup_message(_base_config(), session_id=None)
    assert "session_id" not in msg


def test_build_wakeup_message_includes_session_id_when_provided():
    msg = build_wakeup_message(_base_config(), session_id="abc-123")
    assert msg["session_id"] == "abc-123"


def test_build_wakeup_message_omits_profile_when_not_set():
    msg = build_wakeup_message(_base_config(profile=None), session_id=None)
    assert "profile" not in msg


def test_build_wakeup_message_includes_profile_when_set():
    msg = build_wakeup_message(_base_config(profile="home"), session_id=None)
    assert msg["profile"] == "home"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/test_ws_protocol.py -v`
Expected: FAIL — `build_ws_url` still takes a `session_id` kwarg and returns a query-string URL; `build_wakeup_message` doesn't exist yet.

- [ ] **Step 3: Implement**

Replace all of `a2a_client/ws_protocol.py` with:

```python
from __future__ import annotations

from .config import Config


def build_ws_url(config: Config) -> str:
    scheme = "wss" if config.secure else "ws"
    return f"{scheme}://{config.host}:{config.port}/v1/lugo/stream"


def build_wakeup_message(config: Config, session_id: str | None) -> dict:
    message: dict = {
        "type": "wakeup",
        "audio_params": {
            "sample_rate": config.uplink_sample_rate,
            "output_sample_rate": config.output_sample_rate,
        },
        "features": {"mcp": True},
    }
    if config.profile:
        message["profile"] = config.profile
    if session_id:
        message["session_id"] = session_id
    return message
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/test_ws_protocol.py -v`
Expected: all 8 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
git add a2a_client/ws_protocol.py tests/test_ws_protocol.py
git commit -m "feat: point ws_protocol at /v1/lugo/stream and build the wakeup message"
```

---

### Task 10: `service.py` — handshake and binary-frame handling

**Files:**
- Modify: `a2a_client/service.py` (imports; `run_forever` connect block; `receiver`'s binary-message branch; `sender`'s encode branch; remove `_negotiated_audio_codec`/`_negotiated_audio_out`/`_play_audio_url`)

**Interfaces:**
- Consumes: `build_ws_url(config)`, `build_wakeup_message(config, session_id)` from Task 9; `LUGO_FRAME_OPUS`, `decode_frame` from Task 5.
- Produces: no new public interface — internal to `service.py`. Task 11 (JSON event mapping) and Task 12 (MCP wiring) build on this task's version of `receiver()`/`run_forever()`.

No automated test for this task: `service.py`'s WebSocket orchestration loop has never had direct unit tests in this repo (none of the 4 pre-existing test files touch it) — it's covered by the manual validation checklist in the design spec (`docs/superpowers/specs/2026-07-12-rpi-mcp-tools-design.md`, Part 4) once Tasks 10-13 are all in place and there's a full loop to exercise end-to-end. Verify this task with `python -m py_compile` (matching the repo's own `make check` convention) plus a manual read-through against the steps below.

- [ ] **Step 1: Update imports**

At the top of `a2a_client/service.py`, change:

```python
from .session_state import load_session_id, save_session_id
from .ws_protocol import build_ws_url
```

to:

```python
from .lugo_frame import LUGO_FRAME_OPUS, decode_frame
from .session_state import load_session_id, save_session_id
from .ws_protocol import build_ws_url, build_wakeup_message
```

- [ ] **Step 2: Remove the two negotiated-protocol attributes from `__init__`**

Delete these two lines from `__init__` (currently right after `self._session_ready = asyncio.Event()`):

```python
        self._negotiated_audio_codec = "opus"
        self._negotiated_audio_out = "opus"
```

(They're always Opus now — lugo has no other codec/transport option, confirmed at `apps/api_gateway/app/api/routes/lugo.py:105`: `audio_codec="opus", ..., audio_out="opus"` is hardcoded server-side.)

- [ ] **Step 3: Delete `_play_audio_url` and `_build_absolute_url`'s now-unused caller sites**

Delete the entire `_play_audio_url` method (currently lines 150-165: `async def _play_audio_url(self, maybe_relative_url: str) -> None: ...`). Keep `_build_absolute_url` — it's still used by `_warm_stt_engine` for the `/v1/stt/warm` REST call, which is independent of the WS protocol switch.

- [ ] **Step 4: Simplify `sender()`'s encode branch**

Replace:

```python
                try:
                    if self._negotiated_audio_codec == "opus":
                        pcm_frame = self.audio.resample_pcm16_mono(
                            pcm_frame,
                            source_rate=self.config.input_sample_rate,
                            target_rate=self.config.uplink_sample_rate,
                        )
                        packet = self.audio.encode_frame(pcm_frame)
                    else:
                        packet = self.audio.resample_pcm16_mono(
                            pcm_frame,
                            source_rate=self.config.input_sample_rate,
                            target_rate=self.config.uplink_sample_rate,
                        )
                    await ws.send(packet)
```

with:

```python
                try:
                    pcm_frame = self.audio.resample_pcm16_mono(
                        pcm_frame,
                        source_rate=self.config.input_sample_rate,
                        target_rate=self.config.uplink_sample_rate,
                    )
                    packet = self.audio.encode_frame(pcm_frame)
                    await ws.send(packet)
```

Also update the log line right below it, which still references `self._negotiated_audio_codec` — change `codec={self._negotiated_audio_codec}` to `codec=opus` (a literal, since it's the only option now).

- [ ] **Step 5: Rewrite the binary-message branch in `receiver()`**

Replace:

```python
            if isinstance(message, bytes):
                if message.startswith(b"RIFF"):
                    self.audio.set_speaking(True)
                    await asyncio.to_thread(self.audio.play_wav_bytes, message)
                    continue
                if self._negotiated_audio_out != "opus":
                    continue
                # Decode + queue the Opus frame; the output callback plays it from the
                # jitter buffer at a steady rate (seamless across network jitter).
                self.audio.set_speaking(True)
                self.audio.play_opus_frame(message)
                continue
```

with:

```python
            if isinstance(message, bytes):
                try:
                    frame_type, opus_payload = decode_frame(message)
                except ValueError as exc:
                    self.log(f"bad downlink frame: {exc}")
                    continue
                if frame_type != LUGO_FRAME_OPUS:
                    continue
                # Decode + queue the Opus frame; the output callback plays it from the
                # jitter buffer at a steady rate (seamless across network jitter).
                self.audio.set_speaking(True)
                self.audio.play_opus_frame(opus_payload)
                continue
```

- [ ] **Step 6: Send the `wakeup` message right after connecting, in `run_forever()`**

Replace:

```python
                ws_url = build_ws_url(self.config, session_id=self._session_id)
                try:
                    async with websockets.connect(ws_url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                        self.log(f"connected: {ws_url}")
                        backoff = self.config.reconnect_initial_seconds
```

with:

```python
                ws_url = build_ws_url(self.config)
                try:
                    async with websockets.connect(ws_url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                        self.log(f"connected: {ws_url}")
                        await ws.send(json.dumps(build_wakeup_message(self.config, self._session_id)))
                        backoff = self.config.reconnect_initial_seconds
```

- [ ] **Step 7: Compile-check**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m py_compile a2a_client/service.py`
Expected: no output, exit code 0.

- [ ] **Step 8: Run the full test suite to confirm no regressions in unrelated modules**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/ -v`
Expected: all PASS (this task doesn't add tests, but must not break Tasks 5-9's tests, none of which import `service.py`).

- [ ] **Step 9: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
git add a2a_client/service.py
git commit -m "feat: switch handshake and downlink framing to the lugo wire protocol"
```

---

### Task 11: `service.py` — JSON event mapping (welcome/goodbye/tts/stt/engines_ready)

**Files:**
- Modify: `a2a_client/service.py` (the `if/elif` chain in `receiver()` handling parsed JSON messages)

**Interfaces:**
- Consumes: the new lugo wire message shapes from gateway Tasks 1-4 (`welcome{session_id,stt_ready,tts_ready}`, `engines_ready`, `speech_start`, `speech_end{speech_ms}`, `processing{turn}`, `stt{text,final}`, `tts{state,text?,reason?}`, `goodbye{reason}`, `error{message}`).
- Produces: no new public interface. Task 12 adds one more branch (`mcp`) to this same chain.

No automated test — same rationale as Task 10 (this is the WS orchestration loop, covered by the manual validation checklist, not unit tests).

- [ ] **Step 1: Replace the entire event-dispatch `if/elif` chain**

In `receiver()`, replace everything from `name = event.get("event")` down through the final `elif self.config.log_events:` fallback (i.e., replace lines 271-385 of the pre-change file) with:

```python
            name = event.get("type")
            if name == "speech_start":
                self._cancel_idle_reset()
                self._status("listening")
                self._schedule_ready_reset()
            elif name == "speech_end":
                self._cancel_idle_reset()
                self._status("processing")
                self._schedule_ready_reset()
            elif name == "processing":
                self._cancel_idle_reset()
                self._status("processing")
                self._schedule_ready_reset()
            elif name == "stt":
                text = (event.get("text") or "").strip()
                if text:
                    self.log(f"you: {text}")
                    self._cancel_idle_reset()
                    self._status("listening")
                    self._schedule_ready_reset()
                else:
                    self._set_ready()
            elif name == "tts":
                state = event.get("state")
                if state == "start":
                    self._cancel_idle_reset()
                    self.audio.set_speaking(True)
                    self._status("speaking")
                    self._schedule_ready_reset(delay=3.0)
                elif state == "sentence_start":
                    text = (event.get("text") or "").strip()
                    if text:
                        self.log(f"assistant: {text}")
                elif state == "stop":
                    self._cancel_idle_reset()
                    if event.get("reason"):
                        self.audio.reset_playback()  # interrupt: drop queued audio immediately
                    else:
                        self.audio.set_speaking(False)  # buffer drains naturally
                        if self.config.log_events:
                            self.log(f"playback underrun total: {self.audio.play_buffer.underrun_samples} samples")
                    self._set_ready()
            elif name == "welcome":
                self._cancel_idle_reset()
                new_session_id = event.get("session_id")
                if new_session_id and new_session_id != self._session_id:
                    self._session_id = str(new_session_id)
                    try:
                        save_session_id(self.config.session_state_path, self._session_id)
                    except Exception as exc:  # noqa: BLE001 - persistence must not break the session
                        self.log(f"session_id persist failed: {exc}")
                out_sr = int((event.get("audio_params") or {}).get("sample_rate") or self.config.output_sample_rate)
                self.audio.set_negotiated_sample_rate(out_sr)
                self._session_ready.set()
                self.log(f"session started: session_id={self._session_id} output_sample_rate={out_sr}")
                # Missing keys (older server) default to ready, so behavior is
                # unchanged against a server that doesn't send them yet.
                self._engines_ready = event.get("stt_ready", True) and event.get("tts_ready", True)
                if self._engines_ready:
                    self._set_ready()
                else:
                    self.log("engines still warming up server-side — please wait before speaking")
                    self.leds.warming()
                    self.oled.warming()
                    await asyncio.to_thread(self.audio.play_tone, _WARMING_TONE_HZ)
                    self._start_warming_reminder()
            elif name == "engines_ready":
                self._cancel_idle_reset()
                self._stop_warming_reminder()
                self._engines_ready = True
                self.log("engines ready")
                await asyncio.to_thread(self.audio.play_tone, _READY_TONE_HZ)
                self._set_ready()
            elif name == "goodbye":
                self.log(f"server goodbye: {event.get('reason', '')}")
            elif name == "error":
                self._cancel_idle_reset()
                self.log(f"server error: {event.get('message', 'unknown')}")
                self.leds.error()
                self.oled.error("SERVER ERR")
            elif self.config.log_events:
                self.log(f"event: {name} payload={event}")
```

Notes for the implementer: `_status`, `_set_ready`, `_schedule_ready_reset`, `_cancel_idle_reset`, `_start_warming_reminder`, `_stop_warming_reminder`, `_WARMING_TONE_HZ`, `_READY_TONE_HZ` are all pre-existing and unchanged — this task only changes what triggers them, not their implementations. The `command` event type (forwarded by the gateway's existing `elif event == "command":` branch, unrelated to MCP) has no case here and falls through to the generic `elif self.config.log_events:` logger — that's intentional, it's out of scope for this pass (see the design spec's Part 2, "command" note).

- [ ] **Step 2: Compile-check**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m py_compile a2a_client/service.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Run the full test suite to confirm no regressions**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
git add a2a_client/service.py
git commit -m "feat: map lugo wire events (welcome/tts/stt/goodbye/engines_ready) to LED/OLED state"
```

---

### Task 12: `service.py` — MCP wiring, idle-wake, and screen-text overlay

**Files:**
- Modify: `a2a_client/service.py` (imports; `__init__`; new `_mcp_context`/`_go_idle`/`_show_text_overlay` methods; one new branch in `receiver()`'s event chain; one new gate at the top of `sender()`'s inner loop)

**Interfaces:**
- Consumes: `McpToolContext`, `handle_mcp_request` from Task 6; `AudioIO.get_volume_pct`/`set_volume_pct` from Task 7.
- Produces: no new public interface — this is the task that makes the MCP tools from Task 6 actually reachable over the wire.

No automated test — same rationale as Tasks 10-11.

- [ ] **Step 1: Add the import**

At the top of `a2a_client/service.py`, add:

```python
from .mcp_tools import McpToolContext, handle_mcp_request
```

- [ ] **Step 2: Add idle-tracking state to `__init__`**

Right after `self._engines_ready = False` (the last line of `__init__`), add:

```python
        self._start_time = time.monotonic()
        self._idle = False
        self._idle_wake_frames = 0
```

- [ ] **Step 3: Add the three new helper methods**

Add these methods to `AudioToAudioService`, near `_build_absolute_url` (any location in the class body is fine — they don't depend on method order):

```python
    def _mcp_context(self) -> McpToolContext:
        return McpToolContext(
            get_volume_pct=self.audio.get_volume_pct,
            set_volume_pct=self.audio.set_volume_pct,
            uptime_seconds=lambda: time.monotonic() - self._start_time,
            go_idle=self._go_idle,
            show_text=self._show_text_overlay,
        )

    def _go_idle(self) -> None:
        self._idle = True
        self._idle_wake_frames = 0
        self._cancel_idle_reset()
        self.leds.stopped()
        self.oled.show("idle", "say something")

    def _show_text_overlay(self, line1: str, line2: str) -> None:
        self._cancel_idle_reset()
        self.oled.show(line1, line2)
        self._schedule_ready_reset(delay=5.0)
```

- [ ] **Step 4: Handle incoming `mcp` messages in `receiver()`**

In the event-dispatch chain added in Task 11, add a new branch (placement doesn't matter relative to the others; put it right before the `elif name == "goodbye":` branch):

```python
            elif name == "mcp":
                payload = event.get("payload") or {}
                response = handle_mcp_request(payload, self._mcp_context())
                await ws.send(json.dumps({"type": "mcp", "payload": response}))
```

- [ ] **Step 5: Add the idle-wake gate to `sender()`**

In `sender()`'s inner `while len(buffer) >= self.audio.in_frame_bytes:` loop, right after the frame is sliced off the buffer (`del buffer[: self.audio.in_frame_bytes]`) and before the existing `if self.audio.is_playing():` half-duplex check, insert:

```python
                # Idle gate: while self.device.idle is active, drop uplink frames until a
                # loud sound (same RMS detector used for barge-in) wakes the device — there
                # is no physical wake button on this client, so voice is the only trigger.
                if self._idle:
                    samples = np.frombuffer(pcm_frame, dtype=np.int16)
                    rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) if len(samples) else 0.0
                    if rms >= self.config.barge_in_rms_threshold:
                        self._idle_wake_frames += 1
                    else:
                        self._idle_wake_frames = 0
                    if self._idle_wake_frames < self.config.barge_in_min_frames:
                        continue
                    self._idle_wake_frames = 0
                    self._idle = False
                    self.log("idle: woke on loud sound")
                    self._set_ready()
```

(This reuses `barge_in_rms_threshold`/`barge_in_min_frames` for a different purpose than barge-in — waking from idle — and works regardless of whether `allow_barge_in` is enabled, since the two features are independent uses of the same tunables.)

- [ ] **Step 6: Compile-check**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m py_compile a2a_client/service.py`
Expected: no output, exit code 0.

- [ ] **Step 7: Run the full test suite to confirm no regressions**

Run: `cd /Users/lugon/code/speech-text-transformer/agent-assistant && ../.venv/bin/python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
git add a2a_client/service.py
git commit -m "feat: wire MCP dispatcher, idle-wake, and screen-text overlay into the service loop"
```

---

### Task 13: Manual end-to-end validation

**Files:** none (verification task, no code changes)

**Interfaces:** none.

- [ ] **Step 1: Start a staging gateway with the Task 1-4 changes deployed**

Confirm `settings.device_mcp_enabled` is `True` (it defaults to `True` in `apps/api_gateway/app/core/settings.py:242`).

- [ ] **Step 2: Run the RPi client against it**

```bash
cd /Users/lugon/code/speech-text-transformer/agent-assistant
../.venv/bin/python -m a2a_client.runner --config config.yaml
```

Confirm the log shows `connected: ws://.../v1/lugo/stream` followed by `session started: session_id=...` (not a connection error).

- [ ] **Step 3: Verify each item from the design spec's manual checklist** (`docs/superpowers/specs/2026-07-12-rpi-mcp-tools-design.md`, Part 4)

- Wakeup→welcome handshake succeeds (checked in Step 2).
- Ask the assistant (voice) to change the volume; confirm playback volume audibly changes and `self.get_device_status` (ask "what's your status") reports the new value.
- Ask the assistant to go idle; confirm the mic stops producing transcripts, then make a loud sound near the mic and confirm it wakes (log line `idle: woke on loud sound`) and resumes normal operation.
- Ask the assistant to show a message on the screen; confirm the OLED updates and reverts to the normal status text after ~5 seconds.
- Restart the client process (`Ctrl-C` then rerun) and confirm the log shows `resuming session: <same id>` and the assistant retains prior conversation context on the next turn.

- [ ] **Step 4: No commit** — this task only produces a manual sign-off; if any checklist item fails, open a follow-up task against the specific failing behavior rather than committing anything here.

---

## Self-Review Notes

- **Spec coverage:** Part 1 (gateway) → Tasks 1-4. Part 2 (client architecture: lugo_frame, ws_protocol, service.py handshake/binary/event-mapping, config cleanup) → Tasks 5, 8, 9, 10, 11. Part 3 (MCP tool set) → Tasks 6, 12. Part 4 (testing & rollout) → Tasks 1-12's own test steps plus Task 13's manual checklist; rollout order is stated in Global Constraints and Task 4's closing note ("Gateway work is now deployable").
- **Placeholder scan:** no TBD/TODO; every step shows the actual code or exact command, not a description of one.
- **Type/name consistency checked:** `McpToolContext` field names (`get_volume_pct`, `set_volume_pct`, `uptime_seconds`, `go_idle`, `show_text`) match between Task 6's definition and Task 12's `_mcp_context()` construction. `build_wakeup_message`/`build_ws_url` signatures match between Task 9's definition and Task 10's call sites. `LUGO_FRAME_OPUS`/`decode_frame` names match between Task 5's definition and Task 10's import/use. `AudioIO.get_volume_pct`/`set_volume_pct` names match between Task 7's definition and Task 12's `_mcp_context()`.
