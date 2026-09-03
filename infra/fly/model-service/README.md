# Model service on Fly.io

Deploys `apps/model_service` (see `apps/model_service/README.md`) as a Fly.io
app, using the same image as the Cloudflare Containers template
(`infra/cloudflare/model-service/`): `infra/docker/Dockerfile.model_service`.

Currently configured for `SERVICE_ENGINE=vieneu` (`app = 'lugo-vieneu-tts'`),
deployed and end-to-end verified against `lugo-vieneu-tts.fly.dev`.

## Why the deploy command needs `--config` + an explicit context

`fly.toml`'s `dockerfile` field
(`infra/docker/Dockerfile.model_service`) is resolved relative to the Docker
build **context**, which Fly sets to the project root by default — and
"project root" here means the directory `fly deploy` is run from, not the
directory `fly.toml` lives in. Since this `fly.toml` no longer sits at the
repo root, always deploy from the repo root with both flags:

```bash
cd /Users/lugon/code/lugo-gateway
export FLY_API_TOKEN=...   # never commit this
fly deploy . --config infra/fly/model-service/fly.toml
```

`.` pins the build context to the repo root (so `COPY pyproject.toml
README.md ./` / `COPY apps ./apps` in the Dockerfile keep resolving
correctly); `--config` points at the relocated `fly.toml`. Other `flyctl`
commands that need this app's config (`fly status`, `fly logs`, `fly secrets
set`, ...) take the same `--config infra/fly/model-service/fly.toml` flag;
most of them don't care about CWD/build-context the way `deploy` does.

## Deploy a second engine

Copy this directory (e.g. `infra/fly/model-service-qwen3-tts/`), edit `app`
and `[env]` in the copied `fly.toml`, then deploy the same way with `--config`
pointed at the copy. `omnivoice` isn't deployable this way yet — see the
"Why `vieneu`, not `omnivoice`" note in
`infra/cloudflare/model-service/README.md` (same root cause, independent of
which platform hosts the container).
