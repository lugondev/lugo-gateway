# Drop Server-Side Audio Artifacts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The gateway never writes synthesized audio to disk — reply audio reaches clients as binary frames over the already-open WebSocket instead of as a fetchable `/artifacts/*.wav` URL.

**Architecture:** `TTSProvider.render_audio(payload) -> (bytes, media_type)` becomes the single seam that produces audio; `synthesize()`, `TTSResult`, `ArtifactStore.save_wav/save_mp3` and the `/artifacts` HTTP mount are deleted. The conversation and livehost sockets gain an `audio_out="wav"` transport (`audio_start` → one binary frame per sentence → `audio_end`) that mirrors the existing Opus transport. The artifact directory survives, narrowed to voice-clone reference audio.

**Tech Stack:** Python 3.12, FastAPI, pytest/pytest-asyncio, vanilla-ES-module static UI, React + Vitest (`lugo-web-client`).

**Spec:** `docs/superpowers/specs/2026-07-29-drop-audio-artifacts-design.md`

## Global Constraints

- **Branch:** work on `feat/drop-audio-artifacts` off `main`. Do NOT commit to `main` — pushing `main` deploys production.
- **Git identity:** commit as `lugondev <lugondev@gmail.com>`. Never use the Claude-account email.
- **Test scope while working:** run ONLY the specific test case or file you are touching (`pytest tests/path/test_x.py::test_y -v`). The repo has a pytest concurrency guard — two overlapping full-suite runs deadlock. Run the full suite exactly once, in Task 9.
- **Reference audio is out of scope and must keep working:** `save_reference_audio`, `contains`, `path_for`, `ref_audio_path`, `POST /v1/tts/reference-audio`, and the `TTSRequest.ref_audio_path` containment validator are untouched by every task below.
- **Directory and setting names are frozen:** keep `artifact_store`, `ARTIFACTS_DIR`, `settings.artifacts_dir_resolved` and the on-disk path. Persisted `TtsProfile.ref_audio_path` rows hold absolute paths into that directory; renaming breaks live data.
- **Static UI (`apps/api_gateway/app/static/js/*`) has no automated test harness.** Verify those edits by reading the file back with the Read tool — a syntax check is not sufficient (smart-quote corruption has passed `node --check` in this repo before).
- **Wire format, used verbatim by Tasks 1 and 2:**
  ```
  audio_start { turn, chunk_index, text?, codec: "wav" | "mp3" }
  <one binary frame: the complete container>
  audio_end   { turn, chunk_index }
  ```
  `codec` maps from the provider's media type: `audio/wav` → `"wav"`, `audio/mpeg` → `"mp3"`.

---

### Task 1: Conversation WebSocket — WAV downlink replaces `audio_out=url`

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/session.py` (`:135`, `:228`, `:283-290`, `:600-719`, `:891-933`)
- Modify: `apps/api_gateway/app/api/routes/conversation.py:370-372`
- Modify: `apps/api_gateway/app/static/js/conversation.js` (`:47-66`, `:309-316`, `:339`, `:353-358`, `:409-415`)
- Test: `tests/unit/conversation/test_session_opus_nodisk.py` (primary), plus stub migration in 13 more files listed in Step 7

**Interfaces:**
- Consumes: `TTSProvider.render_audio(payload) -> tuple[bytes, str]` (already exists, `services/tts/base.py:52`), `wav_bytes_to_pcm16(bytes, target_sr) -> bytes` (`core/audio.py:95`, already handles MP3 through its `soundfile` fallback).
- Produces: `SessionRuntimeConfig.audio_out` now takes `"wav" | "opus"`; `ConversationSession.output_sample_rate_effective` returns the rate only for `"opus"`. Task 2 mirrors this shape for livehost.

- [ ] **Step 1: Write the failing test**

Replace `test_url_mode_still_calls_synthesize_and_emits_audio_url` in `tests/unit/conversation/test_session_opus_nodisk.py:180-192` with the WAV-downlink test. Reuse the file's existing helpers: `_silence_wav()` (`:37`), `_FakeRenderingTTS` (`:49`), `_cfg(**over)` (`:99`) and `_drive_text_turn(cfg) -> (session, events, audio_frames)` (`:111`).

```python
@pytest.mark.asyncio
async def test_wav_mode_pushes_one_binary_frame_per_sentence():
    fake_wav = _silence_wav()
    provider = _FakeRenderingTTS(fake_wav)
    tts_service.providers["stub-opus-nodisk-render-tts"] = provider

    _session, events, audio_frames = await _drive_text_turn(
        _cfg(audio_out="wav", tts_engine="stub-opus-nodisk-render-tts")
    )

    assert not [p for n, p in events if n == "audio_chunk"]  # event is gone
    starts = [p for n, p in events if n == "audio_start"]
    ends = [p for n, p in events if n == "audio_end"]
    assert len(starts) == len(ends) == 1
    assert starts[0]["codec"] == "wav"
    assert len(audio_frames) == 1
    assert audio_frames[0][:4] == b"RIFF"
```

(If `_drive_text_turn`'s tuple order differs when you open the file, follow the file — the neighbouring `test_opus_mode_...` at `:130` shows the exact call shape.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/conversation/test_session_opus_nodisk.py::test_wav_mode_pushes_one_binary_frame_per_sentence -v`
Expected: FAIL — the session emits `audio_chunk` with an `audio_url` and pushes no binary frames.

- [ ] **Step 3: Collapse `_synth` onto `render_audio`**

