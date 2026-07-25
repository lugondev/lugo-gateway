# QwenCloud fun-asr-mtl Async Batch (multilingual) — Design

**Date:** 2026-07-25
**Status:** Approved design → implementation
**Scope:** Add an async file-transcription batch path to the `qwencloud` STT engine so the **multilingual** fun-asr models (`fun-asr-mtl`, `fun-asr`, `fun-asr-flash`) work for the batch transcribe endpoint (`/v1/stt/transcribe`, the admin STT tab). Fixes the reported problem that fun-asr transcribes Vietnamese as Chinese.

## 1. Problem (verified live 2026-07-25)
- The current fun-asr batch runs a one-shot over the **realtime** WS with `fun-asr-realtime`, which is **Chinese-centric**: a Vietnamese clip returns Chinese (`新朝…`) regardless of `language_hints` (auto/en/vi/zh all Chinese).
- `fun-asr-mtl`/`fun-asr-mtl-realtime` are **not** realtime WS models (`Model not found`). The **multilingual** fun-asr (`fun-asr-mtl`, 30 languages) lives only on the **async file-transcription API** (needs a file URL) — this is what QwenCloud's own UI uses, hence its correct auto-detected Vietnamese.
- DashScope provides a built-in temporary file host (`getPolicy` → OSS upload → `oss://` + `X-DashScope-OssResourceResolve`), so **no external storage is needed**. Full flow verified end-to-end: a VN clip returned `"Xin chào, hôm nay trời rất đẹp và tôi muốn đi dạo công viên."` via `fun-asr-mtl`.

## 2. Locked decisions

**Guiding principle (user):** a model whose id ends in `-realtime` is a **conversation/streaming** model (WS); a model **without** `-realtime` is a **batch** model. Batch never uses a realtime model.

Applied to qwencloud:

| Family | Batch model (`transcribe_bytes`, STT tab) | Conversation model (`open_stream`, streaming) |
|---|---|---|
| qwen3-asr | `qwen3-asr-flash` → inline HTTP | `qwen3-asr-flash-realtime` → OpenAI-realtime WS |
| fun-asr | `fun-asr-mtl` (multilingual) → **async** | `fun-asr-realtime` → native WS (Chinese) |

1. **Batch = non-realtime model.** In `transcribe_bytes`, the batch model is the effective model with any `-realtime` suffix stripped (`_batch_model()`), so a mistakenly-configured realtime model still routes to a batch model. qwen3 batch → inline; fun-asr batch → **async** (§3).
2. **Remove the one-shot-WS batch.** The old fun-asr batch ran a one-shot over the realtime WS with `fun-asr-realtime` (Chinese-only — the reported bug). It is obsolete: delete `_funasr_batch` and its only consumer `drain_remaining_finals`. fun-asr batch is now always async/multilingual. (`FunAsrNativeStream` stays — used by streaming.)
3. **Conversation unchanged:** `open_stream` still uses the realtime model (`config.realtime_model` or default `-realtime` variant). No streaming behavior changes.
4. **Auto-detect:** the async submit sends **no** `language_hints` (pure auto-detect — the verified-correct behavior; QwenCloud UI does the same). A future opt-in hint is out of scope.
5. **Polling** feels near-realtime but is rate-limit-safe: initial 0.8s delay, then poll every 1s for the first 20s, then every 2s, until `SUCCEEDED`/`FAILED` or `max_wait` (default 180s; overridable via entry `config.timeout_seconds`).
6. **No external infra:** use DashScope's `getPolicy`+OSS temporary upload; files are private, expire ~5 min.

## 3. Verified API flow (all against host from entry base_url, default `dashscope-intl.aliyuncs.com`)
1. **getPolicy:** `GET {host}/api/v1/uploads?action=getPolicy&model={model}`, `Authorization: Bearer` → `data: {policy, signature, upload_dir, upload_host, oss_access_key_id, x_oss_object_acl, x_oss_forbid_overwrite, expire_in_seconds, max_file_size_mb}`.
2. **OSS upload:** `POST {upload_host}` multipart form — fields `OSSAccessKeyId`, `Signature`, `policy`, `key={upload_dir}/audio.wav`, `x-oss-object-acl`, `x-oss-forbid-overwrite`, `success_action_status=200`, and `file` → HTTP 200. Resource = `oss://{key}`.
3. **submit:** `POST {host}/api/v1/services/audio/asr/transcription`, headers `Authorization: Bearer`, `X-DashScope-Async: enable`, `X-DashScope-OssResourceResolve: enable`, `Content-Type: application/json`, body `{"model": model, "input": {"file_urls": [oss_url]}}` → `{output: {task_id, task_status: "PENDING"}}`.
4. **poll:** `POST {host}/api/v1/tasks/{task_id}`, `Authorization: Bearer` → `{output: {task_status: PENDING|RUNNING|SUCCEEDED|FAILED, results: [{transcription_url, subtask_status, ...}]}}`. On FAILED, surface `output` message.
5. **fetch transcript:** `GET transcription_url` (a signed OSS URL) → JSON `{file_url, properties, transcripts: [{text, ...}]}`. Result text = `"".join(t["text"] for t in transcripts)` (strip).

