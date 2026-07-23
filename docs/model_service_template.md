# Adding a new STT engine — the standard template

This project exposes every speech-to-text engine two ways, and a new engine should
plug into both without touching the others:

1. **In-process** — a `STTProvider` subclass the gateway calls directly. Lowest
   latency, keeps warm state, thread-pinning, streaming.
2. **As an OpenAI-compatible model_service** — the same provider wrapped by
   `apps/model_service` and served at `POST /v1/audio/transcriptions`. The gateway
   consumes it through `HttpSttProvider` (engine `http_stt`) using the registry
   `base_url`. This is how CPU-only / GPU-isolated engines run in their own container
   in prod. (Named `http_stt`/`http_tts`, not `remote_stt`/`remote_tts` — that name is
   already taken by `RemoteSttConfig`'s fixed `whisper_service`/`eventlab` slots.)

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
- The gateway consumes it via a registry row for `http_stt` pointing `base_url` at
  the container. No gateway code change beyond the registry row.
- If the engine needs native build steps (like the GGUF binary), gate them in
  `infra/docker/Dockerfile.model_service` behind a build ARG so other engines'
  images stay slim. See `BUILD_QWEN3_ASR_GGUF`.
- A CMake-built binary needs `make` installed alongside `cmake`/`g++` (CMake's
  default Unix Makefiles generator shells out to it; `cmake --build` fails with
  "CMAKE_MAKE_PROGRAM is not set" without it) and should end with
  `cmake --install <build-dir>` so the binary lands on `PATH` (e.g.
  `/usr/local/bin`) instead of only being reachable via the provider's
  build-dir fallback path. Only works if the project's `CMakeLists.txt` declares
  an `install(TARGETS ...)` rule — check before relying on it.

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

## Deploying a model_service engine to Coolify

Each engine gets its own one-service compose file in `infra/compose/` (e.g.
`docker-compose.stt-qwen3-asr-gguf.yml`) built from the shared
`Dockerfile.model_service` — see `docker-compose.vm.yml` for running several
engines together on one plain SSH+docker-compose VM instead.

Gotchas hit standing these up (in case they resurface):

- **`build.context` must be `.`, not `../..`.** Coolify invokes `docker compose`
  with `--project-directory` set to the app's `base_directory` (repo root), which
  becomes the resolution base for every relative `build.context` — not the
  compose file's own directory. `../..` (correct for a bare `docker compose -f
  infra/compose/x.yml build` with no `--project-directory` override) overshoots
  by two levels under Coolify and fails with `lstat /infra: no such file or
  directory`. Building the same file locally the way Coolify does needs
  `docker compose -f infra/compose/<file> --project-directory . build` from the
  repo root.
- **`POST /api/v1/applications/private-github-app` hangs → nginx 504** for this
  repo specifically (confirmed reproducible on Coolify 4.0.0-beta.459), rolling
  back with no app created. Workaround: `POST
  /api/v1/applications/private-deploy-key` instead — add an SSH deploy key to the
  repo (`gh api repos/<owner>/<repo>/keys`), register the private half via `POST
  /api/v1/security/keys`, and pass its `private_key_uuid`. This path returns in
  well under a second.
- **Submodules break the deploy-key path.** Coolify runs `git submodule update
  --init --recursive` unconditionally whenever `.gitmodules` exists, using the
  app's one configured SSH key for the whole clone. A deploy key is scoped to a
  single repo (GitHub rejects reusing one public key as a deploy key across
  repos), so any submodule outside that repo fails to clone and aborts the whole
  deployment — even if the submodule is irrelevant to the image being built.
  Fix: point the Coolify app at a `deploy` branch that has the submodule gitlinks
  and `.gitmodules` stripped (`git rm --cached <path>...` + `git rm --cached
  .gitmodules`), regenerated from `main` before each deploy. (The GitHub App
  path doesn't have this problem — its installation already covers every repo
  under the account — which is the real reason to prefer fixing the 504 over
  living with the deploy-key workaround long-term.)
- **`ports_exposes` doesn't drive the Traefik label port for `dockercompose`
  builds** the way it does for `dockerfile`/`dockerimage` builds — PATCHing it
  and redeploying did not change the generated
  `loadbalancer.server.port` label (stayed `80` even with `ports_exposes=8100`).
  Adding an explicit `expose:` entry in the compose service didn't fix it either
  in testing. Unresolved: the public sslip/Traefik URL for a `dockercompose` app
  may 404 even while the container itself is `running:healthy`. Treat the public
  domain as unreliable for these apps for now; call the service from another
  Coolify app on the same server over the internal `coolify` Docker network
  instead of through the public URL.
- **A `dockercompose` app's Traefik domain (`docker_compose_domains`) needs both
  a domain assignment AND a redeploy to route** — PATCHing the domain alone
  doesn't regenerate the running container's labels. `docker_compose_domains`
  itself is only PATCH-able as an *array*, and any PATCH to a dockercompose app
  requires resending the full base64 `docker_compose_raw` in the same request
  or the API rejects it — annoying enough that redeploying via the UI/API
  `POST /deploy` after setting the domain is simpler than fighting the PATCH
  shape.
- **`custom_docker_run_options` volumes (`-v name:path`) do not reliably
  survive a `dockerfile`-buildpack app's redeploy**, even though the field is
  API-accepted and looks identical to a normal `docker run -v` flag. Confirmed
  by symptom: every redeploy of the `api` app re-triggered
  `_bootstrap_admin_if_needed()`'s "no users yet" path (see `app/main.py`) and
  the Model Registry lost every admin-added row (config-sentinel rows survived
  since those get re-seeded, not stored per-admin-action) — i.e. the SQLite
  file at the declared mount path was empty on every boot, not actually
  persisted. Coolify's own `persistent_storages` field is API-blocked (`PATCH
  .../applications/{uuid} {"persistent_storages": [...]}` → 422 "This field is
  not allowed", same shape as the `dockerfile_location` UI-only quirk above) —
  no API path exists to configure this correctly. Fix that actually worked:
  Coolify UI → app → **Storages** tab → **+ Add → Volume Mount** (name +
  destination path only, leave Source Path blank), one entry per volume,
  **and remove the matching `-v` lines from Custom Docker Options** first (both
  declaring the same destination path caused container start issues). Verified
  by redeploying twice after switching: admin-added registry rows and the
  bootstrap-admin user both survived, and the "no users yet" log line stopped
  appearing.
- **Static JS/CSS served through a Cloudflare-proxied domain (orange-cloud
  DNS, not DNS-only) gets edge-cached independent of app redeploys** — a
  `system-status.js` fix stayed invisible for hours after a successful deploy
  (`cf-cache-status: HIT`, `cache-control: max-age=14400`) purely because nothing
  purged the CDN cache; the origin was already serving the new file the whole
  time. `wrangler` (Workers CLI) has no zone/cache-purge command and is a
  separate Cloudflare account/permission scope from the DNS zone anyway.
  Confirm proxy status is the actual cause via response headers (`server:
  cloudflare`, `cf-cache-status`) before assuming it's a deploy problem; fix is
  a manual Cache Purge in the Cloudflare dashboard for that zone (no API path
  available without a zone-scoped token). Also: no bundler/build step serves
  these files, and the ES modules import each other by fixed relative URL
  (`main.js` → `./system-status.js` → ...), so cache-busting only the
  top-level `<script src>` tag would NOT invalidate the individually-cached
  submodule URLs underneath it — real cache-busting here would need every
  import site versioned, not just the entry point.

## Why not move every engine into model_service?

Considered and rejected for a blanket rewrite. The in-process path keeps latency low
and preserves warm state, MLX thread-pinning, and streaming — moving the existing
GPU/MLX engines behind HTTP would sacrifice all of that. The model_service surface is
the right home for engines that must run in an isolated container (CPU-only builds,
GPU pinning, heavyweight native deps). `qwen3_asr_gguf` is registered **both** ways
on purpose: it is the reference for how a single provider serves both surfaces.