In `session.py`, replace the whole engine-type branch at `:610-634` with:

```python
                    audio, media_type = await self.tts_provider.render_audio(request)
                    await self._record_tts_usage(sentence)
                    if self.opus_encoder is not None:
                        pcm = await asyncio.to_thread(wav_bytes_to_pcm16, audio, cfg.output_sample_rate)
                        packets = await asyncio.to_thread(self.opus_encoder.encode_pcm16, pcm)
                        return None, packets, None
                    return (audio, media_type), None, None
```

The `isinstance(self.tts_provider, RenderingTTSProvider)` fast path existed only to dodge `synthesize()`'s artifact write; `render_audio()` never writes, so both engine kinds take one path. Drop the now-unused `RenderingTTSProvider` import and the `wav_file_to_pcm16` import from this module.

- [ ] **Step 4: Emit the WAV frames in the consume loop**

In the same function, the pipeline loop unpacks `(result, packets, tts_error)`. Rename the first element to `audio` and replace the `else` branch at `:715-719` with:

```python
                    else:
                        audio_bytes, media_type = audio
                        if self._speaking_since is None:
                            self._speaking_since = time.monotonic()
                        await self.emit(
                            "audio_start", turn=turn, chunk_index=index,
                            text=sentence if want_text else None,
                            codec="mp3" if media_type == "audio/mpeg" else "wav",
                        )
                        await self.emit_audio(audio_bytes)
                        await self.emit("audio_end", turn=turn, chunk_index=index)
```

Setting `_speaking_since` here is new and load-bearing: in url mode the browser fetched the audio itself, so the server had no idea when playback started; now the server pushes it, and the barge-in echo grace window in `feed_audio` needs the same marker the Opus branch sets at `:681-682`.

Apply the identical treatment to `speak()` (`:891-933`): call `render_audio()` once, drop the `isinstance` branch and the `wav_file_to_pcm16` fallback at `:908`, and replace the `audio_chunk` emit at `:931-933` with the same `audio_start` / `emit_audio` / `audio_end` trio (`chunk_index=0`).

- [ ] **Step 5: Retire the `"url"` transport value**

`session.py:135` — the field comment becomes `audio_out: str  # "wav" | "opus"`.

`session.py:228`:

```python
        return self.cfg.output_sample_rate if self.cfg.want_audio and self.audio_out == "opus" else None
```

`session.py:289` — the no-libopus downgrade becomes `self.audio_out = "wav"` and its log message `"client requested opus output but server has no libopus; using wav"`.

`api/routes/conversation.py:370-372`:

```python
    # How reply audio is delivered: "wav" (one complete container per sentence,
    # pushed as a binary frame) or "opus" (60ms frames, ~10x less bandwidth --
    # ESP32/RPi and WebCodecs browsers). Nothing is ever written to disk.
    audio_out = (q.get("audio_out") or "wav").lower()
    if audio_out != "opus":
        audio_out = "wav"
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `pytest tests/unit/conversation/test_session_opus_nodisk.py -v`
Expected: PASS (both the new WAV test and the existing Opus no-disk test).

- [ ] **Step 6b: Rewrite the non-rendering-provider Opus test**

`test_session_opus_nodisk.py:196` (`test_opus_mode_falls_back_to_synthesize_for_non_rendering_provider`) asserts the old file-based fallback: a plain `TTSProvider` (the `_NonRenderingTTS` stub at `:70`, standing in for edge_tts/MP3) went through `synthesize()` and had its artifact read back off disk. There is no fallback any more — both engine kinds go through `render_audio()`. Rename it to `test_opus_mode_encodes_non_rendering_provider_bytes_without_touching_disk`, have `_NonRenderingTTS.render_audio` return `(mp3_or_wav_bytes, "audio/mpeg")`, and assert Opus packets came out while `artifact_store.base_dir` gained no file during the turn:

```python
@pytest.mark.asyncio
async def test_opus_mode_encodes_non_rendering_provider_bytes_without_touching_disk():
    before = set(artifact_store.base_dir.iterdir())
    _session, _events, audio_frames = await _drive_text_turn(
        _cfg(audio_out="opus", tts_engine="stub-opus-nodisk-nonrender-tts")
    )
    assert audio_frames  # opus packets were produced
    assert set(artifact_store.base_dir.iterdir()) == before
```

This is the spec's "edge_tts (MP3) over Opus" case: `wav_bytes_to_pcm16`'s `soundfile` fallback (`core/audio.py:110-112`) is what decodes a non-WAV container in memory.

Run: `pytest tests/unit/conversation/test_session_opus_nodisk.py -v`
Expected: PASS.

- [ ] **Step 7: Migrate the conversation test stubs**

These files define `class _StubTTS(TTSProvider)` with `async def synthesize(...) -> TTSResult(...)`. Convert each to:

```python
    async def render_audio(self, payload) -> tuple[bytes, str]:
        return _silence_wav(), "audio/wav"
