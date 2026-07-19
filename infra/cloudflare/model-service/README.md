# Model service on Cloudflare Containers (template)

Deploys `apps/model_service` (see `apps/model_service/README.md`) as a
Cloudflare Worker + [Container](https://developers.cloudflare.com/containers/),
reusing the same image as the Fly.io deploy
(`infra/docker/Dockerfile.model_service`) — no Cloudflare-specific Dockerfile.

**Status: deployed and end-to-end verified** (2026-07-19, account "Zzitorez",
`https://lugo-model-service-vieneu.zzitorez.workers.dev`) — `/health` returns
`{"status":"ok","kind":"tts","engine":"vieneu"}`, `POST /v1/audio/speech`
returns a real WAV (PCM16 mono 48kHz, matching vieneu's sample rate), and an
unauthenticated request to the same endpoint correctly gets `401`. Warm-request
latency for a short Vietnamese sentence: ~7.2s (batch, non-streaming API —
expected, not comparable to the gateway's own streaming TTS path). Two
version pins in `package.json` were wrong on first install and had to be
corrected against what npm actually publishes:
`@cloudflare/containers` is `^0.3.7`, not `^1.0.0`; `wrangler@4.112.0`
requires `@cloudflare/workers-types@^5`, not `^4` — a reminder that this
product's docs and its published packages can drift, so re-verify before
trusting version numbers here on a future deploy.

## What's here

- `wrangler.jsonc` — one Worker + one Container (`ModelServiceContainer`)
  bound through a Durable Object (Cloudflare's required routing layer for
  Containers). Builds `../../docker/Dockerfile.model_service` with the repo
  root as build context (`image_build_context`), same files the Fly.io
  deploy and `docker compose --profile models` use.
- `src/index.ts` — the Worker. Routes every request to a single warm
  container instance (`sleepAfter = "10m"`) running `SERVICE_KIND=tts`,
  `SERVICE_ENGINE=vieneu`. `SERVICE_API_TOKEN` is read from a Worker secret,
  never hardcoded.

## Why `vieneu`, not `omnivoice`

`apps/model_service/README.md`'s Limits section already rules out
`SERVICE_ENGINE=omnivoice` for this container: `OmnivoiceConfig.omnivoice_path`
defaults to a hardcoded developer-machine path with no env override, so the
container refuses to serve it — a gap in `apps/model_service` itself, not
something Cloudflare-specific. Making OmniVoice deployable here needs that env
override added first (`apps/api_gateway/app/services/system_config.py` +
`apps/model_service/app/config.py`), independent of which platform hosts the
container. `vieneu` (ONNX int8, in-process) is what this template deploys,
matching the already-verified `lugo-vieneu-tts` Fly.io deploy.

Cloudflare Containers are CPU-only (no GPU instance type as of this writing;
see https://developers.cloudflare.com/containers/platform-details/limits/) —
fine for `vieneu`, same as the CPU-only Fly.io deploy.

## Deploy

Requires a Cloudflare account on the **Workers Paid plan** (Containers need
it) and `SERVICE_API_TOKEN` chosen ahead of time.

```bash
cd infra/cloudflare/model-service
npm install
npx wrangler login                      # once, opens a browser
npx wrangler secret put SERVICE_API_TOKEN   # paste the token; not stored in this repo
npx wrangler deploy
```

First deploy builds and pushes the image (can take a few minutes — it's the
same multi-hundred-MB image as the Fly.io build, containing all of
`apps/api_gateway` + `apps/model_service` and their ML deps). Subsequent
`wrangler dev`/`deploy` reuse layers.

## Deploy a second engine

Copy this directory (e.g. `infra/cloudflare/model-service-qwen3-tts/`) and
change:

1. `wrangler.jsonc`: `name` (must be globally unique per Cloudflare account)
   and, if the engine needs more than `vieneu`'s CPU/memory footprint,
   `instance_type` (see
   https://developers.cloudflare.com/containers/platform-details/limits/ for
   the `basic`/`standard-1..4` tiers).
2. `src/index.ts`: `envVars.SERVICE_KIND`/`SERVICE_ENGINE` in
   `ModelServiceContainer`.

Then deploy the copy the same way (its own `wrangler secret put
SERVICE_API_TOKEN`, its own `wrangler deploy`).

## Wiring into the gateway

Same as any other model-service deployment — add a Model Registry entry
pointing at the deployed Worker's URL. See
`apps/model_service/README.md#wiring-into-the-gateways-model-registry`:

- Kind: `tts`, Engine: `openai_tts`
- `base_url`: `https://<worker-name>.<your-subdomain>.workers.dev/v1`
  (or your custom domain, if one is routed to this Worker)
- `api_key`: the exact value set via `wrangler secret put SERVICE_API_TOKEN`

Saving the entry triggers the gateway's test-before-add call, so a saved
entry is already proof the Worker answered over HTTPS with the token
accepted.

## Known unknowns (verify before relying on this)

- Cold-start latency after `sleepAfter` (10m) is not precisely measured —
  the first `/health` call after deploy took a bit longer than a warm one,
  but that could equally have been `workers.dev` DNS/edge propagation for a
  brand-new subdomain rather than container cold start. Worth a real
  measurement (kill the container, time the next request) before relying on
  this for latency-sensitive use.
- No CI/health-check wiring yet (the Docker image's own `HEALTHCHECK` is
  unused by Cloudflare's platform, which has its own instance health model).
- Not yet wired into the gateway's Model Registry — the deployed URL above
  is verified reachable and working standalone, but no `openai_tts` entry
  points at it yet (see "Wiring into the gateway" above).
