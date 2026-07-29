# Drop server-side audio artifacts — design

## Goal

The gateway must never persist synthesized audio. Today every TTS sentence is
written to `artifacts/<uuid>.wav` and handed to the client as an `audio_url`;
this design removes that mechanism entirely and replaces it with binary audio
frames pushed over the already-open WebSocket.

Voice-clone **reference audio** (user-uploaded input, `save_reference_audio` /
`ref_audio_path`) is explicitly **out of scope and stays** — deleting it would
remove the voice-cloning feature.

## Current state

Producers of `audio_url`:

| Site | What it does |
|---|---|
| `services/tts/base.py:87-97` (`RenderingTTSProvider.synthesize`) | render WAV → `artifact_store.save_wav` → URL |
| `services/tts/providers/edge_tts_provider.py:106-116` | render MP3 → `artifact_store.save_mp3` → URL |

Consumers:

| Site | What it does |
|---|---|
| `services/conversation/session.py:716-719`, `:931-933` | emits `audio_chunk{audio_url}` when `audio_out == "url"` |
| `api/routes/livehost.py:449-452` | same, for the livehost socket |
| `api/routes/tts.py:236-250` | SSE job `/v1/tts/stream` publishes `audio_url` per segment |
| `services/conversation/session.py:630-632`, `:908`; `api/routes/livehost.py:394-396` | Opus fallback for non-`RenderingTTSProvider` engines: reads the artifact **back off disk** to make PCM |
| `main.py:314` | `StaticFiles` mount serving `/artifacts` (classified user-auth in `core/auth_guard.py:52`) |
| `main.py:181-189` | hourly `prune_loop` janitor, TTL `settings.artifacts_ttl_hours` |
| `static/js/conversation.js:414`, `livehost.js:365`, `tts-stream.js:78-119` | browser fetches the URL |

Two facts that shape the design:

1. **`audio_out=url` is the default** for `/v1/conversation/stream`
   (`api/routes/conversation.py:372`). Removing it changes the default path, not
   a side branch. Devices (`api/routes/lugo.py:158`) and `lugo-web-client`
   (`src/audio/conversation.ts:16`) both pin `audio_out=opus` and are unaffected.
2. **`POST /v1/tts/synthesize` already returns raw bytes**
   (`api/routes/tts.py:142-166`) via `render_audio()`. The bytes seam exists; this
   design makes it the only seam.

## Design

### 1. Provider API: `render_audio()` becomes the single seam

- `TTSProvider.render_audio(payload) -> (bytes, media_type)` becomes the sole
  abstract method for producing audio.
- Delete `TTSProvider.synthesize()` and both implementations
  (`RenderingTTSProvider.synthesize`, `EdgeTTSProvider.synthesize`).
- Delete the `TTSResult` model (`schemas/tts.py:66-73`). `TTSRequest` stays
  untouched, including its `ref_audio_path` containment validator.
- `RenderingTTSProvider` keeps `render_wav()` as the subclass hook; its
  `render_audio()` returns `(wav, "audio/wav")` as it already does.

After this, no code path in `apps/` can write generated audio to disk — the
guarantee is structural, not a convention.

### 2. Artifact store narrows to reference audio

- Delete `save_wav`, `save_mp3`, `prune`, `prune_loop`, and
  `settings.artifacts_ttl_hours`.
- Keep `save_reference_audio`, `contains`, `path_for`.
- **Keep the names `artifact_store` / `ARTIFACTS_DIR` and the directory path.**
  Persisted `TtsProfile.ref_audio_path` rows hold absolute paths into that
  directory; renaming would break existing data. Only the module docstring
  changes to describe its narrowed role.
- Delete `core/audio.py:wav_file_to_pcm16` (no callers remain).
  `wav_bytes_to_pcm16` stays and already handles MP3 via its `soundfile`
  fallback (`core/audio.py:110-112`), which is what keeps edge_tts working on
  the Opus path with no file.

### 3. Remove the HTTP artifact mount

Delete the `StaticFiles` mount (`main.py:314`) **and** the `"/artifacts"` entry
in `core/auth_guard._USER_PREFIXES` (`core/auth_guard.py:52`) in the same
change. The guard is default-deny, so removing only one side either 401s a live
route or leaves an unclassified prefix (the route-classifier test fails).

Nothing fetches reference audio over HTTP — `POST /v1/tts/reference-audio`
returns a filesystem path (`api/routes/tts.py:107`), and `tts-profiles.js:213`
stores that path. Removing the mount also closes a standing exposure: any
logged-in user who obtained an artifact id could read another user's audio.

### 4. Replace the janitor with a startup sweep in model_service

`model_service/routes_tts.py:98` writes a temporary reference file named
`<uuid4-hex>.wav` into the artifacts directory (it must live there to pass the
`ref_audio_path` containment check) and unlinks it in a `finally`. Its comment
relies on `prune()` as the crash-safety net.

Replace that net with a **sweep at model_service startup**: delete any
`<32-hex>.wav` in the artifacts directory. Such a file can only be leftover
from a previous run — the live request holds its own file for the duration of
one call. `ref_*.wav` files never match the pattern and are never touched. No
background task, no TTL setting.

### 5. Wire protocol

`audio_out` accepts `"wav"` (default) and `"opus"`. `"url"` is removed;
unrecognized values fall back to `"wav"`.