```

using a module-local WAV builder (copy the `_silence_wav` helper from `tests/unit/conversation/test_session_opus_nodisk.py` where the file has none). Any assertion on `audio_chunk` / `audio_url` becomes an assertion on `audio_start` + binary frame.

Files: `tests/unit/conversation/test_conversation_session_core.py`, `test_conversation_tts_profile.py`, `test_lugo_authz.py`, `test_lugo_barge_in.py`, `test_lugo_idle_timeout.py`, `test_lugo_stream.py`, `test_session_bad_ref_audio_path_degrades.py`, `test_session_refresh_memory_scoping.py`, `test_session_tts_failure.py`, `test_session_usage_metering.py`, `tests/integration/test_conversation_ws.py`, `test_gateway_modalities.py`, `test_opus_transport.py`, `test_session_attribution.py`, `test_ws_auth.py`.

Run each file as you convert it: `pytest <file> -v`. Expected: PASS.

- [ ] **Step 8: Update the static UI**

`static/js/conversation.js` — delete `convEnqueueAudio(url)` (`:47-66`) and add, next to `convScheduleBuffer`:

```js
// Reply audio arrives as one complete WAV/MP3 per sentence on a binary frame.
export function convEnqueueAudioBytes(bytes) {
  conv.chain = (conv.chain || Promise.resolve())
    .then(async () => {
      const ctx = convAudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      const buf = await ctx.decodeAudioData(bytes);
      convScheduleBuffer(buf);
    })
    .catch((e) => convLog("audio error: " + e));
}
```

At `:339` set `ws.binaryType = "arraybuffer"` unconditionally (drop the `if (conv.opusMode)` guard).

At `:353-358` route binary frames by the codec the server announced:

```js
    if (typeof event.data !== "string") {
      if (conv.outCodec === "opus") convFeedOpus(event.data);
      else convEnqueueAudioBytes(event.data);
      return;
    }
```

In the `audio_start` case (`:409-412`) record it, and reset it when a session starts:

```js
      case "audio_start":
        conv.outCodec = d.codec || "wav";
        if (d.codec === "opus" && d.sample_rate) conv.outRate = d.sample_rate;
        break;
```

Delete the `audio_chunk` case (`:413-415`). In the connect path (`:309-316`) leave the Opus opt-in as-is — it now selects between two live transports; update its comment from "Falls back to the default URL/WAV path" to "Falls back to one-WAV-per-sentence binary frames".

- [ ] **Step 9: Verify the static UI edit by reading it back**

Read `static/js/conversation.js` and confirm: `convEnqueueAudio` is gone, `convEnqueueAudioBytes` exists, no `audio_chunk` case remains, `binaryType` is unconditional, and no smart quotes were introduced.

- [ ] **Step 10: Commit**

```bash
git add apps/api_gateway/app/services/conversation/session.py \
        apps/api_gateway/app/api/routes/conversation.py \
        apps/api_gateway/app/static/js/conversation.js tests/
git commit -m "feat(conversation): push reply audio as binary WAV frames, drop audio_out=url"
```

---

### Task 2: Livehost WebSocket — same WAV downlink

**Files:**
- Modify: `apps/api_gateway/app/api/routes/livehost.py` (`:205`, `:229-235`, `:308-309`, `:383-398`, `:448-452`)
- Modify: `apps/api_gateway/app/static/js/livehost.js` (`:64-86`, `:283`, `:302-305`, `:361-365`)
- Test: `tests/integration/test_livehost_ws_voice.py`, `test_livehost_ws_social.py`, `test_livehost_disabled_cutoff.py`, `tests/unit/livehost/test_livehost_tts_profile.py`

**Interfaces:**
- Consumes: the wire format from Global Constraints and `TTSProvider.render_audio` — identical to Task 1.
- Produces: nothing new; livehost is a leaf.

- [ ] **Step 1: Write the failing test**

In `tests/integration/test_livehost_ws_voice.py`, add a case modelled on `test_livehost_voice_turn_end_to_end` (`:84`) — same `_register_stub` / `_set_default_tts` fixtures, same `TestClient` websocket drive. Collect JSON events and binary frames separately (`ws.receive()` returns `{"text": ...}` or `{"bytes": ...}`), then:

```python
def test_livehost_wav_downlink_pushes_binary_frame(_register_stub, monkeypatch, tmp_path):
    events, frames = _drive_voice_turn(monkeypatch, tmp_path, audio_out="wav")
    assert not [p for n, p in events if n == "audio_chunk"]
    starts = [p for n, p in events if n == "audio_start"]
    assert starts and starts[0]["codec"] == "wav"
    assert frames and frames[0][:4] == b"RIFF"
```

`_drive_voice_turn` does not exist yet — extract it from the body of `test_livehost_voice_turn_end_to_end` as a module-local helper taking `audio_out`, and have that existing test call it too, so both share one drive path.

Make `_StubTTS` (`:34`) return real WAV bytes from `render_audio` (use `pcm16_to_wav_bytes` from `app.core.audio`, as `tests/unit/test_every_paid_entry_point_meters.py:42` does) rather than a placeholder — the assertion checks the `RIFF` header.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_livehost_ws_voice.py::test_livehost_wav_downlink_pushes_binary_frame -v`
Expected: FAIL — `audio_chunk` with `audio_url` is emitted, no binary frame.

- [ ] **Step 3: Convert the livehost synth path**

`livehost.py:383-398` becomes:

```python
                try:
                    audio, media_type = await tts_provider.render_audio(request)
                    try:
                        await record_usage(
                            user_id=identity.user_id or "", profile_id=profile_name or "",
                            kind="tts", engine=tts_engine, model_id=tts_model or "",
                            unit="chars", native_amount=len(sentence or ""),
                        )
                    except Exception as exc:  # noqa: BLE001 - metering must never break the turn
                        logger.warning("livehost tts usage metering failed: %s", exc)
                    if opus_encoder is not None:
                        pcm = await asyncio.to_thread(wav_bytes_to_pcm16, audio, output_sample_rate)
                        packets = await asyncio.to_thread(opus_encoder.encode_pcm16, pcm)
                        return None, packets, None
                    return (audio, media_type), None, None
```

