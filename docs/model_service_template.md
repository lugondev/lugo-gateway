# Adding a new STT engine — the standard template

This project exposes every speech-to-text engine two ways, and a new engine should
plug into both without touching the others:

1. **In-process** — a `STTProvider` subclass the gateway calls directly. Lowest
   latency, keeps warm state, thread-pinning, streaming.
2. **As an OpenAI-compatible model_service** — the same provider wrapped by
   `apps/model_service` and served at `POST /v1/audio/transcriptions`. The gateway
   consumes it through `OpenAICompatSttProvider` (engine `openai_stt`) using the
   registry `base_url`. This is how CPU-only / GPU-isolated engines run in their own
   container in prod.

You write the provider once; both surfaces reuse it. There is **no separate HTTP
handler to write** — model_service builds its `/v1` route from the provider you
register.

The reference non-Python example is `qwen3_asr_gguf` (shells out to the
`qwen3-asr-cli` binary instead of importing a module). Read it alongside this doc:
`apps/api_gateway/app/services/stt/providers/qwen3_asr_gguf_provider.py`.

---

## Checklist

Names below use `<engine>` = your snake_case engine id (e.g. `qwen3_asr_gguf`).

### 1. Provider — `apps/api_gateway/app/services/stt/providers/<engine>_provider.py`

Subclass `STTProvider` (`app/services/stt/base.py`). Implement:

- `name` = `"<engine>"` (class attribute, must equal the registry key).
- `available() -> bool` — cheap probe. Return `False` when the package/binary/model
  is missing so the engine **auto-hides** instead of erroring. Never raise here.
- `detail() -> str` — one-line human status (model name, chip). Shown in the UI.
- `transcribe_bytes(audio_bytes, language=None, model=None) -> STTResult` — the
  work. Return `STTResult(engine=self.name, text=..., is_final=True, confidence=None)`.
- `warm() -> None` — build/probe the model handle off the request path. Cheap no-op
  is fine if there is no persistent handle.
- `open_stream(...)` — only if the engine streams; otherwise inherit the base.

Conventions the existing engines follow (copy them):

- **Config** comes from `resolve_stt_engine_config("<engine>")`, never hard-coded.
- **Heavy/serialized work** runs in a module-level
  `concurrent.futures.ThreadPoolExecutor(max_workers=1, ...)` submitted via
  `loop.run_in_executor(...)`. MLX engines use this to pin to one thread; the GGUF
  engine uses it to serialize subprocess launches. Match this shape so the registry
  is uniform.
- **Binary lookup** (non-Python engines): precedence config path → `shutil.which` →
  default build dir, mirroring `_ollama_bin()` in `llm_models.py`. See
  `resolve_qwen3_asr_gguf_binary()`.

### 2. Config defaults — `app/services/model_registry/resolve.py`

Add an entry to `STT_ENGINE_CONFIG_DEFAULTS["<engine>"]` with every tunable key and
its default. Resolution precedence is **registry sentinel row (`model_id=""`) > env
`STT_<ENGINE>_<KEY>` > this default**. Keep values here, not in the provider.

### 3. Register in the service — `app/services/stt/service.py`

- Import the provider and add it to the `providers` dict: `"<engine>": XProvider(),`.
- Add a branch in `list_engines()` that reports `mode` (`"local"` or `"remote"`),
  `available`, and `detail`. Local engines gate on `provider.available()`; do not
  fall through to the remote-registry branch (that assumes a `base_url`).

### 4. Request schema — `app/schemas/stt.py`

Add `<engine>` to the `STTRequest.engine` regex `pattern`. (Easy to forget — the API
rejects the engine name with a 422 until you do.)

### 5. Recommender — so it shows up in "recommended engines"

- `app/services/recommend/capabilities.py`: add a `bool` dataclass field, include it
  in `has()`'s named dict, add a `_<flag>()` probe, and set it in the
  `Capabilities(...)` constructor.
- `app/services/recommend/catalog.py`: add a `Candidate(...)` with category `"stt"`,
  the right `chip` (`cpu`/`apple`/`cuda`), `vietnamese` flag, and `requires=[...]`
  listing the capability flag(s). The `_config(...)` note should tell the user how to
  install/build it.

### 6. model_service (OpenAI-compatible container)

Nothing engine-specific to write in the HTTP layer — it wraps the provider. To ship
the engine as its own container:

- Run it locally: `SERVICE_KIND=stt SERVICE_ENGINE=<engine>` against
  `apps/model_service`, then `curl` `POST /v1/audio/transcriptions` (multipart:
  `file=@sample.wav`, `model=<engine>`) and confirm the OpenAI envelope
  (`{"text": "..."}`).
- The gateway consumes it via a registry row for `openai_stt` pointing `base_url` at
  the container. No gateway code change beyond the registry row.
- If the engine needs native build steps (like the GGUF binary), gate them in
  `infra/docker/Dockerfile.model_service` behind a build ARG so other engines'
  images stay slim. See `BUILD_QWEN3_ASR_GGUF`.

### 7. Tests

- Unit (`tests/unit/test_<engine>_model.py`): fake the module/binary and assert
  availability gating, config resolution, and the transcribe call shape. No real
  model download.
- Integration (`tests/test_<engine>.py`): assert the engine is registered in
  `stt_service.providers`, appears in `list_engines()` with the right `mode`, is in
  the recommend `CANDIDATES`, and that `STTRequest(engine="<engine>")` validates.

Run the **changed repo's** tests before pushing (this repo auto-deploys `main` to
prod). Full suite is a pre-commit gate, not for static-UI edits.

---

## Why not move every engine into model_service?

Considered and rejected for a blanket rewrite. The in-process path keeps latency low
and preserves warm state, MLX thread-pinning, and streaming — moving the existing
GPU/MLX engines behind HTTP would sacrifice all of that. The model_service surface is
the right home for engines that must run in an isolated container (CPU-only builds,
GPU pinning, heavyweight native deps). `qwen3_asr_gguf` is registered **both** ways
on purpose: it is the reference for how a single provider serves both surfaces.