Per sentence, over the existing socket:

```
audio_start { turn, chunk_index, text?, codec: "wav" | "mp3" }
<binary frame: the complete file, one frame per sentence>
audio_end   { turn, chunk_index }
```

- The `audio_chunk` event is deleted; it existed only to carry `audio_url`.
- One frame per sentence, never split: `decodeAudioData()` needs a complete
  container. `codec` is derived from the provider's `media_type`:
  `audio/wav` → `"wav"`, `audio/mpeg` → `"mp3"`.
- `session_started.output_sample_rate` keeps its current behavior — meaningful
  for Opus, `null` otherwise (`session.py:228`). A WAV/MP3 client reads the rate
  from the container header.
- Frame size: a 5s sentence at 24 kHz mono 16-bit is ~240 KB — the same bytes
  the browser fetches over HTTP today, on a different path.
- `/v1/livehost/*` gets the identical treatment. `/v1/lugo` (ESP32/RPi) is
  unchanged: always Opus.

### 6. Delete `POST /v1/tts/stream`

The SSE job exists only for the admin test panel; no device or web client calls
it, and SSE cannot carry binary. Delete the route, its job-owner bookkeeping,
the `index.html:547-562` panel, and `static/js/tts-stream.js`.
`POST /v1/tts/synthesize` (bytes) covers the same "try this engine" need.

`GET /v1/events/jobs/{job_id}` goes with it. That endpoint exists solely to
subscribe to a TTS stream job — `/v1/stt/stream` publishes to `session:`
channels (`api/routes/stt.py:215`), never `job:` — so with the producer gone,
it, `_job_owners`, `get_job_owner`, `_record_job_owner` and `_stream_jobs` are
unreachable. `GET /v1/events/sessions/{session_id}` is unaffected.
`segment_text` also stays: `services/conversation/responder.py:149` uses it.

This is a deliberate removal of a metered, quota-gated call site, so the
anti-omission harnesses must be updated with it, not left to fail:
`tests/unit/test_paid_call_site_inventory.py`,
`tests/unit/test_every_paid_entry_point_meters.py`.

### 7. Clients

**`static/js/conversation.js`** — set `ws.binaryType = "arraybuffer"`
unconditionally (today only in opus mode, `:339`); record `codec` from
`audio_start`; route binary frames by codec — `opus` → `convFeedOpus()`,
`wav`/`mp3` → new `convEnqueueAudioBytes(buf)` which does `decodeAudioData` then
reuses the existing gapless `convScheduleBuffer()`. Delete
`convEnqueueAudio(url)` (`:47-66`). The `conv-opus` checkbox becomes a choice
between two live transports rather than "streaming vs URL".

**`static/js/livehost.js`** — same change for `lhEnqueueAudio`.

**`lugo-web-client/src/api/tools.ts:26-47`** — switch to `resp.blob()` +
`URL.createObjectURL`, revoking the previous object URL on replacement. This
also **fixes a live bug**: the client currently calls `resp.json()` against an
`audio/wav` response, so the Tools screen's read-aloud is broken today.

### 8. Error handling

No new failure modes; one is removed (write-then-read-back of an artifact).
Provider failures still raise `ProviderError`, still surface as a single
`tts_error` event per turn, text still streams, `turn_done` still fires.
Metering and quota call sites keep their current positions.

## Testing

29 test files reference `audio_url`. Most are stub providers of the shape
`async def synthesize(...) -> TTSResult(audio_url=...)`; they convert
mechanically to `async def render_audio(...) -> (wav_bytes, "audio/wav")`.

Delete: `tests/unit/tts/test_tts_stream_route.py`,
`tests/unit/tts/test_tts_stream_metering.py`,
`tests/integration/test_tts_stream.py`.

Rewrite assertions: conversation and livehost WS tests move from
`audio_chunk.audio_url` to `audio_start(codec="wav")` + binary frame +
`audio_end` (notably `tests/unit/conversation/test_session_opus_nodisk.py`,
whose `test_url_mode_...` case becomes the WAV-downlink case).

New tests, one property each:

1. **No audio-writing API exists** — `ArtifactStore` has no `save_wav` /
   `save_mp3` attribute. This is the test that keeps the goal from silently
   regressing.
2. **WAV downlink shape** — one sentence yields exactly one `audio_start`, one
   binary frame starting with `RIFF`, one `audio_end`.
3. **edge_tts (MP3) over Opus** still produces packets via the `soundfile`
   path with no filesystem access.
4. **`/artifacts/<uuid>.wav` returns 404** while `POST /v1/tts/reference-audio`
   and voice cloning still work.
5. **model_service startup sweep** removes leftover `<32-hex>.wav` and leaves
   `ref_*.wav` untouched.
6. **`tools.ts`** uses the blob path.

Docs to update: `README.md:104,152`, `docs/api.md:426-431`,
`docs/architecture.md:74`, `docs/device-integration.md`,
`rpi-assistant/integration.md`.

## Risks

Breaking change for any out-of-repo client using `audio_out=url` or reading the
`audio_chunk` event. In-repo: ESP32/RPi firmware and `lugo-web-client` both use
Opus and are unaffected; only the static admin UI changes. No compatibility
shim is provided — keeping `url` alive would mean keeping `save_wav` alive,
which is exactly what this design removes.