Replace the `audio_chunk` emit at `:448-452` with:

```python
                    else:
                        audio_bytes, media_type = result
                        await send(
                            "audio_start", turn=turn, chunk_index=index,
                            text=sentence if want_text else None,
                            codec="mp3" if media_type == "audio/mpeg" else "wav",
                        )
                        await websocket.send_bytes(audio_bytes)
                        await send("audio_end", turn=turn, chunk_index=index)
```

At `:205` default `audio_out` to `"wav"` and normalize anything that is not `"opus"` to `"wav"` (same two lines as `conversation.py`). At `:235` the no-libopus downgrade becomes `audio_out = "wav"`, and at `:309` the sample-rate advertisement condition becomes `audio_out == "opus"`. Swap the `wav_file_to_pcm16` import for `wav_bytes_to_pcm16`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_livehost_ws_voice.py -v`
Expected: PASS.

- [ ] **Step 5: Migrate the remaining livehost stubs**

Convert `synthesize` → `render_audio` (returning `(wav_bytes, "audio/wav")`) in `tests/integration/test_livehost_ws_social.py`, `test_livehost_disabled_cutoff.py`, `tests/unit/livehost/test_livehost_tts_profile.py`, and update any `audio_url` assertion to the `audio_start` shape.

Run: `pytest tests/integration/test_livehost_ws_social.py tests/integration/test_livehost_disabled_cutoff.py tests/unit/livehost/test_livehost_tts_profile.py -v`
Expected: PASS.

- [ ] **Step 6: Update `static/js/livehost.js`**

Delete `lhEnqueueAudio(url)` (`:64-86`) and add:

```js
function lhEnqueueAudioBytes(bytes) {
  lh.chain = (lh.chain || Promise.resolve())
    .then(async () => {
      const ctx = lhAudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      const buf = await ctx.decodeAudioData(bytes);
      lhScheduleBuffer(buf);
    })
    .catch((e) => lhLog("audio error: " + e));
}
```

Make `ws.binaryType = "arraybuffer"` unconditional (`:283`); route binary frames by `lh.outCodec` exactly as Task 1 does for `conv.outCodec` (`:302-305`); set `lh.outCodec = d.codec || "wav"` in the `audio_start` case (`:361`); delete the `audio_chunk` case (`:365`).

- [ ] **Step 7: Verify by reading the file back**

Read `static/js/livehost.js` and confirm `lhEnqueueAudio` is gone, `lhEnqueueAudioBytes` exists, no `audio_chunk` case remains, and no smart quotes were introduced.

- [ ] **Step 8: Commit**

```bash
git add apps/api_gateway/app/api/routes/livehost.py \
        apps/api_gateway/app/static/js/livehost.js tests/
git commit -m "feat(livehost): push reply audio as binary WAV frames, drop audio_out=url"
```

---

### Task 3: Delete `POST /v1/tts/stream` and its now-dead SSE job channel

**Files:**
- Modify: `apps/api_gateway/app/api/routes/tts.py` (delete `:33-54` job bookkeeping, `:169-291` the route, and the `uuid` / `StreamEvent` / `segment_text` / `event_bus` imports)
- Modify: `apps/api_gateway/app/api/routes/events.py:29-42` (delete `GET /jobs/{job_id}`)
- Modify: `apps/api_gateway/app/static/index.html:547-562`
- Delete: `apps/api_gateway/app/static/js/tts-stream.js`
- Delete: `tests/unit/tts/test_tts_stream_route.py`, `tests/unit/tts/test_tts_stream_metering.py`, `tests/integration/test_tts_stream.py`
- Modify: `tests/unit/test_paid_call_site_inventory.py:99`, `tests/unit/test_every_paid_entry_point_meters.py:151`, `tests/unit/test_static_quota_messages.py:16`, `tests/unit/http/test_event_bus.py:69`

**Interfaces:**
- Consumes: nothing from Tasks 1–2.
- Produces: `apps/api_gateway/app/api/routes/tts.py` no longer exports `get_job_owner`; `events.py` keeps only `GET /v1/events/sessions/{session_id}`.

**Why the SSE job endpoint goes too:** `GET /v1/events/jobs/{job_id}` exists solely to subscribe to a TTS stream job — `/v1/stt/stream` publishes to `session:` channels (`api/routes/stt.py:215`), never `job:`. With the producer gone, the endpoint, `_job_owners`, `get_job_owner`, `_record_job_owner` and `_stream_jobs` are unreachable code. `segment_text` stays: `services/conversation/responder.py:149` still uses it.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/tts/test_stream_job_removed.py`. There is no shared `client` fixture in this repo — build a `TestClient` the way `tests/unit/test_every_paid_entry_point_meters.py:39-42,127` does:

```python
"""POST /v1/tts/stream and its SSE job channel are gone -- synthesized audio is
never persisted, so there is nothing for a URL-emitting job to hand back. See
docs/superpowers/specs/2026-07-29-drop-audio-artifacts-design.md."""
from fastapi.testclient import TestClient

from app.main import app


def test_stream_job_endpoints_are_gone():
    client = TestClient(app)
    assert client.post("/v1/tts/stream", json={"text": "xin chao"}).status_code == 404
    assert client.get("/v1/events/jobs/anything").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/tts/test_stream_job_removed.py -v`
