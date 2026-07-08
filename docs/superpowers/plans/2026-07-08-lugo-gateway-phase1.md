# Lugo Gateway Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a unified `Lugo` device voice protocol to the gateway (`WS /v1/lugo/stream`) on top of a shared `ConversationSession` core extracted from `conversation.py`, keeping the legacy `/v1/conversation/stream` working as a thin shim.

**Architecture:** Extract all protocol-neutral turn logic from the 694-line `conversation.py` route into a reusable `ConversationSession` class that talks to the outside world through two callbacks (`emit(event, **payload)` for JSON, `emit_audio(packet)` for binary opus). The old route becomes a thin adapter that renders neutral events as the current `{"event": ...}` wire (regression-guarded by the existing suite). A new `lugo.py` route renders them as Lugo `{"type": ...}` messages + v3 binary framing, adds the wakeup/welcome handshake, barge-in, and profile-configured idle disconnect.

**Tech Stack:** Python 3.12, FastAPI (`WebSocket`), Pydantic v2, pytest + `fastapi.testclient.TestClient` (`websocket_connect`), existing services (`VadEndpointer`, `stt_service`, `tts_service`, responder, MCP, memory, `app.core.opus`).

## Global Constraints

- Protocol name is **Lugo**. Never emit the string "xiaozhi" in code, comments, wire, or docs.
- Endpoint: `WS /v1/lugo/stream`. Legacy `WS /v1/conversation/stream` stays functional this phase.
- JSON control on WebSocket **text** frames; opus audio on **binary** frames wrapped in the v3 header `{uint8 type; uint8 reserved; uint16 payload_size; payload}` (`type`: 0=OPUS, 1=JSON reserved).
- Handshake messages: client `wakeup`, server `welcome`. Other server types: `stt`, `tts`, `mcp`, `error`, `goodbye`. Other client types: `listen`, `abort`, `text`.
- `welcome` includes `idle_timeout_s` resolved from the profile (`Profile.session.idle_timeout_s`, default 30, `0` = never).
- Turn segmentation is **server-side VAD (auto mode)** this phase. `listen` is parsed but only `mode:"auto"` behavior is implemented; wake-word (`detect`) is Phase 2.
- Barge-in (`abort`) cancels the current turn and stops audio **without closing** the WebSocket. Only idle timeout / disconnect closes it.
- The existing test suite must stay green after every task (regression gate for the extraction).
- Python 3.12 venv (`.venv`). Run tests with `pytest` from repo root.

---

### Task 1: Add `SessionConfig` to the profile model

**Files:**
- Modify: `apps/api_gateway/app/services/profiles/models.py`
- Modify: `apps/api_gateway/app/api/routes/profiles.py:18-25` (ProfileRequest)
- Test: `tests/unit/test_profile_session_config.py`

**Interfaces:**
- Produces: `SessionConfig(idle_timeout_s: int = 30)` and `Profile.session: SessionConfig`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_profile_session_config.py
from app.services.profiles.models import Profile, SessionConfig


def test_profile_has_default_session_timeout():
    p = Profile(name="d")
    assert isinstance(p.session, SessionConfig)
    assert p.session.idle_timeout_s == 30


def test_profile_session_timeout_override():
    p = Profile(name="d", session=SessionConfig(idle_timeout_s=10))
    assert p.session.idle_timeout_s == 10


def test_profile_loads_without_session_field():
    # Legacy profiles.json entries omit `session` -> must default, not error.
    p = Profile.model_validate({"name": "esp32-assistant"})
    assert p.session.idle_timeout_s == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_profile_session_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'SessionConfig'`

- [ ] **Step 3: Add the model**

In `apps/api_gateway/app/services/profiles/models.py`, add before `class Profile`:

```python
class SessionConfig(BaseModel):
    idle_timeout_s: int = 30    # seconds of inactivity before the server disconnects; 0 = never
```

Add the field to `Profile`:

```python
class Profile(BaseModel):
    name: str
    nickname: str = ""
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()
    session: SessionConfig = SessionConfig()
```

- [ ] **Step 4: Expose it on the CRUD request**

In `apps/api_gateway/app/api/routes/profiles.py`, import `SessionConfig` alongside the other profile models and add to `ProfileRequest`:

```python
    session: SessionConfig = SessionConfig()
