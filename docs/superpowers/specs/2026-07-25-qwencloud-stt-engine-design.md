# QwenCloud STT Engine — Design

**Date:** 2026-07-25
**Status:** Approved design → implementation
**Scope:** Add a new remote STT engine `qwencloud` (Alibaba DashScope Model Studio, international `dashscope-intl` surface) with both batch and native real-time (WebSocket) transcription, supporting **two model families: qwen3-asr-flash and fun-asr** — each via its own WebSocket protocol.

> All endpoints, request/response shapes, and the WebSocket protocol below were **verified live against the real API** on 2026-07-25 with a provided key. Findings that overrode the initial assumptions are called out in §2.

## 1. Goal & context

The gateway has a clean STT provider interface (`app/services/stt/base.py`):

- `STTProvider.transcribe_bytes(audio_bytes, language, model) -> STTResult` — batch, bytes-in/text-out.
- `STTProvider.open_stream(sample_rate, language) -> STTStream` — native realtime; defaults to `BufferingStream` (accumulate PCM, transcribe once on `finalize`). Only `vosk` overrides it today.

Remote engines resolve config **per call** from the **Model Registry** (`kind="stt"`, `engine`, `model_id`, `base_url`, `api_key`, `config`), so admin edits take effect immediately (pattern: `http_stt_provider.py`). This engine follows that pattern.

## 2. Verified findings & locked decisions

Live probing (host `dashscope-intl.aliyuncs.com`, `Authorization: Bearer <key>`) established two model families, each with a **different WebSocket protocol**:

| Family | Batch (raw bytes-in) | Realtime WS | WS protocol |
|---|---|---|---|
| **qwen3-asr-flash** | ✅ **inline sync** via multimodal-generation (text + emotion + language) | ✅ verified (Vietnamese) | OpenAI-Realtime-compatible |
| **fun-asr** | ✅ via **one-shot realtime WS** (no inline HTTP; async public-URL exists but needs hosting) | ✅ verified (`fun-asr-realtime`) | DashScope-native (`run-task`) |

- There is **no** OpenAI-compatible `/compatible-mode/v1/audio/transcriptions` endpoint (404).
- Both WS hosts are **fixed** (no `workspace_id`, contrary to the MaaS `{workspace_id}.{region}.maas…` gateway in some Alibaba docs), but the **paths and protocols differ per family** (§3b, §3c).

**Decisions (locked):**

1. **Engine name** `qwencloud`, one unified provider; the registry entry's model determines the family (`qwen3-asr*` vs `fun-asr*`), and both `open_stream` and `transcribe_bytes` branch on it.
2. **v1 supports both families**, each realtime + batch:
   - **qwen3-asr-flash** — realtime via OpenAI-Realtime WS; batch via inline multimodal-generation HTTP.
   - **fun-asr** — realtime via DashScope-native WS; batch via a **one-shot native WS session** (feed the whole buffer → `finish-task` → collect sentences). No file hosting.
3. The async **public-URL** file-transcription API (long files, timestamps, diarization) stays **out of scope** for v1 — it does not fit the bytes-in pipeline (§8).

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

### 3b. Realtime — qwen3-asr (OpenAI-Realtime protocol)
`wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime`
Connect header: `Authorization: Bearer <key>`. Audio = **PCM16, 16 kHz, mono**, **base64** in JSON, ~3200 bytes/100 ms.

Client events: `session.update` → then N × `input_audio_buffer.append {audio: b64}` → `session.finish`.
```json
{"type":"session.update","session":{"modalities":["text"],"input_audio_format":"pcm",
 "sample_rate":16000,"input_audio_transcription":{"language":"vi"},
 "turn_detection":{"type":"server_vad","threshold":0.0,"silence_duration_ms":400}}}
```
Server events (verified order): `session.created` → `session.updated` → `input_audio_buffer.speech_started` → `conversation.item.created` → N × `conversation.item.input_audio_transcription.text` → `speech_stopped` → `input_audio_buffer.committed` → `conversation.item.input_audio_transcription.completed` → `session.finished`.

**Critical field detail (from live test):** on `…transcription.text`, the incremental partial is in **`stash`** (the `text` field is empty ""); on `…transcription.completed`, the final is in **`transcript`**. Read `stash` for partials, `transcript` for finals.

### 3c. Realtime — fun-asr (DashScope-native `run-task` protocol)
`wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference` (**no model in URL**). Connect header: `Authorization: bearer <key>`. Audio = **PCM16, 16 kHz, mono**, sent as **raw binary WebSocket frames** (NOT base64), after `task-started`.

Client directives (text JSON): `run-task` → (binary audio frames) → `finish-task`. Each carries `header.task_id` (a client-generated UUID) and `header.streaming:"duplex"`.
```json
{"header":{"action":"run-task","task_id":"<uuid>","streaming":"duplex"},
 "payload":{"task_group":"audio","task":"asr","function":"recognition","model":"fun-asr-realtime",
  "parameters":{"format":"pcm","sample_rate":16000,"semantic_punctuation_enabled":true},"input":{}}}
```
Server events (verified): `task-started` → N × `result-generated` → `task-finished` (`event` is at `header.event`).

**Critical field detail (from live test):** each `result-generated` carries `payload.output.sentence` = `{text, sentence_begin, sentence_end, begin_time, end_time, words, stash}`. Map **`sentence_end:false` → partial** (`text` so far), **`sentence_end:true` → final** for that sentence; `sentence.stash` starts the next sentence. Concatenate finalized sentences for the full transcript.

## 4. Provider shape

New file: `app/services/stt/providers/qwencloud_provider.py`