Expected: FAIL — the POST returns 200 with a `job_id`, the GET returns a 200 SSE stream (or 404 only by ownership accident).

- [ ] **Step 3: Delete the route and the job channel**

Remove from `api/routes/tts.py`: the `_stream_jobs` set, `_job_owners`, `_JOB_OWNERS_LIMIT`, `_record_job_owner`, `get_job_owner` (`:33-54`) and the whole `create_stream_job` function (`:169-291`). Then drop the imports that become unused — `asyncio`, `uuid`, `StreamEvent`, `segment_text`, `event_bus` — keeping `time`, `wav_duration_seconds`, `artifact_store` and the rest that `/synthesize` and `/reference-audio` still need.

Remove `stream_job_events` from `api/routes/events.py:29-42` along with its `from app.api.routes.tts import get_job_owner`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/tts/test_stream_job_removed.py -v`
Expected: PASS.

- [ ] **Step 5: Remove the admin panel**

Delete `apps/api_gateway/app/static/js/tts-stream.js`, its `<section>` in `static/index.html:547-562`, the `import`/wiring of that module in whichever entry module references it (grep `tts-stream` under `static/`), and any nav entry pointing at the panel. Read `index.html` back afterwards to confirm no orphaned markup or dangling ids remain.

- [ ] **Step 6: Update the anti-omission harnesses**

Delete `tests/unit/tts/test_tts_stream_route.py`, `tests/unit/tts/test_tts_stream_metering.py`, `tests/integration/test_tts_stream.py`.

In `tests/unit/test_paid_call_site_inventory.py:99` remove the `/v1/tts/stream` inventory entry (the surrounding comment explains `POST /v1/tts/synthesize` already moved to bytes — keep that note accurate). In `tests/unit/test_every_paid_entry_point_meters.py:151` delete the `/v1/tts/stream` case. In `tests/unit/test_static_quota_messages.py:16` and `tests/unit/http/test_event_bus.py:69` update the prose/ids that name the removed endpoint.

Run: `pytest tests/unit/test_paid_call_site_inventory.py tests/unit/test_every_paid_entry_point_meters.py tests/unit/test_static_quota_messages.py tests/unit/http/test_event_bus.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A apps/api_gateway/app/api/routes/tts.py apps/api_gateway/app/api/routes/events.py \
           apps/api_gateway/app/static tests/
git commit -m "refactor(tts): delete /v1/tts/stream and its dead SSE job channel"
```

---

### Task 4: Collapse the provider seam — delete `synthesize()`, `TTSResult`, and the audio-writing store methods

**Files:**
- Modify: `apps/api_gateway/app/services/tts/base.py:20-22`, `:87-97`
- Modify: `apps/api_gateway/app/services/tts/providers/edge_tts_provider.py:13`, `:106-116`
- Modify: `apps/api_gateway/app/schemas/tts.py:66-73`
- Modify: `apps/api_gateway/app/services/artifacts.py` (delete `save_wav`, `save_mp3`, `prune`, `prune_loop`, `_ARTIFACT_FILENAME`)
- Modify: `apps/api_gateway/app/core/audio.py:123-126` (delete `wav_file_to_pcm16`)
- Test: `tests/unit/artifacts/test_no_audio_persistence.py` (create)
- Test: `tests/unit/artifacts/test_artifacts.py` (rewrite — it currently tests `save_wav` / `prune` / `prune_loop`)
- Test: stub migration in the 8 files listed in Step 5

**Interfaces:**
- Consumes: every caller of `synthesize()` was already migrated in Tasks 1–3; this task removes the method itself.
- Produces: `TTSProvider.render_audio` is the only abstract method producing audio. `ArtifactStore` exposes exactly `save_reference_audio`, `contains`, `path_for`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/artifacts/test_no_audio_persistence.py`:

```python
"""Structural guarantee: nothing in the gateway can persist synthesized audio.

This is not a behavior test -- it is the guard that keeps the artifact-writing
mechanism from creeping back in. See
docs/superpowers/specs/2026-07-29-drop-audio-artifacts-design.md.
"""
from app.services.artifacts import ArtifactStore, artifact_store
from app.services.tts.base import TTSProvider


def test_artifact_store_cannot_write_generated_audio():
    for gone in ("save_wav", "save_mp3", "prune"):
        assert not hasattr(ArtifactStore, gone), f"{gone} must not exist"


def test_reference_audio_api_survives():
    for kept in ("save_reference_audio", "contains", "path_for"):
        assert hasattr(artifact_store, kept)


def test_render_audio_is_the_only_audio_seam():
    assert not hasattr(TTSProvider, "synthesize")
    assert "render_audio" in TTSProvider.__abstractmethods__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/artifacts/test_no_audio_persistence.py -v`
Expected: FAIL on all three — `save_wav` exists, `synthesize` exists, `render_audio` is not abstract.

- [ ] **Step 3: Delete the synthesize seam**

`services/tts/base.py`: delete `TTSProvider.synthesize` (`:20-22`) and mark `render_audio` `@abstractmethod` (keep its docstring, drop the `raise ProviderError(...)` body in favour of `raise NotImplementedError`). Delete `RenderingTTSProvider.synthesize` (`:87-97`). Drop the `TTSResult`, `artifact_store` and `wav_duration_seconds` imports that become unused.

`edge_tts_provider.py`: delete `synthesize` (`:106-116`), the `TTSResult` import, and the `artifact_store` import. `render_audio` (`:102-104`) already carries the whole contract.

