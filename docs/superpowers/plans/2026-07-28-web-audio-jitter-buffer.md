# Web Audio Playback Jitter Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop web client audio playback from stuttering on good networks by letting the web client disable server-side real-time Opus pacing (tuned for tiny ESP32/RPi ring buffers) and rely on the browser's own `AudioContext` scheduling as its jitter buffer instead — without changing device (ESP32/RPi) behavior at all.

**Architecture:** Add one optional field, `opus_pace: bool | None`, to the per-connection `SessionRuntimeConfig` dataclass in `apps/api_gateway/app/services/conversation/session.py`. `None` (the default, and the only value `api/routes/lugo.py` ever produces) inherits the existing global `system_config.conversation.conversation_opus_pace`; `api/routes/conversation.py` (web) parses a new `opus_pace` query param and the web client always sends `opus_pace=0`. On the client, `lugo-web-client/src/audio/player.ts` gets a small fixed startup-lead constant applied only to the first scheduled chunk of a turn, as a cushion against main-thread jank at the one moment nothing is queued yet.

**Tech Stack:** Python 3.14 / FastAPI / pytest (backend, `apps/api_gateway`), TypeScript / Vitest (frontend, `lugo-web-client`).

## Global Constraints

- Zero behavior change for `api/routes/lugo.py` (ESP32/RPi) sessions — verified by never setting `opus_pace` there, so it stays `None` and falls through to the untouched global default.
- No new WS protocol messages, no adaptive/feedback pacing — per the design doc's rejected-approaches section.
- Follow existing patterns exactly: reuse `_truthy()` in `conversation.py` (already used for `denoise`), reuse the `_cfg()`/`_stub`-fixture style already used in this test suite.
- Design reference: `docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md` (already written and committed — do not re-derive rationale, cite it).
- `AGENTS.md` and `docs/api.md` are already updated with this feature's gotchas (commit `f6e0dca`) — no doc-writing steps in this plan.

---

