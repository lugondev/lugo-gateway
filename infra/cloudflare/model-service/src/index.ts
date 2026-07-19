import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  MODEL_SERVICE: DurableObjectNamespace<ModelServiceContainer>;
  // Set via `wrangler secret put SERVICE_API_TOKEN` -- never hardcoded here
  // or in wrangler.jsonc, and never committed. See ../README.md.
  SERVICE_API_TOKEN: string;
}

// infra/docker/Dockerfile.model_service serves one engine, picked by
// SERVICE_KIND/SERVICE_ENGINE at container start -- same env-driven design
// the Fly.io deploy (fly.toml [env]) and docker-compose use. This class is
// the "vieneu" instance of the template; see wrangler.jsonc's header
// comment for how to stand up a second engine.
export class ModelServiceContainer extends Container<Env> {
  defaultPort = 8100;
  // Model weights are cached in the container's ephemeral disk, so a cold
  // start after sleep re-downloads/re-loads them -- keep this long enough
  // that normal request gaps don't repeatedly pay that cost.
  sleepAfter = "10m";

  // Plain field, not a getter: Container's own constructor does
  // `this.envVars = options.envVars` when constructed with options, which
  // would throw against a getter-only property. `this.env` (the Worker's
  // secret binding) is already set by the DurableObject base constructor
  // by the time this field initializer runs.
  envVars = {
    SERVICE_KIND: "tts",
    SERVICE_ENGINE: "vieneu",
    SERVICE_PORT: "8100",
    SERVICE_API_TOKEN: this.env.SERVICE_API_TOKEN,
  };

  override onStart() {
    console.log(`[${this.envVars.SERVICE_ENGINE}] container started`);
  }

  override onStop(params: { exitCode: number; reason: string }) {
    console.log(
      `[${this.envVars.SERVICE_ENGINE}] container stopped: ${params.reason} (exit ${params.exitCode})`,
    );
  }

  override onError(error: unknown) {
    console.error(`[${this.envVars.SERVICE_ENGINE}] container error:`, error);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Stateless model server, not per-session state -- every request routes
    // to the same single container instance (kept warm for `sleepAfter`,
    // cold-started otherwise). No per-user routing needed here.
    const container = getContainer(env.MODEL_SERVICE, "default");
    return container.fetch(request);
  },
};