`schemas/tts.py`: delete `class TTSResult` (`:66-73`). Leave `TTSRequest` and its `ref_audio_path` validator — and the deliberate `artifact_store` import above it — exactly as they are.

`services/artifacts.py`: delete `save_wav`, `save_mp3`, `prune`, `prune_loop`, `_ARTIFACT_FILENAME`, and the `asyncio` / `re` / `time` imports they used. Rewrite the module docstring to describe the narrowed role:

```python
"""Local filesystem store for voice-clone reference audio.

Synthesized audio is never persisted -- TTS providers return bytes
(`render_audio`) that go straight out over the request or socket. What lives
here is user-uploaded reference audio for voice cloning, plus OmniVoice's
pinned voice reference, and it is never served over HTTP.
"""
```

`core/audio.py`: delete `wav_file_to_pcm16` (`:123-126`) and trim the `wav_bytes_to_pcm16` docstring sentence at `:95-98` that refers to it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/artifacts/test_no_audio_persistence.py -v`
Expected: PASS.

- [ ] **Step 5: Migrate the remaining test stubs**

These still define `async def synthesize` and will now fail to instantiate (unimplemented abstract method). Convert each to `async def render_audio(self, payload) -> tuple[bytes, str]: return <wav bytes>, "audio/wav"`:

`tests/unit/model_registry/test_model_registry_routes.py`, `tests/unit/quota/test_quota_enforcement_core.py`, `tests/unit/quota/test_quota_enforcement_routes.py`, `tests/unit/usage/test_routes_usage_metering.py`, `tests/unit/test_warmup.py`, `tests/unit/tts/test_tts_voices_route.py`, `tests/unit/tts/test_tts_render_seam.py`, `tests/unit/tts/test_synthesize_returns_bytes.py`.

The last two are about the bytes seam itself: drop their assertions that `synthesize()` still writes an artifact (`test_synthesize_returns_bytes.py:138` names `/v1/tts/stream`, which no longer exists) and keep the ones asserting `/v1/tts/synthesize` returns bytes with the `X-TTS-*` headers.

Run each file as you convert it. Expected: PASS.

- [ ] **Step 5b: Prune the artifact-store test file**

`tests/unit/artifacts/test_artifacts.py` tests the machinery this task deleted. Remove `test_prune_removes_only_files_older_than_max_age`, `test_prune_leaves_non_artifact_files_alone`, `test_prune_ignores_subdirectories`, `test_prune_loop_prunes_periodically_and_sleeps_first`, the `prune_loop` import, and the `asyncio` / `os` / `time` / `_age_file` helpers they used.

Keep every `save_reference_audio` / `contains` / `path_for` test unchanged — that is the surface this whole change is protecting.

The intent behind `test_prune_leaves_non_artifact_files_alone` (OmniVoice's pinned `_omnivoice_voice_ref.wav` must never be deleted) does not disappear; it moves to Task 6's sweep test, which asserts the same protection for the only remaining deleter in the system.

Run: `pytest tests/unit/artifacts/test_artifacts.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/tts/ apps/api_gateway/app/schemas/tts.py \
        apps/api_gateway/app/services/artifacts.py apps/api_gateway/app/core/audio.py tests/
git commit -m "refactor(tts): render_audio is the only audio seam; drop synthesize/TTSResult/save_wav"
```

---

### Task 5: Remove the `/artifacts` HTTP mount and the prune janitor

**Files:**
- Modify: `apps/api_gateway/app/main.py:181-189`, `:313-314`, and the `prune_loop` import at `:186`
- Modify: `apps/api_gateway/app/core/auth_guard.py:45-52`
- Modify: `apps/api_gateway/app/core/settings.py` (delete `artifacts_ttl_hours`)
- Test: `tests/unit/artifacts/test_no_audio_persistence.py` (extend), plus the route-classifier test under `tests/unit/` that enumerates auth prefixes

**Interfaces:**
- Consumes: `ArtifactStore` without `prune`/`prune_loop` (Task 4).
- Produces: no HTTP surface serves the artifacts directory; `artifact_store.base_dir` remains reachable in-process for reference audio.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/artifacts/test_no_audio_persistence.py`:

```python
from fastapi.testclient import TestClient

from app.main import app


def test_artifacts_are_not_served_over_http():
    name = "deadbeef" * 4 + ".wav"
    path = artifact_store.base_dir / name
    path.write_bytes(b"RIFFfake")
    try:
        assert TestClient(app).get(f"/artifacts/{name}").status_code == 404
    finally:
        path.unlink(missing_ok=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/artifacts/test_no_audio_persistence.py::test_artifacts_are_not_served_over_http -v`
Expected: FAIL — the StaticFiles mount serves the file (200).

- [ ] **Step 3: Remove the mount, the guard entry, and the janitor together**

`main.py`: delete the `app.mount("/artifacts", ...)` line and its comment (`:313-314`); delete the janitor block at `:181-189` including the local `from app.services.artifacts import prune_loop` import; drop the now-unused `artifact_store` import at `:41` **only if** nothing else in the module uses it (grep first — `_artifacts_stats` lives in `routes/system.py`, not here).

`core/auth_guard.py`: delete the `"/artifacts"` entry and its comment (`:49-52`). Both edits must land in the same commit: the guard is default-deny, so a mount without a classification 401s and a classification without a mount leaves a phantom prefix the classifier test flags.