## 4. Implementation
In `qwencloud_provider.py`:
- Constants: `_FUNASR_ASYNC_PATH = "/api/v1/services/audio/asr/transcription"`, `_UPLOAD_PATH = "/api/v1/uploads"`, `_TASKS_PATH = "/api/v1/tasks"`.
- `def _batch_model(model: str) -> str`: strip a trailing `-realtime` (`qwen3-asr-flash-realtime`→`qwen3-asr-flash`, `fun-asr-realtime`→`fun-asr`, `fun-asr-mtl`→`fun-asr-mtl`).
- `async def _funasr_async_batch(self, base_url, api_key, model, audio_bytes, max_wait) -> STTResult`: implements §3 (getPolicy → OSS upload → submit → poll → fetch) via `httpx.AsyncClient`, host from `_host_base(base_url)`. Adaptive poll (§2.5). Returns `STTResult(engine="qwencloud", text=…, is_final=True)`.
- **`transcribe_bytes`** funasr branch becomes (no more one-shot WS):
  ```
  bm = _batch_model(effective)
  return await self._funasr_async_batch(base_url, api_key, bm, audio_bytes, max_wait)
  ```
  where `max_wait = cfg["timeout_seconds"] if set else 180`.
- **Delete** `_funasr_batch` and `drain_remaining_finals` (dead once the one-shot batch is gone). Keep `FunAsrNativeStream` (streaming) and its `finalize`/error-surfacing.
- Errors → `RuntimeError` (via `translate_httpx_error` for HTTP; explicit for upload!=200, task `FAILED` with its message, poll timeout, missing `transcription_url`) so the route shows a clear STT-failed message.
- **qwen3 batch** also uses `_batch_model(effective)` (so a realtime id configured for batch still resolves to the inline model) — a one-line consistency tweak in the qwen3 branch.

## 5. UI / selection
No UI change needed: the row-based STT picker already lists `qwencloud|fun-asr-mtl` from `/v1/model_registry/options?kind=stt`. Operator creates a registry entry `engine=qwencloud, model_id=fun-asr-mtl` (via provider or dropdown) and picks it in the STT tab. `list_engines` detail already lists configured model ids.

## 6. Out of scope
- Streaming for fun-asr-mtl (async-only; realtime stays with realtime models).
- Explicit language forcing for the async path (auto-detect only).
- Timestamps/diarization from the transcript JSON (only `text` is used).
- Reusing DashScope uploads across requests / caching.

## 7. Testing
- Unit `_batch_model`: `qwen3-asr-flash-realtime`→`qwen3-asr-flash`; `fun-asr-realtime`→`fun-asr`; `fun-asr-mtl`→`fun-asr-mtl`; `qwen3-asr-flash`→`qwen3-asr-flash`.
- Unit `_funasr_async_batch` with mocked httpx (`httpx.MockTransport`): assert the 5-step sequence — getPolicy GET, OSS multipart POST (carries the file + `key`), submit POST (headers `X-DashScope-Async`+`X-DashScope-OssResourceResolve`, `oss://` url, **no** language_hints), poll POST (PENDING→SUCCEEDED), transcript GET → text = joined `transcripts[].text`. Mirror `test_http_stt_provider.py`'s MockTransport style.
- Unit: task `FAILED` → RuntimeError; poll timeout → RuntimeError; OSS upload non-200 → RuntimeError.
- Unit `transcribe_bytes` dispatch: `fun-asr-mtl` AND `fun-asr-realtime` (as model_id) both → async path (mock `_funasr_async_batch`; the latter proves `_batch_model` strips `-realtime`); `qwen3-asr-flash` → inline.
- Unit: the removed `_funasr_batch`/`drain_remaining_finals` no longer referenced (delete their tests; keep the `FunAsrNativeStream` streaming tests).
- Regression: existing qwencloud + stt route tests still pass.

## 8. Files touched
- `apps/api_gateway/app/services/stt/providers/qwencloud_provider.py`
- `tests/unit/test_qwencloud_stt_provider.py`
