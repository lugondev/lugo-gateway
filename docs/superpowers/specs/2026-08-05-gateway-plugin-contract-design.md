# Gateway Plugin Contract, with Livehost as the First Plugin — Design

## Purpose

Turn feature modules into out-of-process plugins that register with the
gateway, and prove the contract by moving Livehost (TikTok Live AI co-host)
out of `apps/api_gateway` into its own repository and process.

The motivation is four-fold, and each part is load-bearing:

- **Independent deploy.** Livehost ships and restarts on its own cadence.
- **Its own repository.** A separate release cycle, packaged like the existing
  `servers/*` services.
- **Dirty dependency containment.** `TikTokLive` is an unofficial,
  reverse-engineered client that breaks whenever TikTok changes its protocol.
  It has no business living inside the gateway process.
- **Readability.** `api/routes/livehost.py` is 663 lines doing turn
  orchestration, quota checks, TTS pacing and usage recording in one file.

Out of scope for this design: porting `lugo.py` (own spec, after this contract
has carried real traffic); running Livehost as more than one replica (see
*Known limitations*); converting `recommend.py` or `memories.py` into plugins;
replacing the existing MCP server registry.

## Background: what the codebase already provides

Three findings shaped this design. Each one removed work rather than adding it.

**1. The Livehost package is already decoupled.** All of
`app/services/livehost/` — `ingestor.py`, `tiktok_adapter.py`, `scheduler.py`,
`orchestrator.py`, `registry.py`, `schemas.py`, ~500 lines — imports nothing
but stdlib, pydantic, and its own siblings. `TikTokLive` is already an optional
extra (`[tiktok]`). The entire coupling to the gateway (21 distinct modules)
sits in the single file `api/routes/livehost.py`.

**2. The host API already exists.** `WS /v1/conversation/stream` accepts audio
frames *or* a `{"type":"text"}` control message and returns paced audio plus a
JSON event stream. Its control protocol is `text`, `abort`, `reset`,
`new_session`, `flush`, `end`; its event stream includes `speech_start`,
`speech_end`, `processing`, `turn_done`, `user_transcript`, `response_text`,
`audio_start`, `audio_end`, `aborted`, `error`. Behind it,
`ConversationSession` already performs STT, endpointing, LLM, TTS with
prefetch and pacing, quota gating, usage recording, history persistence and
memory injection.

This interface was never designed as a plugin API. It is the interface the
gateway grew for ESP32 devices and the web client. Livehost and Lugo are
simply its third and fourth consumers — which is the strongest available
evidence that the boundary is real rather than invented.

**3. The registry pattern is already generic.** `SqliteBackedStore[T]` is
parameterized and backs three stores today: `ProfileStore`, `TtsProfileStore`
and `McpServerStore`. (`ModelRegistryStore` and `SystemConfigStore` are
hand-rolled and share only the `invalidate()` convention.) `McpServer` —
`name`, `owner_id`, `url`, `headers`, `enabled` — is within one field of what
a plugin record needs. The four services under
`servers/` are integrated purely through runtime configuration; the gateway
does not reference them in code at all.

## Architecture

```
Browser (livehost UI)
   │  1. POST /v1/plugins/ticket {"plugin":"livehost"} → {url, token, expires_in}
   │  2. WS <plugin url>/v1/livehost/stream?ticket=…   mic ↑  audio + events ↓
   ▼
servers/livehost-api                      ← new repo, owns the TikTokLive dep
   ├─ ingest/tiktok.py ─► scheduler ─► orchestrator  (arbitration)
   ├─ auth.py           → POST /api/auth/introspect  (once per connection)
   └─ upstream.py       → WS /v1/conversation/stream?output=audio,text
         │   mic bytes, passed through unchanged
         │   {"type":"text"}   social turn, formatted
         │   {"type":"abort"}  streamer barges in
         ▼
   api_gateway
     STT · endpointer · LLM · TTS + pacing · quota · usage · history ·
     memory injection · profile resolution · auth
```

Livehost keeps **zero** of its 21 former gateway imports — not because they
were wrapped, but because each one is already performed inside the
`ConversationSession` the gateway runs on its behalf.

## Components

### Plugin record and registry

A fourth instance of `SqliteBackedStore[T]`, modelled directly on `McpServer`:

```python
class PluginMount(BaseModel):
    path: str                            # "/v1/livehost/stream"
    kind: Literal["ws", "http"]
    public: bool = True                  # browser connects directly

class Plugin(BaseModel):
    name: str                            # "livehost"
    owner_id: str | None = None
    url: str                             # "https://livehost.internal:8091"
    secret: str                          # the plugin's credential for calling back
    enabled: bool = True
    kind: Literal["feature", "tools"] = "feature"
    mounts: list[PluginMount] = []
```

`url` is validated for an `http`/`https` scheme exactly as `McpServer.url` is.
`kind` exists so the MCP server registry can later fold into this one; nothing
in this design depends on that happening.

`secret` runs plugin → gateway, the opposite direction from `McpServer.headers`,
because in this design the gateway never calls the plugin — the browser does.
It is masked for non-admin readers exactly as `mcp.py::_view` masks `headers`.

New route module `api/routes/plugins.py`, mirroring `api/routes/mcp.py`:
`GET|POST /v1/plugins`, `GET|PUT|DELETE /v1/plugins/{name}`, plus
`POST /v1/plugins/ticket`. `GET /v1/plugins` is what the web client reads to
decide which feature tabs to render.

**Why the ticket route is not `/v1/plugins/{name}/ticket`.** `/v1/plugins` is
an admin prefix, and the two routes users must reach — listing plugins and
minting a ticket — are carve-outs inside it. `auth_guard._USER_EXACT` matches
exactly and by method precisely to stop a path parameter from shadowing an
admin handler (its comments document this as bug class M1). A carve-out cannot
be written for a parameterized path, so the plugin name moves into the request
body and the route becomes a fixed string:

```python
_ADMIN_PREFIXES += ("/v1/plugins",)
_USER_EXACT["/v1/plugins"]        = frozenset({"GET", "HEAD"})
_USER_EXACT["/v1/plugins/ticket"] = frozenset({"POST"})
```

`GET|PUT|DELETE /v1/plugins/{name}` would route `name="ticket"`, which is why
the carve-out is restricted to `POST` — and no `POST /v1/plugins/{name}` route
exists, so `POST /v1/plugins/ticket` can only ever reach the ticket handler.

### Ticket issuance and introspection

Access tokens today carry a user id and nothing else — `auth_guard.py` states
plainly that a role claim is never read because it does not exist. A plugin
ticket is therefore a new token kind, not a new claim on the existing one.

`tokens.py` signs with `itsdangerous.URLSafeTimedSerializer` under a salt per
token kind — `lugo-access` and `lugo-refresh` — so that access and refresh
tokens share a secret but can never be used for each other. Audience binding
therefore needs no new claim; it is the salt:

```python
PLUGIN_TICKET_TTL_SECONDS = 60

def _plugin_salt(plugin: str) -> str:
    return f"lugo-plugin:{plugin}"

def issue_plugin_token(user_id: str, plugin: str) -> str
def verify_plugin_token(token: str, plugin: str) -> str | None   # → user_id
```

A ticket minted for `livehost` fails signature verification under any other
plugin's salt. The verifier names the plugin it expects, which every plugin
knows about itself, so the check cannot be forgotten.

The 60-second TTL is short because a ticket is redeemed immediately on
connect: browsers cannot set headers on a WebSocket handshake, so the ticket
travels as a query parameter and lands in access logs. It buys one connection,
not a session — the connection itself lives as long as the socket.

`POST /v1/plugins/ticket` takes `{plugin}`, requires a normal authenticated
caller, checks the plugin exists and is enabled, and returns
`{url, token, expires_in}`.

`POST /api/auth/introspect` takes `{token, plugin}` and returns
`{active, user_id}`. The plugin calls it once when a connection opens,
authenticating with its own `Plugin.secret` as a bearer.

**Why introspection rather than local verification:** verifying signatures in
the plugin would mean distributing the gateway's session secret, and any
holder of that secret can mint tokens for arbitrary users. Introspection costs
one round trip per connection, off the audio hot path, and keeps the signing
key inside the gateway.

**Why introspection is authenticated:** `/api/auth` sits in
`_NO_AUTH_PREFIXES` — it must, since login lives there. An unauthenticated
introspect would therefore turn any ticket into a `user_id` lookup for anyone
who could read it, and per the paragraph above, tickets are written to access
logs by design. Requiring `Plugin.secret` closes that. If plugin count or
connection rate ever makes the round trip hurt, the upgrade is asymmetric
signing plus a published JWKS — a change confined to `tokens.py` and the
plugin's `auth.py`.

### Host API v1

The contract a plugin may depend on, and nothing else:

| Capability | Endpoint | Status |
|---|---|---|
| Voice turn loop (STT, LLM, TTS, pacing, quota, usage, history, memory) | `WS /v1/conversation/stream` | exists |
| Identity from a ticket | `POST /api/auth/introspect` | **new** |
| Profile and TTS-profile listing for the plugin's own UI | `GET /v1/profiles`, `GET /v1/tts_profiles` | exists |

Plugins must not import gateway internals. That rule is the whole point: the
21-import status quo is what makes an edit to `turn_tts.py` able to break
Livehost, and moving the file to another repository without the rule would
preserve the breakage while adding a network hop.

### `servers/livehost-api`

Packaged like `servers/knowledge-api`: own `pyproject.toml`, own `Dockerfile`,
own `docker-compose.yml`, a `livehost doctor` / `livehost serve` CLI that
refuses to start on a failing doctor.

```
src/livehost/
  ingest/tiktok.py     ← ingestor.py + tiktok_adapter.py, verbatim
  scheduler.py         ← verbatim
  orchestrator.py      ← verbatim
  registry.py          ← verbatim
  schemas.py           ← verbatim
  upstream.py          ← new: the conversation/stream client
  auth.py              ← new: ticket → user_id via introspect
  api/ws.py            ← rewritten from routes/livehost.py
  api/control.py       ← connect / disconnect / status
  cli.py               ← doctor | serve
  static/              ← livehost.js and its page
```

The WS handler after the port does four things:

1. Accept, introspect the ticket, resolve `user_id`.
2. Open the upstream socket with the user's own token:
   `WS /v1/conversation/stream?output=audio,text&profile=…&tts_profile=…`.
3. Run two pumps. Downstream: upstream audio bytes and JSON events relayed to
   the browser unchanged. Upstream: browser bytes and control messages relayed
   up unchanged.
4. Run the social loop: `ingestor` → `scheduler` →
   `orchestrator.poll_social_turn(voice_active)` → `{"type":"text"}` upstream.

`voice_active` is derived from the upstream event stream — `speech_start` sets
it, `turn_done` clears it — rather than from a locally owned endpointer. When
the streamer barges in on a social turn, the handler sends `{"type":"abort"}`.

Expected size: roughly 250 lines, down from 663, because STT resolution,
endpointing, quota gating, usage recording, TTS synthesis, prefetch and pacing
are not reimplemented anywhere.

### Gateway subtractions

At cutover the gateway loses: the `livehost_router` import and
`include_router` call in `main.py`, the `/v1/livehost` prefix in
`core/auth_guard.py`, `app/services/livehost/`, `app/schemas/livehost.py`,
`app/static/js/livehost.js`, the eight `livehost_*` fields in
`core/settings.py`, and the `[tiktok]` extra in `pyproject.toml`.

## Data flow: a social turn

1. `TikTokLiveIngestor` normalizes a room event into a `SocialEvent`.
2. `EventScheduler` batches and prioritizes it.
3. The social loop polls `orchestrator.poll_social_turn(voice_active)`. With
   `voice_active` true, it returns `None` and the event waits.
4. Otherwise `format_social_turn` renders `[TikTok @user]: …` and the handler
   sends `{"type":"text", "text": …}` upstream.
5. The gateway runs a full turn: quota gate, LLM, sentence segmentation, TTS
   with prefetch, paced audio frames, usage recorded, history written.
6. Audio bytes and events arrive on the upstream socket and are relayed
   straight to the browser.
7. If `speech_start` arrives mid-turn, the handler sends `{"type":"abort"}`.

## Error handling

**Upstream disconnect.** The TikTok connection is expensive to establish and
carries its own backoff; it must survive an upstream drop. On disconnect the
handler reconnects to `conversation/stream` with the previous `?session_id=`
so history stays continuous, while the ingestor keeps running unaware. Events
accumulate in the scheduler's bounded queue during the gap and are subject to
its existing overflow behaviour.

**Ticket rejected.** `introspect` returning `active: false` closes the browser
socket with 4401, matching what `resolve_ws_identity` does today.

**Plugin unreachable.** `POST /v1/plugins/ticket` does not probe the
plugin; a dead plugin surfaces as a failed browser connection. `GET
/v1/plugins` reports stored state only. Health probing is deliberately absent
from v1 — the gateway already has `services/model_registry/health_probe.py`
and it can be reused later if the need is demonstrated rather than assumed.

**Room offline.** Unchanged: `RoomOfflineError` and the existing offline
polling interval, now inside the plugin.

## Testing