`core/settings.py`: delete `artifacts_ttl_hours`. Keep `artifacts_dir` / `artifacts_dir_resolved`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/artifacts/test_no_audio_persistence.py -v`
Expected: PASS.

Then run the auth route-classifier test (find it with `grep -rln "_USER_PREFIXES\|route classif" tests/`) — it must still pass, proving no route is left unclassified.

- [ ] **Step 5: Confirm reference audio still works end to end**

Run: `pytest tests/unit/tts/ tests/unit/conversation/test_session_bad_ref_audio_path_degrades.py -v`
Expected: PASS — `POST /v1/tts/reference-audio` and the `ref_audio_path` containment validator are unaffected by the mount removal.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/main.py apps/api_gateway/app/core/auth_guard.py \
        apps/api_gateway/app/core/settings.py tests/
git commit -m "refactor(artifacts): stop serving /artifacts over HTTP and drop the prune janitor"
```

---

### Task 6: Replace the janitor safety net with a model_service startup sweep

**Files:**
- Modify: `apps/model_service/app/main.py` (add the sweep in `create_app`)
- Modify: `apps/model_service/app/routes_tts.py:81-98` (update the comment that cites `prune()`)
- Test: `tests/unit/model_service/test_ref_audio_sweep.py` (create; place it beside the existing model_service tests — `grep -rl model_service tests/` for the directory this repo uses)

**Interfaces:**
- Consumes: `artifact_store.base_dir` (`app.services.artifacts`), shared with the gateway package.
- Produces: `sweep_stale_ref_audio(base_dir: Path) -> int` in `apps/model_service/app/main.py`, returning how many files it deleted.

**Why:** `routes_tts.py:98` writes `<uuid4-hex>.wav` into the artifacts directory (it must live there to satisfy the `ref_audio_path` containment check) and unlinks it in a `finally`. Its comment relies on `ArtifactStore.prune()` to mop up if the process dies in between; Task 4 deleted `prune`. A boot-time sweep is the replacement — a bare-hex `.wav` present at startup can only be leftovers, since a live request holds its file for the duration of one call.

- [ ] **Step 1: Write the failing test**

```python
import re
from app.services.artifacts import artifact_store
from model_service.app.main import sweep_stale_ref_audio


def test_sweep_removes_leftover_tmp_refs_but_keeps_reference_audio():
    stale = artifact_store.base_dir / ("a1" * 16 + ".wav")
    # Inherited from the deleted prune() tests: OmniVoice's pinned voice
    # reference and user-uploaded clips must survive every sweep -- deleting
    # the pinned file silently changes the cloned voice.
    keepers = [
        artifact_store.base_dir / "ref_deadbeef.wav",
        artifact_store.base_dir / "_omnivoice_voice_ref.wav",
        artifact_store.base_dir / "notes.txt",
    ]
    stale.write_bytes(b"RIFF")
    for keep in keepers:
        keep.write_bytes(b"RIFF")
    try:
        removed = sweep_stale_ref_audio(artifact_store.base_dir)
        assert removed >= 1
        assert not stale.exists()
        for keep in keepers:
            assert keep.exists()
    finally:
        stale.unlink(missing_ok=True)
        for keep in keepers:
            keep.unlink(missing_ok=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/model_service/test_ref_audio_sweep.py -v`
Expected: FAIL with `ImportError: cannot import name 'sweep_stale_ref_audio'`.

- [ ] **Step 3: Implement the sweep**

In `apps/model_service/app/main.py`:

```python
_TMP_REF_FILENAME = re.compile(r"^[0-9a-f]{32}\.wav$")


def sweep_stale_ref_audio(base_dir: Path) -> int:
    """Delete temp reference clips left behind by a previous run.

    routes_tts.py writes `<uuid4 hex>.wav` into the artifacts dir (it has to
    live there to pass TTSRequest's ref_audio_path containment check) and
    unlinks it in a finally. A crash in between used to be mopped up by the
    gateway's artifact janitor, which no longer exists -- synthesized audio is
    never persisted, so there was nothing else left for it to prune. At
    startup any such file can only be leftovers: a live request holds its own
    file for the duration of a single call. `ref_*.wav` never matches this
    pattern and is never touched.
    """
    if not base_dir.is_dir():
        return 0
    removed = 0
    for path in base_dir.iterdir():
        if not _TMP_REF_FILENAME.match(path.name):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
```

Call it inside `create_app` (`main.py:43`), after the app object exists, logging the count when non-zero. Add the `re` and `pathlib.Path` imports.