```

(Find the existing `from app.services.profiles.models import ...` line and add `SessionConfig`; add the `session` field next to `memory` in `ProfileRequest`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_profile_session_config.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/profiles/models.py apps/api_gateway/app/api/routes/profiles.py tests/unit/test_profile_session_config.py
git commit -m "feat(profiles): add SessionConfig.idle_timeout_s to profile model"
```

---

### Task 2: v3 binary frame codec

**Files:**
- Create: `apps/api_gateway/app/services/conversation/lugo_frame.py`
- Test: `tests/unit/test_lugo_frame.py`

**Interfaces:**
- Produces:
  - `LUGO_FRAME_OPUS = 0`, `LUGO_FRAME_JSON = 1`
  - `encode_frame(frame_type: int, payload: bytes) -> bytes`
  - `decode_frame(data: bytes) -> tuple[int, bytes]` — raises `ValueError` on a short/oversized frame.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lugo_frame.py
import pytest
from app.services.conversation.lugo_frame import (
    LUGO_FRAME_OPUS, encode_frame, decode_frame,
)


def test_roundtrip_opus():
    payload = b"\x01\x02\x03\x04"
    frame = encode_frame(LUGO_FRAME_OPUS, payload)
    assert len(frame) == 4 + len(payload)
    ftype, out = decode_frame(frame)
    assert ftype == LUGO_FRAME_OPUS
    assert out == payload


def test_header_layout():
    frame = encode_frame(LUGO_FRAME_OPUS, b"ab")
    assert frame[0] == 0          # type
    assert frame[1] == 0          # reserved
    assert frame[2] == 0 and frame[3] == 2  # payload_size big-endian uint16 == 2


def test_empty_payload_ok():
    ftype, out = decode_frame(encode_frame(LUGO_FRAME_OPUS, b""))
    assert out == b""


def test_decode_rejects_short_header():
    with pytest.raises(ValueError):
        decode_frame(b"\x00\x00")


def test_decode_rejects_size_mismatch():
    # header says 5 bytes but only 2 provided
    with pytest.raises(ValueError):
        decode_frame(b"\x00\x00\x00\x05ab")


def test_encode_rejects_oversized_payload():
    with pytest.raises(ValueError):
        encode_frame(LUGO_FRAME_OPUS, b"x" * 70000)  # > uint16 max
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_lugo_frame.py -v`
Expected: FAIL with `ModuleNotFoundError: ... lugo_frame`

- [ ] **Step 3: Implement the codec**

```python
# apps/api_gateway/app/services/conversation/lugo_frame.py
"""Lugo v3 binary framing: 4-byte header + payload.

struct { uint8 type; uint8 reserved; uint16 payload_size (big-endian); uint8 payload[]; }
"""
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

Run: `.venv/bin/pytest tests/unit/test_lugo_frame.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/conversation/lugo_frame.py tests/unit/test_lugo_frame.py
git commit -m "feat(lugo): add v3 binary frame codec"
```

---

### Task 3: Extract `ConversationSession` core from `conversation.py`

This is a refactor guarded by the existing suite. The neutral core owns everything
between "connection accepted" and "connection closed" **except** wire encoding.

**Files:**
- Create: `apps/api_gateway/app/services/conversation/session.py`
- Modify: `apps/api_gateway/app/api/routes/conversation.py` (becomes the first consumer)
- Test: `tests/unit/test_conversation_session_core.py`

**Interfaces:**
- Produces (the contract both front-ends depend on):

