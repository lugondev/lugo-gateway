# QwenCloud STT Engine — Design

**Date:** 2026-07-25
**Status:** Approved design → implementation
**Scope:** Add a new remote STT engine `qwencloud` (Alibaba DashScope Model Studio international) supporting the FunASR and Qwen3-ASR families, with both batch and native real-time (WebSocket) transcription.

## 1. Goal & context

The gateway already has a clean STT provider interface (`app/services/stt/base.py`):

- `STTProvider.transcribe_bytes(audio_bytes, language, model) -> STTResult` — batch, bytes-in/text-out.
- `STTProvider.open_stream(sample_rate, language) -> STTStream` — native realtime; defaults to `BufferingStream` (accumulate PCM, transcribe once on `finalize`). Only `vosk` overrides it today.

Remote engines resolve their config **per call** from the **Model Registry** (`kind="stt"`, `engine`, `model_id`, `base_url`, `api_key`, `config`), so admin edits take effect immediately (pattern: `http_stt_provider.py`). This engine follows that pattern exactly.

QwenCloud = the international product surface of Alibaba DashScope Model Studio. Verified from docs (2026-07-25):

- **Real-time**: OpenAI-Realtime-compatible WebSocket at
  `wss://{workspace_id}.{region}.maas.aliyuncs.com/api-ws/v1/realtime?model={model}`,
  header `Authorization: Bearer <api_key>`. Audio = **PCM16, 16 kHz, mono**, chunks base64 in `input_audio_buffer.append` (~3200 bytes ≈ 100 ms).
- **Batch (async)**: `POST https://dashscope-intl.aliyuncs.com/api/v1/services/audio/asr/transcription` with `X-DashScope-Async: enable` — **only accepts public file URLs** + task polling. Does **not** accept raw uploads.
- **Batch (sync)**: `fun-asr-flash-2026-06-15` supports synchronous multimodal-generation calls with **inline** audio (≤5 min).

## 2. Decisions (locked)

| Question | Decision |
|---|---|
| Mechanisms | **Both** batch + real-time |
| Models | **qwen3-asr-flash** (realtime + batch) and **fun-asr** (realtime + batch). Qwen-Omni excluded. |
| Structure | **One unified** `QwenCloudSttProvider` (model chosen via registry entry) |
| Batch path for raw mic bytes | **Sync inline; fall back to a one-shot realtime-WS session when a model has no sync endpoint** |

**Rationale for the batch decision:** the system's batch contract is bytes-in/text-out with no public file hosting anywhere in the pipeline (every existing cloud provider sends audio inline). The async public-URL API does not fit that contract, so it is **not** implemented here (documented as a future extension, §8). Inline-sync + WS-one-shot fallback guarantees every selected model has a working batch path with zero new infrastructure.

## 3. Provider shape

New file: `app/services/stt/providers/qwencloud_provider.py`

```
class QwenCloudSttProvider(STTProvider):
    name = "qwencloud"

    def __init__(self, name="qwencloud", timeout_seconds=60.0, entry=None): ...
    async def _resolve_entry(self, model) -> dict | None      # registry lookup, like http_stt
    async def transcribe_bytes(audio_bytes, language, model) -> STTResult
    def open_stream(sample_rate, language) -> QwenCloudStream  # native realtime
    def available(self) -> bool                                # ≥1 enabled entry with api_key
    def detail(self) -> str
```

Registry `entry.config` fields (all optional, with defaults):

- `region`: `"ap-southeast-1"` (default) | `"cn-beijing"`
- `workspace_id`: required for the realtime WS host; if absent, realtime is unavailable and `open_stream` raises a clear error while batch still works.
- `language`: passthrough to `input_audio_transcription.language` / batch language (None → auto).
- `turn_detection`: `"server_vad"` (default) | `"manual"`.
- `timeout_seconds`: overrides default.

`_entry_override` (constructor `entry=`) supports the registry's test-before-add call, exactly like `http_stt`/`openrouter`.

## 4. Batch path — `transcribe_bytes`

1. Resolve entry → `base_url`/`api_key` via `resolve_credentials(entry)`; error clearly if unconfigured (mirror `http_stt` message).
2. Determine sync capability by model:
   - `fun-asr-flash*` → **sync multimodal endpoint**, inline base64 audio (WAV/PCM), parse transcript text.
   - Any model without a confirmed sync endpoint (initially `qwen3-asr-flash`) → **fallback**: run a single `QwenCloudStream` in **manual** turn-detection mode: append the whole buffer → `commit` → `finish` → concatenate `.completed` texts. One code path, no file hosting. (An implementation task verifies whether qwen3-asr-flash has a real sync endpoint against a live key; if yes, prefer it and drop the fallback for that model.)