```
def _family(model) -> "qwen3" | "funasr"          # prefix: "qwen3-asr"→qwen3, "fun-asr"→funasr

class QwenCloudSttProvider(STTProvider):
    name = "qwencloud"
    def __init__(self, name="qwencloud", timeout_seconds=60.0, entry=None): ...
    async def _resolve_entry(self, model) -> dict | None      # registry lookup, like http_stt
    async def transcribe_bytes(audio_bytes, language, model) -> STTResult   # branch on family (§5)
    def open_stream(sample_rate, language) -> STTStream                     # family → stream class (§6)
    def available(self) -> bool                                # ≥1 enabled entry with api_key
    def detail(self) -> str
```

The registry entry's model chooses the family; `open_stream`/`transcribe_bytes` dispatch on `_family(realtime_model | model_id)`.

Registry `entry` fields:
- `base_url` — default `https://dashscope-intl.aliyuncs.com`; WS hosts derive from it (`https→wss`; path per family: `/api-ws/v1/realtime` for qwen3, `/api-ws/v1/inference` for fun-asr).
- `model_id` — batch/HTTP model (default `qwen3-asr-flash`).
- `config.realtime_model` — realtime WS model (default `qwen3-asr-flash-realtime`; use `fun-asr-realtime` for the fun-asr family).
- `config.language`, `config.turn_detection` (qwen3: `server_vad` default | `manual`), `config.semantic_punctuation` (fun-asr), `config.timeout_seconds`.
- `_entry_override` (ctor `entry=`) for the registry test-before-add call, like `http_stt`.

## 5. Batch path — `transcribe_bytes` (dispatch by family)
Resolve entry → `base_url`/`api_key`; clear RuntimeError if unconfigured (mirror `http_stt`). Then:

**qwen3 family — inline HTTP (§3a):**
1. POST §3a with base64 of the WAV bytes and `asr_options.language` (from arg/config; omit → auto), `enable_lid: true`.
2. Parse `output.choices[0].message.content[0].text` (defensive: missing/empty → "").
3. `httpx.HTTPError` → `translate_httpx_error(self.name, exc)`.

**fun-asr family — one-shot native WS (§3c):** no inline HTTP endpoint exists, so run a single `FunAsrNativeStream`: `run-task` → send all PCM as binary frames → `finish-task` → concatenate finalized (`sentence_end:true`) sentence texts. No file hosting.

Both return `STTResult(engine="qwencloud", text=…, is_final=True, confidence=None)`.

## 6. Realtime path — two `STTStream` classes (chosen by family)
Both wrap a `websockets` connection + a reader task draining server events into an `asyncio.Queue`; `accept`/`finalize` pull results from the queue; **lazy connect** on first `accept`. Because `open_stream` is overridden, `list_engines` auto-reports `realtime: true`. Use `websockets.connect(url, additional_headers={"Authorization": …})` (websockets ≥14 uses `additional_headers`, not `extra_headers`).

**`QwenOaiRealtimeStream` (qwen3, §3b):** header `Bearer`. On connect send `session.update`. `accept(pcm)`: send `input_audio_buffer.append` with **base64** PCM; drain queue — `…text` → partial `STTResult(text=stash, is_final=False)`, `…completed` → final `STTResult(text=transcript, is_final=True)`. `finalize()`: send `session.finish`; drain to `session.finished`.

**`FunAsrNativeStream` (fun-asr, §3c):** header `bearer`. On connect send `run-task` (uuid task_id) and await `task-started` before sending audio. `accept(pcm)`: send **raw binary** PCM frames; drain queue — `result-generated` sentence `sentence_end:false` → partial (`text`), `sentence_end:true` → final (`text`). `finalize()`: send `finish-task`; drain to `task-finished`; return accumulated final. 

Both: on mid-stream disconnect, `finalize` returns the best partial seen — never raise hard.

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
1. `_family`: `qwen3-asr-flash*`→qwen3, `fun-asr*`→funasr.
2. `transcribe_bytes` (qwen3): mock httpx multimodal-generation → assert body carries base64 audio + model + `asr_options`; parse text from `output.choices[0].message.content[0].text`; empty/missing → "".
3. `transcribe_bytes` (fun-asr): mock `FunAsrNativeStream` → assert one-shot run-task/finish-task and concatenation of `sentence_end:true` texts.
4. `QwenOaiRealtimeStream`: fake WS source → feed `…text {stash}` then `…completed {transcript}`; assert partial(`is_final=False`, stash) / final(`is_final=True`, transcript) and `finalize` sends `session.finish`.
5. `FunAsrNativeStream`: fake WS source → feed `result-generated` `sentence_end:false` then `true`; assert partial/final mapping, that audio is sent as binary frames, that `run-task` precedes audio and `finalize` sends `finish-task`.
6. Registry resolution: unconfigured → clear RuntimeError; configured entry → correct host/path/model/realtime_model per family (pattern: `test_http_stt_provider.py`, `test_stt_remote_registry.py`).
7. `list_engines`: `qwencloud` present, `mode="remote"`, `realtime=True`, `configured` reflects key presence.
8. WS-disconnect: `finalize` returns last partial without raising (both stream classes).

## 10. Files touched
- **New**: `app/services/stt/providers/qwencloud_provider.py`
- **New**: `tests/unit/test_qwencloud_stt_provider.py`
- **Edit**: `app/services/stt/service.py` (register + `list_engines` branch)
- **Edit**: `apps/api_gateway/pyproject.toml` (`websockets` direct dep)
- **Verify**: admin registry entry form exposes `config` for language/turn_detection/realtime_model
