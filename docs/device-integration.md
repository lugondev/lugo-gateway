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
| `stt_engine` | `whisper_mlx` | server-side STT (Vietnamese) |
| `tts_engine` | `vieneu` | server-side Vietnamese TTS |
| `language` | `vi` | STT language hint |
| `sample_rate` | `16000` | **uplink** audio rate (Hz) |
| `audio_codec` | `opus` | uplink codec — raw Opus packets |
| `output` | `audio,text` | what to receive: `audio` (+ `text` for subtitles/debug) |
| `audio_out` | `opus` | reply audio delivered as **pushed Opus frames** (not a URL) |
| `output_sample_rate` | `24000` | **downlink** Opus rate (Hz) |
| `profile` | *(optional)* | named **chatllm profile** — see §1a below |

Full example:
```
ws://192.168.1.50:8000/v1/conversation/stream?stt_engine=whisper_mlx&tts_engine=vieneu&language=vi&sample_rate=16000&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=24000
```

Full example with a profile (replaces `tts_engine`/LLM config, `stt_engine`/`language` still apply as fallback):
```
ws://192.168.1.50:8000/v1/conversation/stream?profile=kitchen&sample_rate=16000&audio_codec=opus&output=audio,text&audio_out=opus&output_sample_rate=24000
```

On connect the server sends one `session_started` JSON with the negotiated config
(`stt_engine`, `tts_engine`, `llm_model`, `audio_codec`, `audio_out`,
`output_sample_rate`). Always read it first.

## 1a. Profiles: connect a device as a preset "chatllm" persona

A **profile** is a named bundle of everything a conversation session needs — LLM
endpoint/model, system prompt, TTS engine/voice, MCP tool servers, and memory
(auto-extraction/retrieval) settings — created once via the REST API and then activated
on any device by passing `?profile=<name>` instead of wiring each setting separately.

Create a profile (once, from any machine that can reach the gateway):
```bash
curl -X POST http://<server-host>:8000/v1/profiles \
  -H "Content-Type: application/json" \
  -d '{
        "name": "kitchen",
        "llm": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"},
        "system_prompt": "You are a concise kitchen assistant.",
        "tts": {"engine": "vieneu"}
      }'
```

Then point the device's WS URL at `?profile=kitchen`. Precedence:
- **LLM (model/base_url/api_key/system_prompt) and MCP tool servers**: always come from
  the profile when set — there's no device-side query param for these.
- **TTS**: the profile's `tts.engine`/`tts.voice` win over `?tts_engine=`/`?voice=` if
  the profile sets them.
- **STT engine/language**: an explicit `?stt_engine=`/`?language=` on the device URL still
  wins. If neither is given and the profile's *name* matches a built-in language preset
  (`vi`, `en`, `multi`, `en_vi`), that preset's engine/language is used.
- **Memory**: if `memory.enabled` is true on the profile, the server auto-extracts and
  later injects relevant memories into the system prompt for that profile — no device
  change needed.

If the `profile` name doesn't exist, the server sends a `warning` event and the session
proceeds with defaults (nothing breaks, but you'll be talking to whatever the `.env`
default LLM is instead of the intended persona) — check for that event during device
bring-up.

