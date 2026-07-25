# QwenCloud STT Engine — Design

**Date:** 2026-07-25
**Status:** Approved design → implementation
**Scope:** Add a new remote STT engine `qwencloud` (Alibaba DashScope Model Studio, international `dashscope-intl` surface) with both batch and native real-time (WebSocket) transcription, built around **qwen3-asr-flash**.

> All endpoints, request/response shapes, and the WebSocket protocol below were **verified live against the real API** on 2026-07-25 with a provided key. Findings that overrode the initial assumptions are called out in §2.

## 1. Goal & context

The gateway has a clean STT provider interface (`app/services/stt/base.py`):

- `STTProvider.transcribe_bytes(audio_bytes, language, model) -> STTResult` — batch, bytes-in/text-out.
- `STTProvider.open_stream(sample_rate, language) -> STTStream` — native realtime; defaults to `BufferingStream` (accumulate PCM, transcribe once on `finalize`). Only `vosk` overrides it today.

Remote engines resolve config **per call** from the **Model Registry** (`kind="stt"`, `engine`, `model_id`, `base_url`, `api_key`, `config`), so admin edits take effect immediately (pattern: `http_stt_provider.py`). This engine follows that pattern.

## 2. Verified findings & locked decisions

Live probing (host `dashscope-intl.aliyuncs.com`, `Authorization: Bearer <key>`) established:

| Model | Batch (raw bytes-in) | Realtime WS |
|---|---|---|
| **qwen3-asr-flash** | ✅ **inline sync** via multimodal-generation (returns text + emotion + language) | ✅ verified (Vietnamese) |
| fun-asr | ⚠️ async **public-URL only** (submit + poll + fetch `transcription_url`) | ❌ "Model not found" on this surface |

- There is **no** OpenAI-compatible `/compatible-mode/v1/audio/transcriptions` endpoint (returns 404).
- The realtime WS host is **fixed**: `wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=…` — **no `workspace_id`** in the host (contrary to the MaaS `{workspace_id}.{region}.maas…` gateway seen in some Alibaba docs).

**Decisions (locked):**

1. **Engine name** `qwencloud`, one unified provider, model chosen via registry entry.
2. **v1 supports qwen3-asr-flash** (both batch inline + realtime WS). This is the only model that fits the system's bytes-in batch contract *and* has a working realtime WS.
3. **fun-asr is out of scope for v1** — no realtime here, and its async public-URL batch does not fit the bytes-in pipeline (would require hosting mic audio at a public URL). Documented as a future extension (§8). *(This overrides the earlier scope that included fun-asr, based on live API results.)*
4. **No WS-one-shot batch fallback needed** — the inline sync batch path works directly (the earlier fallback plan is dropped).

## 3. Verified API reference

### 3a. Batch — inline sync (used by `transcribe_bytes`)
`POST https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation`
Headers: `Authorization: Bearer <key>`, `Content-Type: application/json`

```json
{
  "model": "qwen3-asr-flash",
  "input": {"messages": [{"role": "user",
    "content": [{"audio": "data:audio/wav;base64,<BASE64_WAV>"}]}]},
  "parameters": {"asr_options": {"language": "vi", "enable_lid": true}}
}
```
Response: transcript at `output.choices[0].message.content[0].text`; `output.choices[0].message.annotations[]` carries `{"type":"audio_info","emotion":…,"language":…}`. Omit `language` (or drop `asr_options`) for auto-detect; keep `enable_lid` for language id.

### 3b. Realtime — WebSocket (used by `open_stream`)
`wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime`
Connect header: `Authorization: Bearer <key>`. Audio = **PCM16, 16 kHz, mono**, base64, ~3200 bytes/100 ms.

Client events: `session.update` → then N × `input_audio_buffer.append {audio: b64}` → `session.finish`.
```json
{"type":"session.update","session":{"modalities":["text"],"input_audio_format":"pcm",
 "sample_rate":16000,"input_audio_transcription":{"language":"vi"},
 "turn_detection":{"type":"server_vad","threshold":0.0,"silence_duration_ms":400}}}
```
Server events (verified order): `session.created` → `session.updated` → `input_audio_buffer.speech_started` → `conversation.item.created` → N × `conversation.item.input_audio_transcription.text` → `speech_stopped` → `input_audio_buffer.committed` → `conversation.item.input_audio_transcription.completed` → `session.finished`.

**Critical field detail (from live test):** on `…transcription.text`, the incremental partial is in **`stash`** (the `text` field is empty ""); on `…transcription.completed`, the final is in **`transcript`**. The provider MUST read `stash` for partials and `transcript` for finals.

## 4. Provider shape

New file: `app/services/stt/providers/qwencloud_provider.py`

