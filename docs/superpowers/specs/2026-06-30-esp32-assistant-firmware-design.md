# ESP32-S3 voice firmware (`esp32-assistant`) — design

**Date:** 2026-06-30
**Status:** Approved (design); pending implementation plan

## Goal

A thin-client voice device firmware for the **ESP32-S3** (xiaozhi-style board, **ES8311**
codec) that talks **directly** to this repo's gateway over its native WebSocket protocol
(`/v1/conversation/stream`). All STT / LLM / TTS run server-side; the device only does
WiFi, Opus, I2S audio I/O, and the conversation state machine.

Inspired by the audio-pipeline architecture of
[xiaozhi-esp32](https://github.com/78/xiaozhi-esp32), but **not** a fork: xiaozhi's own
MQTT+UDP / `hello`/`listen`/`tts` protocol is replaced by this gateway's protocol. This
is a lean ESP-IDF project.

## Decisions (from brainstorming)

- **Hardware:** ESP32-S3 xiaozhi board, single **ES8311** codec (mic ADC + speaker DAC).
- **Scope:** MVP duplex voice. Hands-free with **server-side VAD**; half-duplex mic mute
  during playback (mirrors the RPi reference client).
- **Approach:** fresh, lean ESP-IDF firmware speaking the gateway protocol directly.
- **WiFi/config:** via Kconfig → menuconfig (stored in NVS). No runtime web provisioning
  in MVP.
- **Out of MVP scope:** OLED display, wake-word, OTA, web WiFi provisioning, push-to-talk
  (deferred to later versions).

## Protocol (target — from `agent-assistant/integration.md`)

Endpoint:
```
ws://<host>:8000/v1/conversation/stream?stt_engine=whisper_mlx&tts_engine=vieneu&language=vi&sample_rate=16000&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=24000
```

- **Uplink** (device→server): one Opus packet per **binary** WS frame. 16 kHz mono, 60 ms
  = 960 samples.
- **Downlink** (server→device): one Opus packet per binary frame. 24 kHz mono, 60 ms =
  1440 samples.
- **Client→server JSON** (text frames): `{"type":"flush"|"abort"|"reset"|"end"}` and
  `{"type":"text","text":"…"}` (text input; not used in hands-free MVP loop but supported
  by `ws_protocol`).
- **Server→device events** (JSON `{"event":…}`): `session_started`, `speech_start`,
  `speech_end`, `processing`, `user_transcript`, `response_text`,
  `audio_start {chunk_index, codec:"opus", sample_rate, frames}`, `audio_end`,
  `turn_done`, `aborted`, `error`.
- **Reply framing:** per sentence the server sends `audio_start {frames:N}` → exactly N
  binary Opus packets → `audio_end`. A reply has several such groups, then `turn_done`.
- On connect the server sends one `session_started` JSON with the negotiated config — read
  it first, on every (re)connect.

## Architecture

```
  ┌─────────────────── ESP32-S3 ───────────────────┐        gateway
  │ mic ─I2S→ [opus enc 16k/60ms] ─┐                │
  │                                 ├→ ws_client ───┼──WS──▶ /v1/conversation/stream
  │ loa ◀I2S─ [opus dec 24k/60ms] ◀─┘ (jitter buf)  │◀──────  (binary opus + JSON events)
  └──────────────── app state machine ──────────────┘
```

Three FreeRTOS tasks + the WS event callback:
- **mic_task:** read I2S @16k → Opus encode (960-sample/60 ms frames) → send as binary WS
  frame, **only while `state == LISTENING`**. While `SPEAKING` it keeps reading I2S (to
  drain the codec) but does **not** send (half-duplex).
- **ws rx (event callback):** parse JSON text frames → drive the state machine; push
  binary frames into the playback jitter buffer.
- **spk_task:** pull Opus packets from the jitter buffer → decode @24k → write to I2S
  speaker.

## Components (one responsibility each)

| Component | Responsibility | Depends on |
|-----------|----------------|------------|
| `wifi` | STA connect, retry, got-IP event; SSID/pass from Kconfig→NVS | esp_wifi, NVS |
| `ws_protocol` | **Pure logic, no ESP deps** → host-testable. Build client JSON (`flush`/`abort`/`reset`/`end`/`text`), parse server events into a tagged struct, build the connect URL+query | cJSON |
| `audio` | Init ES8311 + I2S (std mode); `mic_read(pcm,n)` / `spk_write(pcm,n)`; mic mute/unmute | esp_codec_dev, es8311 |
| `opus_codec` | Encode 16k mono / decode 24k mono, 60 ms frames | managed component `espressif/opus` |
| `app` (main) | Conversation state machine, jitter buffer (ring of Opus packets), task wiring, Kconfig | all of the above |

`ws_protocol` is deliberately free of ESP-IDF dependencies so it compiles and unit-tests
on the host. It returns parsed events as a small tagged union/struct; the `app` layer maps
events to state transitions and the `ws_client` (esp_websocket_client) handles transport.

## State machine & data flow

```
CONNECTING → (session_started) → LISTENING
LISTENING:  mic→opus→ws continuously; receive speech_start/end, processing, user_transcript
            (audio_start) → SPEAKING
SPEAKING:   mute mic uplink; for each audio_start{frames:N} → N binary packets → audio_end;
            multiple sentence groups may arrive
            (turn_done) → unmute mic → LISTENING
            (aborted)   → flush jitter buffer, unmute → LISTENING
any state:  error event → log, keep socket open
            ws close    → reconnect with backoff, re-read session_started
```

Jitter buffer: ring of decoded-pending Opus packets, target ~150 ms depth, for smooth
playback over WiFi.

## Error handling

- **WS disconnect:** reconnect with exponential backoff (1 s → 20 s); reset state to
  CONNECTING; re-read `session_started`.
- **Jitter buffer underrun:** emit short silence; **overflow:** drop oldest packet.
- **Opus decode error:** skip the packet, do not crash.
- **WiFi lost:** pause audio tasks until got-IP fires again.

## Configuration (Kconfig → menuconfig, persisted in NVS where applicable)

- WiFi SSID / password.
- Server host / port; secure (ws/wss) flag.
- Session params: `stt_engine` (default `whisper_mlx`), `tts_engine` (`vieneu`),
  `language` (`vi`).
- I2S / codec pins: MCLK, BCLK, WS, DO, DI; I2C SDA/SCL + ES8311 address.
- Fixed query defaults: `sample_rate=16000`, `audio_codec=opus`, `output=audio,text`,
  `audio_out=opus`, `output_sample_rate=24000`.

## Testing

- **Host unit tests** for `ws_protocol` (Unity + CMake host build): parse every sample
  server event into the right struct; build correct client JSON for each control message;
  assemble the correct connect URL/query string. This is the highest-risk pure-logic
  surface and is testable without hardware.
- **On-device:** `idf.py build flash monitor`; manual verification against a running
  gateway. The **user** flashes the board (the dev host cannot flash it). README documents
  the flow and recommends sanity-checking the server first via the playground `/ui`
  (Conversation tab, "Opus downlink").

## Directory layout

```
esp32-assistant/
  CMakeLists.txt
  sdkconfig.defaults
  partitions.csv
  main/
    CMakeLists.txt
    Kconfig.projbuild        # WiFi + server + session config
    main.c                   # app_main, state machine, jitter buffer, task wiring
  components/
    wifi/
    ws_protocol/             # pure logic; host-testable
    audio/                   # ES8311 + I2S
    opus_codec/              # Opus encode/decode wrappers
  test/                      # host unit tests for ws_protocol
  README.md
```

## Open items for the implementation plan

- Confirm the exact `espressif/opus` (or alternative) managed-component name/version and
  its encode/decode API on ESP-IDF.
- Confirm ES8311 driver path: direct `esp_codec_dev` + `es8311` vs. an `esp-bsp` board
  package. Design assumes direct `esp_codec_dev` for a generic xiaozhi board.
- Pin defaults for the specific board (set sensible defaults in Kconfig; user overrides in
  menuconfig).