```python
# apps/api_gateway/app/services/conversation/session.py
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

EmitFn = Callable[..., Awaitable[None]]        # emit(event: str, **payload)
EmitAudioFn = Callable[[bytes], Awaitable[None]]  # emit_audio(opus_packet: bytes)


@dataclass
class SessionRuntimeConfig:
    session_id: str
    profile_name: str | None
    # resolved engines / params (already computed by the front-end)
    stt_engine: str
    language: str | None
    tts_engine: str
    voice: str | None
    ref_audio_path: str | None
    ref_text: str | None
    tts_instruct: str | None
    tts_speed: float | None
    tts_language: str | None
    sample_rate: int
    output_sample_rate: int
    audio_codec: str            # "pcm16" | "opus" (input)
    want_audio: bool
    want_text: bool
    audio_out: str              # "url" | "opus"
    denoise: bool
    resume_sid: str | None      # requested_sid, for history resume


class ConversationSession:
    def __init__(self, cfg: SessionRuntimeConfig, emit: EmitFn, emit_audio: EmitAudioFn): ...

    # Emits neutral events via self.emit / opus packets via self.emit_audio.
    # Neutral events (name -> payload keys) — identical payloads to today's route:
    #   session_started(session_id, profile, active_tools, stt_engine, stt_detail,
    #                   tts_engine, tts_detail, responder, llm_model, sample_rate,
    #                   audio_codec, output, audio_out, output_sample_rate,
    #                   stt_ready, tts_ready)
    #   engines_ready()  warning(message)  error(message)
    #   speech_start()   speech_end(speech_ms)   processing(turn)
    #   user_transcript(turn, text, engine)
    #   response_text(turn, chunk_index, text, responder)
    #   audio_start(turn, chunk_index, text, codec, sample_rate, frames)
    #   audio_end(turn, chunk_index)
    #   audio_chunk(turn, chunk_index, text, audio_url, sample_rate)   # url mode
    #   turn_done(turn, **extra)   aborted(reason)   command(**cmd_payload)
    #   reset()   done()
    async def start(self) -> None:            # profile/engine/provider setup + session_started + bg warm
        ...
    async def feed_audio(self, frame: bytes) -> None:   # opus-decode if needed -> endpointer -> maybe start turn
        ...
    async def feed_text(self, text: str) -> None:       # supersede current turn, run text turn
        ...
    async def abort(self, reason: str) -> None:         # cancel current turn (barge-in); keep session
        ...
    async def flush(self) -> None:                      # endpointer.flush -> run turn if audio buffered
        ...
    async def reset(self) -> None:                      # clear history + endpointer; emit reset
        ...
    async def close(self) -> None:                      # cancel turn, mark_ended, memory extraction
        ...
    @property
    def output_sample_rate_effective(self) -> int | None: ...  # for session_started payload
```

- Consumes: `SessionConfig` (Task 1) is used by front-ends when building `welcome`, not by the core.

**Extraction rules (apply verbatim):**
1. Move helpers `_build_tool_registry`, `_spawn_background`, and the closures `send`, `persist`, `refresh_memory`, `abort_turn`, `handle_turn`, `_run_turn`, `_stream_to_tts` into `ConversationSession` as methods/attributes. `conversation.py:52-73` and `76-79` move to the module or stay importable — keep `_build_tool_registry` as a module function in `session.py`.
2. Replace every `await send(name, **p)` with `await self.emit(name, **p)`.
3. Replace the direct `await websocket.send_bytes(pkt)` inside `_stream_to_tts` (`conversation.py:522`) with `await self.emit_audio(pkt)`. **Pacing stays in the core** (unchanged `pacing_delays` logic).
4. All the resolution logic in `conversation.py:210-354` that computes engines/params **stays in the front-end** and is passed in via `SessionRuntimeConfig`; the core receives resolved values. Opus decoder/encoder setup (`conversation.py:284-305`) moves **into** the core (`start()`), driven by `cfg.audio_codec` / `cfg.audio_out` — the core owns opus because it decodes input frames and encodes output packets.
5. `endpointer`, `history`, `turn`, `responder`, `tool_registry`, `tool_ctx`, `stt_provider`, `tts_provider`, `session_ready`, `base_system_prompt` become instance attributes set in `start()`.
6. `close()` contains the `finally` block logic (`conversation.py:681-694`) **except** `websocket.close()` (the front-end owns the socket).

- [ ] **Step 1: Write the failing core test**