Eleven existing test modules cover Livehost today — eight under
`tests/unit/livehost/` and three under `tests/integration/`. They have four
distinct fates, and sorting them correctly matters more than it looks: only
five of the eleven follow the code to the new repository.

**Move verbatim (5).** `test_livehost_schemas.py`,
`test_livehost_ingestor.py`, `test_livehost_tiktok_adapter.py`,
`test_livehost_scheduler.py`, `test_livehost_orchestrator.py`. These exercise
the zero-dependency package and pass in the new repository without edits —
the cheapest available proof that the boundary was drawn where the code
already agreed it was.

**Retarget and keep in the gateway (2).** `test_livehost_tts_profile.py` and
`test_livehost_disabled_cutoff.py` build the gateway app directly and stub
STT/TTS providers. The behaviour they guard — TTS profile resolution
precedence, and cutting off a session whose account was disabled mid-stream —
becomes `conversation/stream` behaviour after the port. It does not stop
mattering, so these are rewritten against that socket and stay. Letting them
leave with the code would silently drop two real guarantees.

**Retire, with one replacement (1).** `test_livehost_quota_gate.py` does not
survive contact with the port, in three different ways. Two of its tests
exercise livehost's `_quota_blocked_for`, a thin wrapper over the shared
`llm_turn_quota_blocked_for_pins` that `tests/unit/conversation/
test_turn_quota.py` already covers directly — keeping them would be keeping a
duplicate. The third reads the livehost route module's source and asserts both
turn functions reach the gate; it becomes vacuous when livehost runs no turns.

What that third test uniquely held is still worth holding, though, and it
moves rather than evaporating. `ConversationSession._run_turn` gates once per
turn *above* the branch between audio and text input, so an injected social
turn cannot bypass the gate — but nothing asserted that for the text path, and
the text path is exactly what the plugin now depends on. One new test in
`test_turn_quota.py` pins the ordering.

**Rewrite plugin-side (3).** `test_livehost_authz.py` changes auth model from
`resolve_ws_identity` to ticket plus introspection.
`test_livehost_ws_social.py` and `test_livehost_ws_voice.py` become
integration tests against a fake upstream that speaks the
`conversation/stream` protocol.

That fake is the contract's executable specification. If the gateway's real
socket and the fake drift apart, the contract has been broken silently — so
the gateway keeps a matching test asserting its socket still satisfies the
same script. Two tests, one script, one on each side of the boundary.

New gateway tests cover ticket issuance (audience binding, expiry, disabled
plugin, unauthenticated caller) and introspection.

## Migration

Four steps, each shippable on its own.

1. **Gateway grows the contract.** `Plugin`, `PluginStore`,
   `api/routes/plugins.py`, `issue_plugin_token` / `verify_plugin_token`,
   `POST /api/auth/introspect`. Livehost remains in-process and untouched. The
   suite stays green.
2. **New repository.** Move the five zero-dependency modules and their tests
   verbatim. Write `upstream.py`, `auth.py`, `cli.py`, the Dockerfile and the
   rewritten WS handler. Test against the fake upstream.
3. **Cut over.** Register the plugin, point the UI at `GET /v1/plugins`,
   retarget the three gateway-side tests onto `conversation/stream`, then
   perform the gateway subtractions listed above. Retargeting comes before
   the subtractions so the guarantees are never unguarded, not even for one
   commit. This is the only irreversible step.
4. **Lugo.** Its own spec, once the contract has carried real traffic.

## Known limitations

**Single replica.** `livehost_registry` is a process-global dict, and the
three control endpoints (`connect`, `disconnect`, `status`) resolve a session
through it. Running more than one Livehost replica breaks them: a control call
may land on a replica that does not hold the session. Independent scaling was
one of the four motivations for this design, and this is the part of it that
v1 does **not** deliver. It is recorded here rather than discovered later.
Resolving it means either sticky routing keyed on `session_id`, or moving the
registry into a shared store — a decision better made against a real
multi-replica requirement.

**Two network hops for audio.** Streamer microphone travels
browser → livehost → gateway, and synthesized audio returns
gateway → livehost → browser. This was accepted deliberately when choosing a
thin-client plugin over a shared-library extraction: it buys zero code
duplication at the cost of one relay. If measured latency becomes a problem,
the alternative on the table is extracting the turn machinery into an
installable package that both processes import.

**The gateway is a hard runtime dependency.** Livehost cannot run without it.
Selling or deploying Livehost standalone means shipping the gateway too. This
follows directly from the thin-client choice and is not a defect of the
implementation.