```
class QwenCloudSttProvider(STTProvider):
    name = "qwencloud"
    def __init__(self, name="qwencloud", timeout_seconds=60.0, entry=None): ...
    async def _resolve_entry(self, model) -> dict | None      # registry lookup, like http_stt
    async def transcribe_bytes(audio_bytes, language, model) -> STTResult   # §3a inline
    def open_stream(sample_rate, language) -> QwenCloudStream               # §3b WS
    def available(self) -> bool                                # ≥1 enabled entry with api_key
    def detail(self) -> str
```

Registry `entry` fields:
- `base_url` — default `https://dashscope-intl.aliyuncs.com`; the WS host derives from it (`https→wss`, `/api-ws/v1/realtime`).
- `model_id` — batch model (default `qwen3-asr-flash`).
- `config.realtime_model` — realtime WS model (default `qwen3-asr-flash-realtime`).
- `config.language`, `config.turn_detection` (`server_vad` default | `manual`), `config.timeout_seconds`.
- `_entry_override` (ctor `entry=`) for the registry test-before-add call, like `http_stt`.

## 5. Batch path — `transcribe_bytes`
1. Resolve entry → `base_url`/`api_key`; clear RuntimeError if unconfigured (mirror `http_stt`).
2. POST §3a with base64 of the WAV bytes and `asr_options.language` (from arg/config; omit → auto). `enable_lid: true`.
3. Parse `output.choices[0].message.content[0].text` (defensive: handle missing/empty → "").
4. Return `STTResult(engine="qwencloud", text=…, is_final=True, confidence=None)`. (Emotion/language available in annotations — not surfaced in v1; §8.)
5. `httpx.HTTPError` → `translate_httpx_error(self.name, exc)`.

## 6. Realtime path — `QwenCloudStream(STTStream)`
- Wraps a `websockets` connection + a reader task draining server events into an `asyncio.Queue`.
- **Lazy connect** on first `accept`; send `session.update` (§3b).
- `accept(pcm)`: audio already PCM16/16 kHz/mono (no resample). Send `input_audio_buffer.append` (base64). Drain queue: `…text` → partial `STTResult(text=stash, is_final=False)`; `…completed` → final `STTResult(text=transcript, is_final=True)`. Return the list produced this call.
- `finalize()`: send `session.finish`; drain until `session.finished`; return the last final (or accumulated `transcript`); close the socket. On mid-stream disconnect, return the best partial seen — never raise hard.
- Because `open_stream` is overridden, `list_engines` auto-reports `realtime: true`.
- `websockets.connect(url, additional_headers={"Authorization": f"Bearer {key}"})` (websockets ≥14 uses `additional_headers`, not `extra_headers`).

## 7. Wiring
- `service.py`: register `"qwencloud": QwenCloudSttProvider(timeout_seconds=remote_stt.remote_stt_timeout_seconds)`. No `reinit_remote_providers` entry (resolves per-call). Add a `list_engines` branch: `mode="remote"`, `available` = an enabled `qwencloud` entry with resolved `api_key` (same loop as `qwen3_asr_or`), `detail` = model id.
- `model_catalog.py`: **no** `STT_MODEL_CATALOGS` entry (model lives in the registry row).
- `pyproject.toml` (api_gateway): add `websockets` as a **direct** dep (currently only transitive; verified `websockets 16.0` importable in the dev venv).
- Admin UI: reuse existing add-remote-engine + registry flow. Verify the entry form exposes `config` JSON (language / turn_detection / realtime_model); if not, note the gap (do not expand scope).

## 8. Out of scope (future extensions)
- fun-asr / qwen3-asr-flash-filetrans **async public-URL** transcription (long files, timestamps, diarization) — needs a file-hosting capability the pipeline lacks.
- Qwen-Omni family.
- Surfacing realtime **emotion**/language metadata (available in both paths) via an `STTResult` metadata extension.

## 9. Testing (TDD)
1. `transcribe_bytes`: mock httpx multimodal-generation → assert body carries base64 audio + model + `asr_options`; parse text from `output.choices[0].message.content[0].text`; empty/missing → "".
2. `QwenCloudStream`: fake WS event source → feed `…text {stash}` then `…completed {transcript}`; assert partial(`is_final=False`, text=stash) / final(`is_final=True`, text=transcript) mapping and that `finalize` sends `session.finish`.
3. Registry resolution: unconfigured → clear RuntimeError; configured entry → correct host/model/realtime_model (pattern: `test_http_stt_provider.py`, `test_stt_remote_registry.py`).
4. `list_engines`: `qwencloud` present, `mode="remote"`, `realtime=True`, `configured` reflects key presence.
5. WS-disconnect: `finalize` returns last partial without raising.

## 10. Files touched
- **New**: `app/services/stt/providers/qwencloud_provider.py`
- **New**: `tests/unit/test_qwencloud_stt_provider.py`
- **Edit**: `app/services/stt/service.py` (register + `list_engines` branch)
- **Edit**: `apps/api_gateway/pyproject.toml` (`websockets` direct dep)
- **Verify**: admin registry entry form exposes `config` for language/turn_detection/realtime_model