3. Return `STTResult(engine="qwencloud", text=..., is_final=True, confidence=None)`.
4. HTTP errors → `translate_httpx_error(self.name, exc)`.

A small internal map `_SYNC_BATCH_MODELS` (prefix match) decides sync-vs-fallback, so adding a newly-confirmed sync model is a one-line change.

## 5. Real-time path — `QwenCloudStream(STTStream)`

State: an aiohttp/`websockets` connection + a reader task draining server events into an `asyncio.Queue`; `accept`/`finalize` pull finished results from the queue.

- **Lazy connect** on first `accept` (needs `workspace_id`; raise a clear error if missing). Send `session.update`:
  `{modalities:["text"], input_audio_format:"pcm", sample_rate:16000, input_audio_transcription:{language}, turn_detection: server_vad{silence_duration_ms:400} | null}`.
- **`accept(pcm)`**: audio is already PCM16/16 kHz/mono (matches WS requirement — no resample). Send `input_audio_buffer.append` with base64 PCM. Drain any queued events: `conversation.item.input_audio_transcription.text` → partial (`is_final=False`); `.completed` → final (`is_final=True`). Return the list produced this call.
- **`finalize()`**: in manual mode send `input_audio_buffer.commit`; send `session.finish`; await `session.finished`; return the last final (or concatenation) as one `STTResult`; close the socket. If the socket dropped mid-stream, return the best partial seen instead of raising.
- Because `open_stream` is overridden, `STTService.list_engines` auto-reports `realtime: true`.

Dependency: reuse whatever WS client is already vendored (`websockets` — confirm in `pyproject`); if none, add `websockets`.

## 6. Wiring

- `service.py`: add `"qwencloud": QwenCloudSttProvider(timeout_seconds=remote_stt.remote_stt_timeout_seconds)` to `self.providers`. No entry in `reinit_remote_providers` needed (resolves per-call). Add a `list_engines` branch: `mode="remote"`, `available` = an enabled `qwencloud` entry exists whose resolved credentials carry an `api_key` (same loop as `qwen3_asr_or`/`whisper_or`), `detail` = model id or region.
- `model_catalog.py`: **no** `STT_MODEL_CATALOGS` entry (model lives in the registry row, not a process-global — same as `http_stt`).
- Admin UI: reuse the existing add-remote-engine + Model Registry entry flow. Verify the registry entry form exposes the `config` JSON (region / workspace_id / turn_detection); if not, note the gap (do not expand scope here).

## 7. Error handling

- Missing entry / empty key / empty base host → `RuntimeError` with an actionable "add a Model Registry entry…" message (mirror `http_stt`).
- Missing `workspace_id` on realtime → raise only from `open_stream`/first `accept`, with a message pointing at the entry's `config.workspace_id`. Batch still works.
- 401/403 (bad key) → surfaced via `translate_httpx_error` (HTTP) or a clear WS-handshake error.
- WS disconnect mid-session → `finalize` returns the last partial, never a hard crash.

## 8. Out of scope (future extensions)

- Async public-URL file-transcription (`fun-asr` / `qwen3-asr-flash-filetrans`) with task polling + timestamps/diarization — needs a file-hosting capability the pipeline doesn't have. Document only.
- Qwen-Omni family.
- Emotion metadata: the realtime stream returns emotion; we pass through text only for now (a `confidence`/metadata extension can carry it later).

## 9. Testing (TDD)

1. `transcribe_bytes` (fun-asr-flash): mock httpx sync endpoint → assert request body carries base64 audio + model; parse `text`.
2. `transcribe_bytes` fallback (qwen3-asr-flash): mock `QwenCloudStream` → assert one-shot manual session assembles final text.
3. `QwenCloudStream`: fake WS event source → feed `.text` then `.completed`; assert partial/final `STTResult` mapping and that `finalize` sends commit+finish.
4. Registry resolution: unconfigured → clear RuntimeError; configured entry → correct host/region/model (pattern: `test_http_stt_provider.py`, `test_stt_remote_registry.py`).
5. `list_engines`: `qwencloud` present with `mode="remote"`, `realtime=True`, `configured` reflecting key presence.
6. WS-disconnect: `finalize` returns last partial without raising.

## 10. Files touched

- **New**: `app/services/stt/providers/qwencloud_provider.py`
- **New**: `tests/unit/test_qwencloud_stt_provider.py`
- **Edit**: `app/services/stt/service.py` (register + `list_engines` branch)
- **Maybe**: `pyproject.toml` (`websockets` dep if not already present)
- **Verify**: admin registry entry form exposes `config` for region/workspace_id