Manage profiles with `GET /v1/profiles`, `GET/PUT/DELETE /v1/profiles/{name}` — see
[api.md](api.md#profiles--named-chatllm-presets).

**Client support today:**
- `scripts/rpi_voice_client.py` — pass `--profile <name>`.
- `agent-assistant/` (production RPi service) — set `session.profile: <name>` in
  `config.yaml`.
- **ESP32 firmware** (`esp32-assistant/`) — set `AA_PROFILE` in `menuconfig` →
  "Assistant configuration" (empty by default = no profile sent).

## 2. Audio formats

| direction | codec | rate | channels | frame |
|-----------|-------|------|----------|-------|
| **uplink** (device → server) | Opus | 16 000 Hz | mono | 20–60 ms (use **60 ms = 960 samples**) |
| **downlink** (server → device) | Opus | 24 000 Hz | mono | 60 ms (1440 samples) |

- Each Opus packet is **one binary WebSocket frame** (no extra header).
- PCM is signed 16-bit little-endian before/after Opus.
- **Downlink pacing:** the server sends the first ~5 packets of each sentence immediately
  (fills your jitter buffer fast → low first-audio latency), then paces the rest at one
  frame (60 ms) so it emits at playback rate and a small device buffer never overflows on
  long replies. Just play packets as they arrive; keep ~100–200 ms of jitter buffer.
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
# activate a saved profile instead of --stt/--tts:
python scripts/rpi_voice_client.py --host <server-ip> --profile kitchen
```

It captures the mic at 16 kHz, encodes 60 ms Opus frames, streams them, decodes the
24 kHz reply frames between `audio_start`/`audio_end`, and plays them — with
half-duplex mic muting during playback.

## 6. Browser client (WebCodecs Opus downlink)

Browsers can receive the streamed Opus reply (instead of fetching WAV URLs) using the
**WebCodecs** `AudioDecoder` — ~10× less downlink bandwidth, gapless playback. Connect
with `audio_out=opus&output_sample_rate=24000`, set `ws.binaryType = "arraybuffer"`:

```js
const dec = new AudioDecoder({
  output: (audioData) => {
    // copy planar f32 -> AudioBuffer -> schedule on an AudioContext timeline
    const buf = ctx.createBuffer(1, audioData.numberOfFrames, audioData.sampleRate);
    const arr = new Float32Array(audioData.numberOfFrames);
    audioData.copyTo(arr, { planeIndex: 0, format: "f32-planar" });
    buf.copyToChannel(arr, 0);
    audioData.close();
    /* createBufferSource → start at max(ctx.currentTime, nextTime) → advance nextTime */
  },
  error: (e) => console.error(e),
});
dec.configure({ codec: "opus", sampleRate: 24000, numberOfChannels: 1 });

let ts = 0; // microseconds
ws.onmessage = (ev) => {
  if (typeof ev.data !== "string") {            // binary = one 60 ms Opus packet
    dec.decode(new EncodedAudioChunk({ type: "key", timestamp: ts, data: ev.data }));
    ts += 60000;                                // 60 ms frames
    return;
  }
  const m = JSON.parse(ev.data);                // audio_start / audio_end / response_text / …
};
```

- Each Opus packet is self-contained → use `type:"key"` for every frame.
- On `aborted` (barge-in), close + recreate the decoder and reset `ts` so stale audio can't play.
- Needs a WebCodecs-capable browser (Chromium, Safari 16.4+, recent Firefox); fall back to
  `audio_out=url` (fetch the WAV and `decodeAudioData`) otherwise.
- The built-in playground (`/ui` → Conversation) has an **"Opus downlink"** checkbox that
  does exactly this — use it to verify before writing your own client.

## 7. Other modes (same endpoint)

- **Audio → text only** (transcription service): `?output=text` (no `audio_out`). You
  get `user_transcript` + `response_text`, no audio.
- **Text → audio**: `?output=audio&audio_out=opus`, then send `{"type":"text","text":"…"}`
  to hear a spoken reply (no mic).
- **Text → text** (chatbot): `?output=text` + `{"type":"text",…}`.

## 8. Notes for the device dev

- The server's TTS/LLM run remotely; first use of a heavy STT model loads it — call
  `POST /v1/stt/warm?engine=<engine>` once at boot if you use one.
- libopus must be present on the device (`apt install libopus0`).
- Reconnect with backoff on socket close; re-read `session_started` each time.
- Keep ~100–200 ms of jitter buffer on playback for smooth audio over WiFi.
- Browser playground at `/ui` (Conversation tab) is the easiest way to sanity-check the
  server before wiring the device.

See [api.md](api.md) for the complete REST/WebSocket reference.