### Task 1: Server — per-connection `opus_pace` override

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/session.py:129` (dataclass field), `:613` (pacing decision)
- Modify: `apps/api_gateway/app/api/routes/conversation.py:334` (query parsing), `:348-358` (`SessionRuntimeConfig` construction)
- Test: Create `tests/integration/test_conversation_opus_pace_override.py`

**Interfaces:**
- Produces: `SessionRuntimeConfig.opus_pace: bool | None = None` — every other task/file that constructs `SessionRuntimeConfig` (e.g. `api/routes/lugo.py`) is unaffected since it's a new field with a default.
- Produces: `/v1/conversation/stream` accepts an `opus_pace` query param (`"0"`/`"false"`/`"1"`/`"true"`/etc, via the existing `_truthy()` helper in `conversation.py`); absence means "inherit global config".

- [ ] **Step 1: Write the failing integration tests**

Create `tests/integration/test_conversation_opus_pace_override.py`:

```python
"""Per-connection opus_pace override: proves the web client can disable
server-side Opus playback pacing for its own session without touching the
global config that api/routes/lugo.py (ESP32/RPi) relies on. See
docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSRequest
from app.services.profiles.models import Profile, SttConfig
from app.services.profiles.store import ProfileStore
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import RenderingTTSProvider
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import TtsProfileStore
from app.services.tts.service import tts_service


def _opus_ok():
    # Same pattern as tests/integration/test_gateway_modalities.py's
    # _opus_ok(): must route through opus_available()'s libopus-findable shim,
    # a bare `import opuslib` depends on collection order.
    from app.core.opus import opus_available

    if not opus_available():
        return False
    import opuslib

    try:
        opuslib.Encoder(24000, 1, opuslib.APPLICATION_VOIP)
        return True
    except Exception:  # noqa: BLE001
        return False


class _StubSTT(STTProvider):
    name = "stub-pace-stt"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(RenderingTTSProvider):
    """600ms of silence -> 10 Opus frames (60ms each) -- well past the
    default 5-frame prebuffer, so paced vs unpaced delivery time is
    measurably different (see the two tests below)."""

    name = "stub-pace-tts"
    sample_rate = 24000

    async def _render_wav(self, payload: TTSRequest) -> bytes:
        n = int(self.sample_rate * 600 / 1000)
        return pcm16_to_wav_bytes(b"\x00\x00" * n, sample_rate=self.sample_rate)


@pytest.fixture(autouse=True)
def _stub(monkeypatch, tmp_path):
    stt_service.providers["stub-pace-stt"] = _StubSTT()
    tts_service.providers["stub-pace-tts"] = _StubTTS()

    fresh_profiles = ProfileStore(str(tmp_path / "profiles.json"))
    fresh_profiles.upsert(Profile(name="p-pace", stt=SttConfig(engine="stub-pace-stt")))
    monkeypatch.setattr("app.api.routes.conversation.profile_store", fresh_profiles)

    fresh_tts_profiles = TtsProfileStore(str(tmp_path / "tts_profiles.json"))
    fresh_tts_profiles.upsert(TtsProfile(name="p-pace-tts", engine="stub-pace-tts"))
    monkeypatch.setattr("app.api.routes.conversation.tts_profile_store", fresh_tts_profiles)

    # system_config_store is a real singleton shared by every test in the run
    # (see conftest.py's _hermetic docstring) -- mutating it in place would
    # leak into unrelated tests, so point at a fresh, tmp_path-scoped store
    # instead. We don't change any values on it (conversation_opus_pace's
    # real default is True, conversation_opus_prebuffer_frames's is 5 --
    # exactly what these tests want to prove behavior against); this is only
    # isolation from whatever state the shared store happens to be in.
    #
    # All THREE modules that did `from app.services.system_config import
    # system_config_store` hold independent name bindings and must each be
    # patched individually -- patching only `app.api.routes.conversation` and
    # `app.services.system_config` (the "dual-binding gotcha" pattern used
    # elsewhere in this suite, e.g. test_gateway_modalities.py) does NOT reach
    # `app.services.conversation.session`, which is where the actual pacing
    # decision is made. Verified empirically: after that two-module patch,
    # `app.services.conversation.session.system_config_store is fresh_config`
    # is False.
    from app.services import system_config as sc_mod

    fresh_config = sc_mod.SystemConfigStore(str(tmp_path / "system_config.json"))
    monkeypatch.setattr("app.api.routes.conversation.system_config_store", fresh_config)
    monkeypatch.setattr("app.services.conversation.session.system_config_store", fresh_config)
    monkeypatch.setattr(sc_mod, "system_config_store", fresh_config)

    yield

    stt_service.providers.pop("stub-pace-stt", None)
    tts_service.providers.pop("stub-pace-tts", None)


def _drain_frames(ws, max_events=100):
    """Consume one whole turn; return (binary_frame_count, elapsed_seconds)."""
    t0 = time.monotonic()
    frames = 0
    for _ in range(max_events):
        msg = ws.receive()
        if msg.get("bytes") is not None:
            frames += 1
            continue
        ev = json.loads(msg["text"])["event"]
        if ev == "turn_done":
            break
    return frames, time.monotonic() - t0


@pytest.mark.skipif(not _opus_ok(), reason="libopus not loadable")
def test_opus_pace_query_override_skips_realtime_pacing():
    # Global conversation_opus_pace stays at its real default (True) -- the
    # query param must override it for THIS session only.
    c = TestClient(app)
    url = (
        "/v1/conversation/stream?profile=p-pace&tts_profile=p-pace-tts"
        "&output=audio&audio_out=opus&output_sample_rate=24000&opus_pace=0"
    )
    with c.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_json({"type": "text", "text": "xin chao"})
        frames, elapsed = _drain_frames(ws)
    assert frames == 10
    # True paced delivery of 10 frames past a 5-frame prebuffer takes >=300ms
    # (see the next test) -- unpaced must be far under that.
    assert elapsed < 0.15


@pytest.mark.skipif(not _opus_ok(), reason="libopus not loadable")
def test_opus_pace_omitted_still_paces_by_default():
    # No opus_pace param -- must inherit the global default (True), exactly
    # what api/routes/lugo.py (ESP32/RPi) gets today. This test passes
    # unchanged before AND after Task 1's production code -- it's a
    # regression guard, not new behavior.
    c = TestClient(app)
    url = (
        "/v1/conversation/stream?profile=p-pace&tts_profile=p-pace-tts"
        "&output=audio&audio_out=opus&output_sample_rate=24000"
    )
    with c.websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "session_started"
        ws.send_json({"type": "text", "text": "xin chao"})
        frames, elapsed = _drain_frames(ws)
    assert frames == 10
    # 10 frames, 5-frame prebuffer -> 5 frames paced 60ms apart = >=300ms.
    assert elapsed >= 0.25
```

- [ ] **Step 2: Run the new tests to verify the override test fails**

Run: `.venv/bin/python -m pytest tests/integration/test_conversation_opus_pace_override.py -v`

Expected: `test_opus_pace_query_override_skips_realtime_pacing` FAILS (`elapsed` is
~0.3s+, not `< 0.15`) — the `opus_pace=0` query param doesn't exist yet, so the
route ignores it and paces exactly like the default case.
`test_opus_pace_omitted_still_paces_by_default` PASSES already — that's expected,
it's a regression guard for behavior that isn't changing, not new behavior. If
libopus isn't loadable on this machine both tests SKIP; if so, still proceed —
Step 4 gets verified by CI/whoever has libopus, and the dataclass-level change
is trivially safe.

- [ ] **Step 3: Add the `opus_pace` field to `SessionRuntimeConfig`**

In `apps/api_gateway/app/services/conversation/session.py`, find (line 129):

```python
    identity_user_id: str | None = None
```

Replace with:

```python
    identity_user_id: str | None = None
    # Per-connection override of Opus playback pacing. None (the default, and
    # the only value api/routes/lugo.py ever produces) means "not specified,
    # inherit system_config.conversation.conversation_opus_pace" -- so
    # ESP32/RPi sessions are byte-for-byte unaffected by this field's
    # existence. api/routes/conversation.py (web) sets it from the
    # `opus_pace` query param so the web client can disable the ~300ms
    # real-time drip-feed sized for embedded ring buffers, without touching
    # the global default devices rely on. See
    # docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md.
    opus_pace: bool | None = None
```

- [ ] **Step 4: Wire the override into the pacing decision**

In the same file, find (around line 612-614, inside `_stream_to_tts`):

```python
                _conv_cfg = system_config_store.get().conversation
                _do_pace = _conv_cfg.conversation_opus_pace
                _prebuf = _conv_cfg.conversation_opus_prebuffer_frames
```

Replace with:

```python
                _conv_cfg = system_config_store.get().conversation
                _do_pace = cfg.opus_pace if cfg.opus_pace is not None else _conv_cfg.conversation_opus_pace
                _prebuf = _conv_cfg.conversation_opus_prebuffer_frames
```

(`cfg` is already `self.cfg`, assigned at the top of `_run_turn` — see line 478 —
and captured by the `_stream_to_tts` closure; no new import or variable needed.)

- [ ] **Step 5: Parse the query param in the web route**

In `apps/api_gateway/app/api/routes/conversation.py`, find (line 334):

```python
    output_sample_rate = int(q.get("output_sample_rate", 24000))
```

Replace with:

```python
    output_sample_rate = int(q.get("output_sample_rate", 24000))
    # Per-connection override of Opus playback pacing (None = inherit the
    # global system_config default -- what api/routes/lugo.py always gets).
    # Web sends opus_pace=0: see
    # docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md.
    _opus_pace_raw = q.get("opus_pace")
    opus_pace = _truthy(_opus_pace_raw, True) if _opus_pace_raw is not None else None
```

Then find the `SessionRuntimeConfig(...)` construction (around line 348-358):

```python
    cfg = SessionRuntimeConfig(
        session_id=session_id, profile_name=profile_name, stt_engine=stt_engine,
        language=language, tts_engine=tts_engine, voice=voice,
        ref_audio_path=ref_audio_path, ref_text=ref_text, tts_instruct=tts_instruct,
        tts_speed=tts_speed, tts_language=tts_language, sample_rate=sample_rate,
        output_sample_rate=output_sample_rate, audio_codec=audio_codec,
        want_audio=want_audio, want_text=want_text, audio_out=audio_out,
        denoise=denoise, resume_sid=requested_sid, stt_model=stt_model,
        tts_model=tts_model,
        identity_user_id=identity.user_id,
    )
```

Replace with:

```python
    cfg = SessionRuntimeConfig(
        session_id=session_id, profile_name=profile_name, stt_engine=stt_engine,
        language=language, tts_engine=tts_engine, voice=voice,
        ref_audio_path=ref_audio_path, ref_text=ref_text, tts_instruct=tts_instruct,
        tts_speed=tts_speed, tts_language=tts_language, sample_rate=sample_rate,
        output_sample_rate=output_sample_rate, audio_codec=audio_codec,
        want_audio=want_audio, want_text=want_text, audio_out=audio_out,
        denoise=denoise, resume_sid=requested_sid, stt_model=stt_model,
        tts_model=tts_model,
        identity_user_id=identity.user_id,
        opus_pace=opus_pace,
    )
```

**Do not touch `apps/api_gateway/app/api/routes/lugo.py`** — it must keep
constructing `SessionRuntimeConfig` without `opus_pace` so the field stays
`None` for every device session.

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/integration/test_conversation_opus_pace_override.py -v`

Expected: both tests PASS (or both SKIP if libopus isn't loadable on this
machine — in that case also run Step 7 to confirm no regressions, then proceed;
the skip is environment-specific, not a plan failure).

- [ ] **Step 7: Run the existing conversation/session test files to check for regressions**

Run: `.venv/bin/python -m pytest tests/integration/test_gateway_modalities.py tests/unit/test_conversation_session_core.py tests/integration/test_conversation_ws.py tests/unit/test_profile_session_config.py -v`

Expected: all PASS, unchanged from before this task (these exercise
`SessionRuntimeConfig` and `_stream_to_tts` without ever setting `opus_pace`,
so they must behave exactly as before).

- [ ] **Step 8: Lint and commit**

Run: `make lint`

Then:

```bash
git add apps/api_gateway/app/services/conversation/session.py \
        apps/api_gateway/app/api/routes/conversation.py \
        tests/integration/test_conversation_opus_pace_override.py
git commit -m "feat(conversation): per-connection opus_pace override for web playback jitter"
```

---

### Task 2: Web client — request pacing off

**Files:**
- Modify: `lugo-web-client/src/audio/conversation.ts:12-24` (`buildParams`)
- Test: Modify `lugo-web-client/src/audio/conversation.test.ts:22-42` (`describe('buildParams', ...)`)

**Interfaces:**
- Consumes: Task 1's `opus_pace` query param (`/v1/conversation/stream?...&opus_pace=0`).
- Produces: nothing new consumed by later tasks — this and Task 3 are independent.

- [ ] **Step 1: Write the failing test**

In `lugo-web-client/src/audio/conversation.test.ts`, inside the existing
`describe('buildParams', ...)` block (after the `'adds session_id only when given'`
test, before the closing `})` at line 42), add:

```ts
  it('disables server-side real-time pacing (browser owns the jitter buffer)', () => {
    // See docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md --
    // the 300ms server prebuffer is sized for ESP32/RPi ring buffers, not
    // browsers, which can hold seconds of audio in AudioContext instead.
    expect(buildParams().get('opus_pace')).toBe('0')
  })
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `lugo-web-client/`): `pnpm test src/audio/conversation.test.ts`

Expected: FAIL — `buildParams().get('opus_pace')` is `null`, not `'0'`.

- [ ] **Step 3: Update `buildParams`**

In `lugo-web-client/src/audio/conversation.ts`, find (lines 12-24):

```ts
export function buildParams(profile?: string, sessionId?: string): URLSearchParams {
  const p = new URLSearchParams({
    // Opus over the already-authenticated socket: audio_out=url would point
    // at /artifacts, which has NO auth -- anyone with the URL could listen in.
    audio_out: 'opus',
    output: 'audio,text',
    sample_rate: '16000',
    output_sample_rate: '24000',
  })
  if (profile) p.set('profile', profile)
  if (sessionId) p.set('session_id', sessionId)
  return p
}
```

Replace with:

```ts
export function buildParams(profile?: string, sessionId?: string): URLSearchParams {
  const p = new URLSearchParams({
    // Opus over the already-authenticated socket: audio_out=url would point
    // at /artifacts, which has NO auth -- anyone with the URL could listen in.
    audio_out: 'opus',
    output: 'audio,text',
    sample_rate: '16000',
    output_sample_rate: '24000',
    // Disable the server's ~300ms real-time pacer (sized for ESP32/RPi ring
    // buffers). AudioContext can hold seconds of scheduled audio, so letting
    // packets arrive as fast as they're synthesized -- instead of drip-fed to
    // match playback speed -- gives the browser a much bigger natural jitter
    // cushion. See docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md.
    opus_pace: '0',
  })
  if (profile) p.set('profile', profile)
  if (sessionId) p.set('session_id', sessionId)
  return p
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pnpm test src/audio/conversation.test.ts`

Expected: all tests in this file PASS, including the new one.

- [ ] **Step 5: Commit**

```bash
git add lugo-web-client/src/audio/conversation.ts lugo-web-client/src/audio/conversation.test.ts
git commit -m "feat(web-client): request opus_pace=0 so playback isn't throttled to device pacing"
```

---

### Task 3: Web client — startup lead for the first chunk of a turn

**Files:**
- Modify: `lugo-web-client/src/audio/player.ts`
- Test: Modify `lugo-web-client/src/audio/player.test.ts`

**Interfaces:**
- Produces: `scheduleStartTime(now: number, cursor: number): number` — exported
  pure function, same shape as the existing `nextStartTime`. Used internally by
  `Player.schedule()`; nothing outside this file needs to call it, but it's
  exported so it's directly unit-testable like `nextStartTime`/`chunkDuration`
  already are.

- [ ] **Step 1: Write the failing tests**

In `lugo-web-client/src/audio/player.test.ts`, change the import (line 2):

```ts
import { chunkDuration, nextStartTime } from './player'
```

to:

```ts
import { chunkDuration, nextStartTime, scheduleStartTime } from './player'
```

Then add a new `describe` block after the existing `describe('nextStartTime', ...)`
block (after line 26, before `describe('chunkDuration', ...)`):

```ts
describe('scheduleStartTime', () => {
  it('gives the first chunk of a turn extra lead beyond "now"', () => {
    // cursor === 0 means nothing has been scheduled yet this turn -- nothing
    // is queued ahead to absorb a main-thread hiccup at this exact moment, so
    // this one chunk gets a small explicit cushion instead.
    expect(scheduleStartTime(5, 0)).toBeCloseTo(5.1)
  })

  it('adds no extra lead once the turn has already started (cursor ahead)', () => {
    // Later chunks already queue onto real scheduled audio -- behaves exactly
    // like nextStartTime.
    expect(scheduleStartTime(5, 10)).toBe(10)
  })

  it('still catches up to now for a mid-turn stall (cursor behind, nonzero)', () => {
    expect(scheduleStartTime(20, 10)).toBeGreaterThanOrEqual(20)
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from `lugo-web-client/`): `pnpm test src/audio/player.test.ts`

Expected: FAIL with "scheduleStartTime is not exported" / a TypeScript error —
the function doesn't exist yet.

- [ ] **Step 3: Add `STARTUP_LEAD_S` and `scheduleStartTime`, wire into `schedule()`**

In `lugo-web-client/src/audio/player.ts`, find (line 3):

```ts
const OUTPUT_SAMPLE_RATE = 24000
```

Replace with:

```ts
const OUTPUT_SAMPLE_RATE = 24000
// Extra lead for the very first chunk of a turn (cursor === 0, nothing
// scheduled yet). Every later chunk in the turn already queues onto audio
// that's already scheduled (see scheduleStartTime) and needs no help -- only
// the first one has zero cushion against main-thread jank at the exact
// moment it arrives. See
// docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md.
const STARTUP_LEAD_S = 0.1
```

Then find the end of the `nextStartTime` function (lines 12-14):

```ts
export function nextStartTime(now: number, cursor: number): number {
  return Math.max(now, cursor)
}
```

Immediately after it, add:

```ts

/** Where to start playing THIS chunk. Delegates to nextStartTime, except the
 * very first chunk of a turn (cursor still 0) gets an extra STARTUP_LEAD_S of
 * lead -- every later chunk already has real scheduled audio ahead of it to
 * absorb jank, but the first one has nothing yet. */
export function scheduleStartTime(now: number, cursor: number): number {
  return nextStartTime(cursor === 0 ? now + STARTUP_LEAD_S : now, cursor)
}
```

Finally, in `Player.schedule()`, find (around line 78):

```ts
    const at = nextStartTime(ctx.currentTime, this.cursor)
```

Replace with:

```ts
    const at = scheduleStartTime(ctx.currentTime, this.cursor)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm test src/audio/player.test.ts`

Expected: all tests in this file PASS, including the 3 new ones. The existing
`nextStartTime` tests must still pass unchanged (that function's behavior and
signature didn't change).

- [ ] **Step 5: Commit**

```bash
git add lugo-web-client/src/audio/player.ts lugo-web-client/src/audio/player.test.ts
git commit -m "feat(web-client): give the first chunk of a turn a small startup lead"
```

---

## Manual verification (post-implementation, not a task)

After all 3 tasks land: open the web client, start a conversation, and in
Chrome DevTools Network conditioning apply a mid-range latency/jitter profile
mid-call. Confirm playback no longer stutters compared to before this change.
This is exploratory/manual (per the project's UI-testing convention) — not a
pass/fail gate the plan can encode as a step.