```python
# tests/unit/test_conversation_session_core.py
import pytest
from app.core.settings import settings
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    name = "stub-core-stt"
    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-core-tts"
    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(engine=self.name, sample_rate=24000,
                         audio_url="/artifacts/x.wav", duration_seconds=0.1, text=payload.text)


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    stt_service.providers["stub-core-stt"] = _StubSTT()
    tts_service.providers["stub-core-tts"] = _StubTTS()
    yield
    stt_service.providers.pop("stub-core-stt", None)
    tts_service.providers.pop("stub-core-tts", None)


def _cfg(**over):
    base = dict(
        session_id="s1", profile_name=None, stt_engine="stub-core-stt", language="vi",
        tts_engine="stub-core-tts", voice=None, ref_audio_path=None, ref_text=None,
        tts_instruct=None, tts_speed=None, tts_language=None, sample_rate=SR,
        output_sample_rate=24000, audio_codec="pcm16", want_audio=False, want_text=True,
        audio_out="url", denoise=False, resume_sid=None,
    )
    base.update(over)
    return SessionRuntimeConfig(**base)


@pytest.mark.asyncio
async def test_text_turn_emits_transcript_and_reply():
    events = []
    async def emit(name, **p): events.append((name, p))
    async def emit_audio(pkt): events.append(("_audio", {"len": len(pkt)}))

    sess = ConversationSession(_cfg(), emit, emit_audio)
    await sess.start()
    await sess.feed_text("hello")
    await sess.close()

    names = [n for n, _ in events]
    assert "session_started" in names
    assert "user_transcript" in names
    assert "turn_done" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_conversation_session_core.py -v`
Expected: FAIL with `ModuleNotFoundError: ... conversation.session`

- [ ] **Step 3: Create `session.py` by moving the logic**

Create `apps/api_gateway/app/services/conversation/session.py` implementing the interface above, moving the closures from `conversation.py` per the Extraction rules. Keep all existing imports the closures use (move the needed imports from `conversation.py:1-42` into `session.py`). The `start()` method runs the setup currently at `conversation.py:284-398` that is post-resolution (opus enc/dec, responder build, tool registry, detail strings, endpointer, history resume, `session_started` emit, background warm). `feed_audio` holds the binary branch of the receive loop (`conversation.py:630-650`); `feed_text`/`abort`/`flush`/`reset` hold the text-control branches (`conversation.py:652-677`).

- [ ] **Step 4: Run the core test**

Run: `.venv/bin/pytest tests/unit/test_conversation_session_core.py -v`
Expected: PASS

- [ ] **Step 5: Rewrite `conversation.py` route to drive the core**

Replace the body of `conversation_stream` (from `conversation.py:284` onward) so that after the existing resolution block (`210-283`) it builds a `SessionRuntimeConfig`, defines the wire adapters, and delegates:

```python
    cfg = SessionRuntimeConfig(
        session_id=session_id, profile_name=profile_name, stt_engine=stt_engine,
        language=language, tts_engine=tts_engine, voice=voice,
        ref_audio_path=ref_audio_path, ref_text=ref_text, tts_instruct=tts_instruct,
        tts_speed=tts_speed, tts_language=tts_language, sample_rate=sample_rate,
        output_sample_rate=output_sample_rate, audio_codec=audio_codec,
        want_audio=want_audio, want_text=want_text, audio_out=audio_out,
        denoise=denoise, resume_sid=requested_sid,
    )

    async def emit(event: str, **payload) -> None:
        await websocket.send_json({"event": event, **payload})

    async def emit_audio(packet: bytes) -> None:
        await websocket.send_bytes(packet)

    session = ConversationSession(cfg, emit, emit_audio)
    try:
        await session.start()
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await session.feed_audio(message["bytes"])
            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "text":
                    await session.feed_text(control.get("text") or "")
                elif ctype == "abort":
                    await session.abort("user")
                elif ctype == "reset":
                    await session.reset()
                elif ctype in {"flush", "end"}:
                    await session.flush()
                    if ctype == "end":
                        await emit("done")
                        break
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
```

Keep the pre-resolution query-param block (`210-283`) and the HTTP routes (`/llm`, `/chat`) exactly as they are. Delete the now-moved closures from the route.

- [ ] **Step 6: Run the full suite (regression gate)**

