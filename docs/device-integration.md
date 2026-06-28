# Device integration guide (Raspberry Pi / ESP32)

How to build a voice device that talks to the gateway. The device is a **thin client**:
it captures the mic, sends audio, and plays back the reply. All STT / LLM / TTS run on
the **server** — the device needs only audio I/O, Opus, and a WebSocket.

```
┌──────────── Raspberry Pi ────────────┐         ┌──────────────── Gateway server ────────────────┐
│ mic → Opus(16k) ──────────────────────┼──WS────▶│ decode → VAD → STT → LLM → TTS                  │
│ speaker ◀── Opus(24k) ◀────────────────┼──WS─────│ → encode → push reply frames                    │
└───────────────────────────────────────┘         └─────────────────────────────────────────────────┘
```

## 1. Endpoint

```
ws://<server-host>:8000/v1/conversation/stream?<params>
```

Recommended params for a duplex voice device:

| param | value | meaning |
|-------|-------|---------|
| `stt_engine` | `whisper_mlx` | server-side STT (Vietnamese). `qwen_omni` = audio-native (server answers audio directly) |
| `tts_engine` | `vieneu` | server-side Vietnamese TTS |
| `language` | `vi` | STT language hint |
| `sample_rate` | `16000` | **uplink** audio rate (Hz) |
| `audio_codec` | `opus` | uplink codec — raw Opus packets |
| `output` | `audio,text` | what to receive: `audio` (+ `text` for subtitles/debug) |
| `audio_out` | `opus` | reply audio delivered as **pushed Opus frames** (not a URL) |
| `output_sample_rate` | `24000` | **downlink** Opus rate (Hz) |

Full example:
```
ws://192.168.1.50:8000/v1/conversation/stream?stt_engine=whisper_mlx&tts_engine=vieneu&language=vi&sample_rate=16000&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=24000
```

On connect the server sends one `session_started` JSON with the negotiated config
(`stt_engine`, `tts_engine`, `llm_model`, `audio_codec`, `audio_out`,
`output_sample_rate`). Always read it first.

## 2. Audio formats

| direction | codec | rate | channels | frame |
|-----------|-------|------|----------|-------|
| **uplink** (device → server) | Opus | 16 000 Hz | mono | 20–60 ms (use **60 ms = 960 samples**) |
| **downlink** (server → device) | Opus | 24 000 Hz | mono | 60 ms (1440 samples) |

- Each Opus packet is **one binary WebSocket frame** (no extra header).
- PCM is signed 16-bit little-endian before/after Opus.
- If you cannot do Opus, use `audio_codec=pcm16` (uplink) and `audio_out=url` (downlink,
  the server returns an `audio_url` to fetch) — simpler but ~10× more bandwidth.

## 3. Protocol

### Device → server
- **Binary frame** = one Opus packet of mic audio (stream continuously).
- **Text JSON**:
  - `{"type":"text","text":"…"}` — text input turn (no mic).
  - `{"type":"flush"}` — force end-of-turn now (push-to-talk: send audio, then flush).
  - `{"type":"abort"}` — cancel the current reply.
  - `{"type":"reset"}` — clear conversation history.
  - `{"type":"end"}` — finalize and close.

### Server → device (JSON `{"event": …}`, plus binary frames)
| event | meaning | fields |
|-------|---------|--------|
| `session_started` | handshake | engines, `audio_out`, `output_sample_rate`, … |
| `speech_start` | server detected you started speaking | — |
| `speech_end` | end of your turn (VAD) | `speech_ms` |
| `processing` | transcribing + generating | `turn` |
| `user_transcript` | what you said (STT) | `text` |
| `response_text` | reply text (subtitle) | `text`, `chunk_index` |
| `audio_start` | **next N binary frames are reply audio** | `chunk_index`, `codec:"opus"`, `sample_rate`, `frames` |
| _(binary)_ | one Opus packet of reply audio | — |
| `audio_end` | end of this sentence's audio | `chunk_index` |
| `turn_done` | reply finished | `turn` |
| `aborted` | reply cancelled (barge-in) | `reason` |
| `error` | failure (keeps the socket open) | `message` |

**Reply audio framing:** for each reply sentence the server sends
`audio_start {frames: N}` → exactly `N` binary Opus packets → `audio_end`. Decode and
play the packets in order. A reply has several sentences (several start/end groups),
then `turn_done`.

## 4. Turn lifecycle

**Always-on VAD (hands-free):** stream mic Opus continuously. The server endpoints on
~700 ms of trailing silence and replies. No control messages needed.

```
device:  ──opus──opus──opus──(silence)──────────────────────────
server:  speech_start … speech_end → processing → user_transcript
         → response_text + audio_start/▮▮▮/audio_end (×sentences) → turn_done
```

**Push-to-talk:** send mic Opus only while the button is held; on release send
`{"type":"flush"}` to end the turn immediately.

**Half-duplex (important):** while you are playing the reply (`audio_start` … `turn_done`),
**stop uplinking mic audio** — otherwise the speaker bleeds into the mic and the server
treats it as barge-in and cancels the reply. To support barge-in (user interrupts),
keep uplinking; a `speech_start` mid-reply yields `aborted` — stop playback on it.

## 5. Reference client

A runnable Python client is in [`scripts/rpi_voice_client.py`](../scripts/rpi_voice_client.py):

```bash
# on the Raspberry Pi
sudo apt install -y libopus0 portaudio19-dev
pip install websockets sounddevice opuslib numpy

python scripts/rpi_voice_client.py --host <server-ip> --port 8000
```

It captures the mic at 16 kHz, encodes 60 ms Opus frames, streams them, decodes the
24 kHz reply frames between `audio_start`/`audio_end`, and plays them — with
half-duplex mic muting during playback.

## 6. Other modes (same endpoint)

- **Audio → text only** (transcription service): `?output=text` (no `audio_out`). You
  get `user_transcript` + `response_text`, no audio.
- **Text → audio**: `?output=audio&audio_out=opus`, then send `{"type":"text","text":"…"}`
  to hear a spoken reply (no mic).
- **Text → text** (chatbot): `?output=text` + `{"type":"text",…}`.

## 7. Notes for the device dev

- The server's TTS/LLM run remotely; first use of a heavy STT (`qwen_omni`) loads a
  model — call `POST /v1/stt/warm?engine=qwen_omni` once at boot if you use it.
- libopus must be present on the device (`apt install libopus0`).
- Reconnect with backoff on socket close; re-read `session_started` each time.
- Keep ~100–200 ms of jitter buffer on playback for smooth audio over WiFi.
- Browser playground at `/ui` (Conversation tab) is the easiest way to sanity-check the
  server before wiring the device.

See [api.md](api.md) for the complete REST/WebSocket reference.
