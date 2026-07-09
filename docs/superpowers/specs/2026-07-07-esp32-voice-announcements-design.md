# ESP32 Voice Status Announcements

## Problem

The esp32-assistant device now shows WiFi/gateway status on an ST7789
display, but has no audio feedback — a user who isn't looking at the screen
has no way to know the device is setting up, connecting, or ready.

## Goals

- Play a short, pre-recorded Vietnamese voice announcement at three points in
  the boot/connection sequence: entering WiFi-setup mode, starting to connect
  to WiFi, and the gateway session becoming ready.
- No on-device TTS (not feasible on ESP32-S3 — no space/CPU budget for a real
  TTS model). Clips are synthesized once, offline, using this project's own
  gateway TTS, and embedded into the firmware as static audio data.

## Non-goals

- No audio announcement for the WS-error state (arbitrary server-provided
  error text can't be pre-recorded).
- No announcement for the transient "WiFi OK, connecting gateway..." state
  (too brief, would be redundant with "connecting"/"connected").
- No configurable voice/language — fixed Vietnamese phrases, matching this
  project's default `AA_LANGUAGE="vi"`.
- No streaming/on-the-fly synthesis on the device.

## Content

Three clips, synthesized via the gateway's `vieneu` TTS engine (voice "Đức
Trí" — clear male voice), at 48kHz mono WAV, then resampled to 16kHz mono
PCM16 (matching the firmware's existing `audio_spk_write()` format exactly —
same rate/format the conversation audio pipeline already uses):

| Clip | Text | Duration | Trigger |
|---|---|---|---|
| `voice_setup` | "Đang cài đặt WiFi. Kết nối vào mạng Lugo." | 3.68s | Entering WiFi provisioning/SoftAP mode |
| `voice_connecting` | "Đang kết nối WiFi." | 2.40s | Starting WiFi connection attempt |
| `voice_connected` | "Đã kết nối. Sẵn sàng." | 2.08s | Gateway session ready |

Clips already generated and converted this session (raw PCM16 files, ~256KB
total, well within the firmware's ~1.9MB free flash): `voice_setup.pcm`,
`voice_connecting.pcm`, `voice_connected.pcm`, to be committed to
`components/voice/assets/`.

**Not yet verified:** audio content quality/correctness (this requires
listening — an on-device or host playback check should confirm the clips
sound right before/during the implementation's manual verification step).

## Architecture

New component `components/voice`:

- Assets embedded directly as binary blobs via ESP-IDF's `EMBED_FILES`
  (`idf_component_register(... EMBED_FILES "assets/voice_setup.pcm" ...)`) —
  no managed component, no hand-written byte arrays; ESP-IDF's build system
  generates `_binary_<name>_start`/`_binary_<name>_end` symbols per file
  automatically.
- `voice_play(voice_clip_t clip)`: blocking playback of one embedded clip,
  writing PCM samples to the existing `audio_spk_write()` in fixed-size
  chunks (matching this project's existing pattern of bounded, chunked
  writes rather than one huge write).

**Concurrency fix in `components/audio`:** `voice_play()` can be called from
contexts that run concurrently with `mic_task`/`spk_task` (specifically: the
"connected" announcement fires from the WS event callback, which can run
around the same time `spk_task` is already active). A small mutex is added
around `audio_mic_read()`/`audio_spk_write()` in the existing `audio.c` to
serialize access to the shared ES8311/I2S resource — the two "boot-time"
announcements (setup, connecting) don't strictly need this since they run
before `mic_task`/`spk_task` exist, but the mutex costs nothing extra and
makes all three call sites definitively safe rather than "probably fine."

## Integration points

| State | Trigger (existing code) | Clip |
|---|---|---|
| Entering provisioning | `provisioning_start()`, right after `display_show("Setup WiFi", ...)` | `VOICE_SETUP` |
| Starting WiFi connect | `main.c`, right after `display_show("Connecting WiFi...", NULL)` | `VOICE_CONNECTING` |
| Session ready | `on_event()`'s `WSP_EV_SESSION_STARTED` case, right after `display_show("Connected", host_port)` | `VOICE_CONNECTED` |

`main` and `provisioning` both gain a dependency on the new `voice`
component (alongside their existing `display` dependency).

## Error handling

- `voice_play()` takes no error path — playback failure (e.g. codec write
  error) is not distinguishable from normal operation at this call site and
  isn't worth surfacing; `audio_spk_write()`'s existing return value is
  ignored here (same as the existing `spk_task`'s handling, which only acts
  on success and silently drops on failure).
- `voice_play()` is blocking and adds the clip's real-time duration
  (2-3.7s) to whichever call site invokes it — one-time cost during
  setup/connect, not a hot path. This is a deliberate simplification (no
  async playback queue), acceptable because none of the three trigger points
  are latency-sensitive.

## Testing

- Not host-testable (ESP-IDF `EMBED_FILES`/`audio_spk_write()` + real audio
  hardware) — verified on-device: confirm each of the three clips plays at
  the right moment, sounds correct (right words, no garbling/static), and
  that the mutex doesn't introduce an audible glitch when a voice
  announcement and mic/speaker activity overlap in practice.
- The `audio.c` mutex addition itself is a small, mechanical wrap of two
  existing functions — no separate test framework exists for this
  ESP-IDF-only component; verify via the same on-device pass.