Run: `.venv/bin/pytest -q`
Expected: same pass count as before this task (no failures). Pay attention to `tests/unit/test_conversation_*.py` and history/memory/tts-profile tests — they exercise the moved logic through the unchanged `{"event": ...}` wire.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/conversation/session.py apps/api_gateway/app/api/routes/conversation.py tests/unit/test_conversation_session_core.py
git commit -m "refactor(conversation): extract ConversationSession core behind emit callbacks"
```

---

### Task 4: Lugo route — wakeup/welcome handshake + one audio turn

**Files:**
- Create: `apps/api_gateway/app/api/routes/lugo.py`
- Modify: `apps/api_gateway/app/main.py:115-119` (register router)
- Test: `tests/unit/test_lugo_stream.py`

**Interfaces:**
- Consumes: `ConversationSession`, `SessionRuntimeConfig` (Task 3); `encode_frame`, `LUGO_FRAME_OPUS` (Task 2); `Profile.session` (Task 1); `profile_store`, `stt_service`, `tts_service`, settings.
- Produces: `router` (APIRouter, prefix `/v1/lugo`) with `WS /stream`.

**Wire translation (neutral event -> Lugo):**
`session_started` is dropped (its data folds into `welcome`, already sent). `user_transcript` -> `{"type":"stt","text":..,"final":true}`. `response_text`/`audio_start`/`audio_end` -> `{"type":"tts","state":"sentence_start"|"start"|"stop","text":?}`. `command` -> `{"type":"mcp",...}`. `error` -> `{"type":"error","message":..}`. Opus packets -> `emit_audio` wraps in `encode_frame(LUGO_FRAME_OPUS, pkt)`. `audio_chunk` (url mode) is not used — Lugo forces `audio_out="opus"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lugo_stream.py
import json
import pytest
from fastapi.testclient import TestClient
from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-lugo-stt"
    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-lugo-tts"
    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(engine=self.name, sample_rate=24000,
                         audio_url="/artifacts/x.wav", duration_seconds=0.1, text=payload.text)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-lugo-stt")
    monkeypatch.setattr(settings, "conversation_tts_engine", "stub-lugo-tts")
    stt_service.providers["stub-lugo-stt"] = _StubSTT()
    tts_service.providers["stub-lugo-tts"] = _StubTTS()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-lugo-stt", None)
    tts_service.providers.pop("stub-lugo-tts", None)


def test_wakeup_gets_welcome_with_idle_timeout():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "version": 1, "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert "session_id" in msg
        assert msg["idle_timeout_s"] == 0


def test_text_turn_yields_stt_and_tts():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "text", "text": "hi"})
        types = []
        for _ in range(6):
            m = ws.receive_json()
            types.append((m["type"], m.get("state")))
            if m["type"] == "tts" and m.get("state") == "stop":
                break
        assert ("stt", None) in types
        assert any(t == "tts" for t, _ in types)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_lugo_stream.py -v`
Expected: FAIL (404 / no route)

- [ ] **Step 3: Implement `lugo.py`**

```python
# apps/api_gateway/app/api/routes/lugo.py
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.settings import settings
from app.services.conversation.lugo_frame import LUGO_FRAME_OPUS, encode_frame
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.profiles.store import profile_store
from app.services.stt.profile import resolve_stt_profile
from app.services.tts.profile_store import tts_profile_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/lugo", tags=["lugo"])

# neutral event -> lugo tts state
_TTS_STATE = {"audio_start": "start", "audio_end": "stop", "response_text": "sentence_start"}


def _resolve(profile_name: str | None):
    """Resolve engines/tts params from a profile (server owns everything)."""
    profile = profile_store.get(profile_name) if profile_name else None
    llm_base_url = (profile.llm.base_url or None) if (profile and profile.llm.base_url) else None
    llm_api_key = profile.llm.api_key if (profile and profile.llm.base_url) else None
    llm_model = (profile.llm.model or None) if (profile and profile.llm.model) else None
    system_prompt = (profile.system_prompt or None) if (profile and profile.system_prompt) else None
    stt_engine = settings.conversation_stt_engine or settings.default_stt_engine
    language = settings.conversation_language or None
    tts_name = (profile.tts.profile_name if profile else "") or None
    tts_profile = tts_profile_store.get(tts_name) if tts_name else None
    if tts_profile and tts_profile.engine:
        tts = dict(engine=tts_profile.engine, voice=tts_profile.voice or None,
                   ref_audio_path=tts_profile.ref_audio_path or None, ref_text=tts_profile.ref_text or None,
                   instruct=tts_profile.instruct or None, speed=tts_profile.speed, language=tts_profile.language)
    else:
        tts = dict(engine=settings.conversation_tts_engine or settings.default_tts_engine,
                   voice=None, ref_audio_path=None, ref_text=None, instruct=None, speed=None, language=None)
    idle = profile.session.idle_timeout_s if profile else 30
    return profile, stt_engine, language, tts, idle