Then update the comment block at `routes_tts.py:81-98`: it currently argues the uuid-hex name is chosen so `prune()` will sweep it and that the file is "briefly fetchable at `/artifacts/<name>.wav`". Both premises are gone — the sweep is now the backstop, and nothing serves that directory over HTTP.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/model_service/test_ref_audio_sweep.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/model_service/app/main.py apps/model_service/app/routes_tts.py tests/
git commit -m "fix(model-service): sweep stale temp reference clips at startup"
```

---

### Task 7: Fix the web client's read-aloud (blob instead of `audio_url`)

**Files:**
- Modify: `lugo-web-client/src/api/tools.ts:26-47`
- Modify: `lugo-web-client/src/api/tools.test.ts:55-80`
- Modify: `lugo-web-client/src/screens/Tools.tsx:59-100`

**Interfaces:**
- Consumes: `POST /v1/tts/synthesize` returning raw audio bytes with `Content-Type: audio/wav` or `audio/mpeg` (already true since the TTS bytes refactor — this task fixes a client that never caught up).
- Produces: `synthesize(text) -> { audioUrl: string }` where `audioUrl` is an object URL the caller must revoke.

**Note:** this is an independent bug fix — the Tools screen's read-aloud is broken on `main` today, because `tools.ts` calls `resp.json()` on an `audio/wav` response. It can run in parallel with Tasks 1–6.

- [ ] **Step 1: Write the failing test**

In `lugo-web-client/src/api/tools.test.ts`, replace the two JSON-shaped `synthesize` cases with:

```ts
it('turns the audio response into an object URL', async () => {
  const blob = new Blob([new Uint8Array([0x52, 0x49, 0x46, 0x46])], { type: 'audio/wav' })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(blob, {
    status: 200, headers: { 'Content-Type': 'audio/wav' },
  })))
  const createObjectURL = vi.fn().mockReturnValue('blob:fake')
  vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL: vi.fn() })

  const r = await synthesize('xin chao')

  expect(createObjectURL).toHaveBeenCalledOnce()
  expect(r.audioUrl).toBe('blob:fake')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && npx vitest run src/api/tools.test.ts`
Expected: FAIL — `synthesize` calls `resp.json()` and throws on a binary body.

- [ ] **Step 3: Implement the blob path**

```ts
export async function synthesize(text: string): Promise<{ audioUrl: string }> {
  const resp = await apiFetch('/v1/tts/synthesize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  if (!resp.ok) {
    throw await viError(resp, 'Could not read this out. Try again in a moment.')
  }
  // The endpoint returns raw audio bytes, not JSON: an object URL keeps the
  // audio in this tab's memory and never round-trips through the server.
  return { audioUrl: URL.createObjectURL(await resp.blob()) }
}
```

`durationSeconds` disappears from the return type — it came from the old JSON body, and `Tools.tsx` never rendered it. In `Tools.tsx`, revoke the previous object URL before replacing it and on unmount:

```tsx
  async function run() {
    setBusy(true); setError(null)
    setUrl((prev) => { if (prev) URL.revokeObjectURL(prev); return null })
    try {
      const r = await synthesize(input.trim())
      setUrl(r.audioUrl)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not read this out')
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => () => { if (url) URL.revokeObjectURL(url) }, [url])
```

Replace the stale comment above the `<audio>` element (`Tools.tsx:92-94`) — the URL is now a local blob, not a cross-domain API path.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && npx vitest run src/api/tools.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lugo-web-client/src/api/tools.ts lugo-web-client/src/api/tools.test.ts \
        lugo-web-client/src/screens/Tools.tsx
git commit -m "fix(web-client): read /v1/tts/synthesize as bytes, not JSON"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md:104`, `:109`, `:152-153`
- Modify: `docs/api.md:426-441`
- Modify: `docs/architecture.md:74-77`
- Modify: `docs/device-integration.md` (grep `audio_url`)
- Modify: `rpi-assistant/integration.md` (grep `audio_url`)

**Interfaces:**
- Consumes: the final wire format and route list produced by Tasks 1–5.

- [ ] **Step 1: Update the endpoint lists**

Remove `POST /v1/tts/stream` and `GET /v1/events/jobs/{job_id}` from `README.md:104,109` and the numbered walkthrough at `:152-153`. In `docs/api.md`, delete the `/v1/tts/stream` and `/v1/events/jobs/{job_id}` sections (`:431-441`) and rewrite the note at `:426` — it currently says `/v1/tts/stream` "is unchanged and still returns URLs", which becomes false.

- [ ] **Step 2: Document the WAV transport**

In `docs/api.md`'s conversation-socket section and `docs/architecture.md:74-77`, replace the artifact-URL flow with the wire format from Global Constraints, and state that `audio_out` takes `"wav"` (default) or `"opus"` and that no synthesized audio is persisted. Do the same wherever `docs/device-integration.md` and `rpi-assistant/integration.md` mention `audio_url` — note that devices are unaffected because they always negotiate Opus.

- [ ] **Step 3: Verify no stale references remain**

Run: `grep -rn "audio_url\|tts/stream\|events/jobs" README.md docs/*.md rpi-assistant/integration.md`
Expected: matches only inside `docs/superpowers/` history (plans and specs are a record of what was decided at the time and are not rewritten).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/api.md docs/architecture.md docs/device-integration.md rpi-assistant/integration.md
git commit -m "docs: describe the binary WAV downlink, drop audio_url and the SSE job endpoints"
```

---

### Task 9: Full-suite verification and merge

**Files:** none modified unless a failure demands it.

- [ ] **Step 1: Run the whole Python suite once**

Run: `pytest -q`
Expected: all tests pass. This is the only full-suite run in the plan — do not start a second one concurrently.

- [ ] **Step 2: Run the web-client suite**

Run: `cd lugo-web-client && npx vitest run`
Expected: PASS.

- [ ] **Step 3: Confirm the goal holds structurally**

Run: `grep -rn "save_wav\|save_mp3\|audio_url\|wav_file_to_pcm16" apps/ --include='*.py' --include='*.js'`
Expected: no matches.

- [ ] **Step 4: Exercise the real app**

Start the gateway, open the admin chat UI, and confirm a spoken reply plays with the Opus checkbox OFF (the new WAV path) and ON (the unchanged Opus path). Confirm the browser devtools Network tab shows no `/artifacts/*` request, and that the artifacts directory gains no new `.wav` file during the conversation.

- [ ] **Step 5: Merge**

Use superpowers:finishing-a-development-branch to merge `feat/drop-audio-artifacts` into local `main`. Do not push — pushing `main` deploys production and is the user's call.