@router.websocket("/stream")
async def lugo_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    # Handshake: first frame must be a `wakeup`.
    try:
        first = await websocket.receive_text()
        hello = json.loads(first)
    except (WebSocketDisconnect, json.JSONDecodeError):
        await websocket.close()
        return
    if hello.get("type") != "wakeup":
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return

    profile_name = hello.get("profile")
    profile, stt_engine, language, tts, idle = _resolve(profile_name)
    if profile_name and not profile:
        await websocket.send_json({"type": "error", "message": f"profile '{profile_name}' not found"})
        await websocket.close()
        return

    session_id = str(uuid.uuid4())
    in_sr = int((hello.get("audio_params") or {}).get("sample_rate", settings.stt_stream_sample_rate))
    out_sr = 24000
    cfg = SessionRuntimeConfig(
        session_id=session_id, profile_name=profile_name, stt_engine=stt_engine, language=language,
        tts_engine=tts["engine"], voice=tts["voice"], ref_audio_path=tts["ref_audio_path"],
        ref_text=tts["ref_text"], tts_instruct=tts["instruct"], tts_speed=tts["speed"],
        tts_language=tts["language"], sample_rate=in_sr, output_sample_rate=out_sr,
        audio_codec="opus", want_audio=True, want_text=True, audio_out="opus",
        denoise=False, resume_sid=None,
    )

    async def emit(event: str, **payload) -> None:
        if event == "user_transcript":
            await websocket.send_json({"type": "stt", "text": payload.get("text", ""), "final": True})
        elif event in _TTS_STATE:
            msg = {"type": "tts", "state": _TTS_STATE[event]}
            if payload.get("text"):
                msg["text"] = payload["text"]
            await websocket.send_json(msg)
        elif event == "command":
            await websocket.send_json({"type": "mcp", **payload})
        elif event == "error":
            await websocket.send_json({"type": "error", "message": payload.get("message", "")})
        # session_started / processing / turn_done / audio_chunk / engines_ready: not on the wire

    async def emit_audio(packet: bytes) -> None:
        await websocket.send_bytes(encode_frame(LUGO_FRAME_OPUS, packet))

    session = ConversationSession(cfg, emit, emit_audio)
    await session.start()
    await websocket.send_json({
        "type": "welcome", "session_id": session_id, "transport": "websocket",
        "audio_params": {"sample_rate": out_sr}, "idle_timeout_s": idle,
    })

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                # Phase 1: device sends raw opus frames (v3 wrapping optional on uplink).
                await session.feed_audio(message["bytes"])
            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "text":
                    await session.feed_text(control.get("text") or "")
                elif ctype == "abort":
                    await session.abort("barge-in")
                elif ctype == "listen":
                    pass  # Phase 1 auto mode: server VAD drives turns
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
```

- [ ] **Step 4: Register the router**

In `apps/api_gateway/app/main.py`, add the import near the other route imports (~line 15) and include it with the others (~line 119):

```python
from app.api.routes.lugo import router as lugo_router
...
app.include_router(lugo_router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_lugo_stream.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/api/routes/lugo.py apps/api_gateway/app/main.py tests/unit/test_lugo_stream.py
git commit -m "feat(lugo): add /v1/lugo/stream route with wakeup/welcome + turn"
```

---

### Task 5: Barge-in abort keeps the connection open

**Files:**
- Test: `tests/unit/test_lugo_barge_in.py`
- (No new code if Task 4's `abort` wiring is correct; this task proves it and fixes if not.)

**Interfaces:**
- Consumes: `/v1/lugo/stream`, `ConversationSession.abort` (Task 3/4).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lugo_barge_in.py
import pytest
from fastapi.testclient import TestClient
from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


class _StubSTT(STTProvider):
    name = "stub-bi-stt"
    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-bi-tts"
    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(engine=self.name, sample_rate=24000,
                         audio_url="/artifacts/x.wav", duration_seconds=0.1, text=payload.text)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-bi-stt")
    monkeypatch.setattr(settings, "conversation_tts_engine", "stub-bi-tts")
    stt_service.providers["stub-bi-stt"] = _StubSTT()
    tts_service.providers["stub-bi-tts"] = _StubTTS()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="dev", session=SessionConfig(idle_timeout_s=0)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    yield
    stt_service.providers.pop("stub-bi-stt", None)
    tts_service.providers.pop("stub-bi-tts", None)


def test_abort_then_still_usable():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "dev",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        # abort with no active turn is a safe no-op; connection stays open
        ws.send_json({"type": "abort", "reason": "wake_word_detected"})
        # a subsequent text turn still works -> connection was not closed
        ws.send_json({"type": "text", "text": "hi"})
        seen_stt = False
        for _ in range(6):
            m = ws.receive_json()
            if m["type"] == "stt":
                seen_stt = True
                break
        assert seen_stt
```

- [ ] **Step 2: Run test**

Run: `.venv/bin/pytest tests/unit/test_lugo_barge_in.py -v`
Expected: PASS (if Task 4 wired `abort` correctly). If it fails because `abort` closed the socket or raised, fix `ConversationSession.abort` to be a safe no-op when `current_turn` is None and never touch the socket.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_lugo_barge_in.py
git commit -m "test(lugo): barge-in abort keeps the connection open"
```

---

### Task 6: Idle timeout sends `goodbye` and closes

**Files:**
- Modify: `apps/api_gateway/app/api/routes/lugo.py` (add idle timer)
- Test: `tests/unit/test_lugo_idle_timeout.py`

**Interfaces:**
- Consumes: `asyncio`, existing `lugo_stream`.
- Produces: after `idle_timeout_s>0` of no inbound message and no active turn, server sends `{"type":"goodbye","reason":"idle_timeout"}` then closes.

**Design:** run a watchdog task alongside the receive loop. Reset a monotonic `last_activity` on every inbound message and whenever a turn is running; the watchdog wakes each second and, if `idle>0` and `now-last_activity >= idle` and no turn active, emits `goodbye` and cancels the loop. Keep it simple: track activity via a shared mutable timestamp updated in the receive loop; treat "turn active" as `session.is_turn_active()` (add a tiny property to `ConversationSession` returning `bool(self._current_turn and not self._current_turn.done())`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lugo_idle_timeout.py
import pytest
from fastapi.testclient import TestClient
from app.core.settings import settings
from app.main import app
from app.schemas.stt import STTResult
from app.services.profiles.models import Profile, SessionConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service


class _StubSTT(STTProvider):
    name = "stub-idle-stt"
    async def transcribe_bytes(self, audio_bytes, language=None) -> STTResult:
        return STTResult(engine=self.name, text="", is_final=True)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "conversation_stt_engine", "stub-idle-stt")
    stt_service.providers["stub-idle-stt"] = _StubSTT()
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    fresh.upsert(Profile(name="fast", session=SessionConfig(idle_timeout_s=1)))
    monkeypatch.setattr("app.api.routes.lugo.profile_store", fresh)
    # Make the watchdog tick fast so the test is quick.
    monkeypatch.setattr("app.api.routes.lugo._IDLE_TICK_S", 0.1, raising=False)
    yield
    stt_service.providers.pop("stub-idle-stt", None)


def test_idle_timeout_emits_goodbye():
    with TestClient(app).websocket_connect("/v1/lugo/stream") as ws:
        ws.send_json({"type": "wakeup", "profile": "fast",
                      "audio_params": {"format": "opus", "sample_rate": 16000}})
        assert ws.receive_json()["type"] == "welcome"
        # say nothing; within ~1s the server should give up
        msg = ws.receive_json()
        assert msg["type"] == "goodbye"
        assert msg["reason"] == "idle_timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_lugo_idle_timeout.py -v`
Expected: FAIL (no goodbye; receive blocks/times out)

- [ ] **Step 3: Add the watchdog to `lugo.py`**

Add near the top: `import asyncio`, `import time`, and `_IDLE_TICK_S = 1.0`. Add the property to `ConversationSession` (Task 3 file):

```python
    def is_turn_active(self) -> bool:
        return bool(self._current_turn and not self._current_turn.done())
```

Replace the receive loop in `lugo_stream` with an activity-tracked version guarded by a watchdog:

```python
    last_activity = time.monotonic()

    async def _watchdog() -> None:
        if idle <= 0:
            return
        while True:
            await asyncio.sleep(_IDLE_TICK_S)
            if session.is_turn_active():
                continue
            if time.monotonic() - last_activity >= idle:
                try:
                    await websocket.send_json({"type": "goodbye", "reason": "idle_timeout"})
                except RuntimeError:
                    pass
                return

    wd = asyncio.create_task(_watchdog())
    try:
        while True:
            if wd.done():
                break
            recv = asyncio.create_task(websocket.receive())
            done, _pending = await asyncio.wait({recv, wd}, return_when=asyncio.FIRST_COMPLETED)
            if wd in done:
                recv.cancel()
                break
            message = recv.result()
            last_activity = time.monotonic()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await session.feed_audio(message["bytes"])
            if message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "text":
                    await session.feed_text(control.get("text") or "")
                elif ctype == "abort":
                    await session.abort("barge-in")
                elif ctype == "listen":
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        wd.cancel()
        await session.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_lugo_idle_timeout.py tests/unit/test_lugo_stream.py tests/unit/test_lugo_barge_in.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/api/routes/lugo.py apps/api_gateway/app/services/conversation/session.py tests/unit/test_lugo_idle_timeout.py
git commit -m "feat(lugo): idle-timeout watchdog emits goodbye and closes"
```

---

### Task 7: Full suite + docs

**Files:**
- Modify: `docs/api.md` (document `WS /v1/lugo/stream`)

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (previous count + the new Lugo/frame/session/profile tests).

- [ ] **Step 2: Document the endpoint**

Add a `WS /v1/lugo/stream` section to `docs/api.md` mirroring the `/v1/conversation/stream` section: the wakeup/welcome handshake, message table (Phase 1), v3 framing, and the connect-on-wake / idle-disconnect / barge-in lifecycle. Do not use the string "xiaozhi".

- [ ] **Step 3: Commit**

```bash
git add docs/api.md
git commit -m "docs(lugo): document /v1/lugo/stream protocol"
```

---

## Self-review

**Spec coverage:**
- Protocol name Lugo, no "xiaozhi" → Global Constraints + Task 7 doc note. ✓
- `wakeup`/`welcome` + message set → Task 4. ✓
- v3 framing → Task 2 (codec) + Task 4 (downlink audio). ✓
- Profile-as-identity, server resolves engines → Task 4 `_resolve`. ✓
- `Profile.session.idle_timeout_s` + `welcome` carries it → Task 1 + Task 4. ✓
- One shared `ConversationSession` core, event shim preserved → Task 3. ✓
- Barge-in keeps connection → Task 5. ✓
- Idle timeout → goodbye, server-owned → Task 6. ✓
- Coexistence / legacy `/v1/conversation/stream` unchanged wire → Task 3 regression gate. ✓
- Testing (core unit, frame unit, ws tests, regression) → Tasks 2,3,4,5,6,7. ✓

**Deferred to Phase 2 (correctly absent):** ESP-SR wake word, `listen{detect}` behavior, remote-call/MQTT, `llm{emotion}`, JSON-over-binary, firmware, agent/RPi/browser migration (separate plans).

**Type consistency:** `SessionRuntimeConfig` field set is identical in Task 3 definition, Task 3 route construction, and Task 4 construction. `emit`/`emit_audio` signatures match across Tasks 3–6. `is_turn_active()`/`_current_turn` introduced in Task 3 and used in Task 6.

**Known risk:** Task 3 is a large refactor; its gate is the existing suite (unchanged `{"event": ...}` wire). If the pre-resolution block references a closure that moved into the core, pass it through `SessionRuntimeConfig` instead of importing back.
