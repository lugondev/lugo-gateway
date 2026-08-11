"""Protocol-neutral conversation core.

``ConversationSession`` owns everything between "connection accepted" and
"connection closed" EXCEPT wire encoding. It talks to the outside world through
two callbacks handed in by the front-end:

* ``emit(event, **payload)`` — a neutral JSON event (name + payload keys)
* ``emit_audio(opus_packet)`` — a single binary Opus packet

Both the ``{"event": ...}`` WebSocket route and the future Lugo device route drive
this same core; only the callbacks differ. The front-end resolves engines/params
from its transport (query params, handshake, …) and passes them in via
``SessionRuntimeConfig``; the core receives already-resolved values.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.audio import (
    pcm16_to_wav_bytes,
    preprocess_pcm16,
    wav_bytes_to_pcm16,
    wav_duration_seconds,
)
from app.core.errors import AppError
from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.conversation.announce import generate_line
from app.services.conversation.background import spawn_background
from app.services.conversation.endpointer import (
    VadEndpointer,
    barge_in_suppressed,
    build_endpointer,
)
from app.services.conversation.llm_config import resolve_llm_config
from app.services.conversation.responder import (
    build_responder_ex,
    resolve_system_prompt,
)
from app.services.conversation.tools.base import ToolContext, ToolRegistry, ToolSource
from app.services.conversation.tools.local import LocalToolSource
from app.services.conversation.tools.mcp import McpToolSource
from app.services.conversation.turn_quota import llm_turn_quota_blocked
from app.services.conversation.turn_stream import stream_reply
from app.services.conversation.turn_usage import record_llm_turn_usage
from app.services.history.store import session_store
from app.services.mcp.pool import mcp_pool
from app.services.mcp.server_store import mcp_server_store
from app.services.memory.extractor import memory_extractor
from app.services.memory.retriever import inject_memories, memory_retriever
from app.services.model_registry.store import model_registry_store
from app.services.profile_visibility import visible_profile_or_none
from app.services.profiles.store import profile_store
from app.services.stt.model_catalog import resolve_default_stt_model
from app.services.stt.routing import select_stt_engine
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.service import tts_service
from app.services.tts.streaming import pacing_delays
from app.services.usage.attribution import resolve_usage_model
from app.services.usage.recorder import record_usage
from app.services.warmup import is_ready, warm_providers

logger = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]  # emit(event: str, **payload)
EmitAudioFn = Callable[[bytes], Awaitable[None]]  # emit_audio(opus_packet: bytes)


async def _build_tool_registry(profile, can_hang_up: bool = False) -> ToolRegistry | None:
    """Merge global + per-profile MCP servers (profile wins on name collision),
    skip disabled entries, and fetch each enabled server's tools.

    Global rows are filtered to owner_id is None (server-managed/template
    rows) before merging. Only admins can create/update/enable/clone mcp_server
    rows (see routes/mcp_servers.py's `_require_admin`), so today every row is
    ownerless -- but that wasn't always true, and a legacy owner-scoped row
    left enabled from before that authz work would otherwise have its tools
    (and header secrets, and invocation URL) injected into every OTHER user's
    turn, not just its owner's. profile.mcp_servers is a separate, already
    admin-gated field (Task 3) and is intentionally NOT filtered here -- it is
    per-profile by construction, not globally broadcast."""
    global_servers = {
        name: srv for name, srv in mcp_server_store.list().items() if srv.owner_id is None
    }
    profile_specific = {s.name: s for s in (profile.mcp_servers if profile else [])}
    merged_servers = {**global_servers, **profile_specific}

    tool_sources: list = []
    # Two independent switches. `conversation_tools_enabled` gates the optional
    # utilities (get_time, device_command). end_conversation is not optional in the
    # same sense: without it a device told to say goodbye says goodbye and keeps
    # listening, so it rides on whether this transport can hang up at all.
    if settings.conversation_tools_enabled or can_hang_up:
        tool_sources.append(
            LocalToolSource(
                utilities=settings.conversation_tools_enabled,
                end_conversation=can_hang_up,
            )
        )
    # Concurrently, and under one deadline. This ran a `for` loop of awaits, so N
    # configured servers cost N round trips end to end -- all of it sitting
    # between "socket accepted" and `session_started`, i.e. before the user may
    # speak. get_tools() already swallows an unreachable server (returns []), but
    # it has no bound on a server that ACCEPTS and then just never answers: one
    # of those held every new conversation open indefinitely.
    enabled_servers = [srv for srv in merged_servers.values() if srv.enabled]
    if enabled_servers:
        try:
            fetched = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        mcp_pool.get_tools(srv.url, headers=srv.headers)
                        for srv in enabled_servers
                    ),
                    return_exceptions=True,
                ),
                timeout=settings.mcp_tool_discovery_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "mcp tool discovery exceeded %ss; starting the session without MCP tools",
                settings.mcp_tool_discovery_timeout_seconds,
            )
            fetched = [[] for _ in enabled_servers]
        for srv, tools in zip(enabled_servers, fetched):
            if isinstance(tools, BaseException):
                logger.warning("MCP server %s tool listing failed: %s", srv.url, tools)
                continue
            if tools:
                tool_sources.append(
                    McpToolSource(
                        tools,
                        invoker=lambda n, a, u=srv.url, h=srv.headers: mcp_pool.invoke(u, n, a, headers=h),
                    )
                )
    return ToolRegistry(tool_sources) if tool_sources else None


def _tail(messages: list[dict]) -> list[dict]:
    """The last N messages -- what the model is actually sent.

    A conversation now runs for as long as its client keeps coming back, with no
    time window, so the transcript is unbounded. The PROMPT must not be: replaying
    an ever-growing history on every turn spends more and more per turn until the
    context window gives out. The full transcript stays in the DB for History;
    what falls off the back is what the memory system is for."""
    limit = system_config_store.get().conversation.conversation_history_max_messages
    return messages[-limit:] if limit > 0 else messages


# Body in conversation/background.py, together with the shutdown drain that is
# the other half of the same contract. Kept under the private name this module
# has always used, so the call sites below (and the comments explaining why they
# use it) do not all have to move at once.
_spawn_background = spawn_background


@dataclass
class SessionRuntimeConfig:
    session_id: str
    profile_name: str | None
    # resolved engines / params (already computed by the front-end)
    stt_engine: str
    language: str | None
    tts_engine: str
    voice: str | None
    ref_audio_path: str | None
    ref_text: str | None
    tts_instruct: str | None
    tts_speed: float | None
    tts_language: str | None
    sample_rate: int
    output_sample_rate: int
    audio_codec: str  # "pcm16" | "opus" (input)
    want_audio: bool
    want_text: bool
    audio_out: str  # "wav" | "opus"
    denoise: bool
    resume_sid: str | None  # requested_sid, for history resume
    stt_model: str = ""  # optional model-variant override (SttConfig.model, resolve_stt's 3rd value)
    tts_model: str = ""  # optional registry-row selector within tts_engine (TTSRequest.model_id)
    # The authenticated WS caller's user id (resolve_ws_identity's identity.user_id),
    # if any. This -- never profile.owner_id -- is what a created session is
    # recorded under: the session belongs to whoever is actually speaking, not
    # the profile's owner. None when auth is disabled (dev mode) or the caller
    # used the legacy shared device_auth_token; in that case the session is
    # created ownerless (there is no real owner to attribute it to), not
    # attributed to the named profile's owner.
    identity_user_id: str | None = None
    # True ONLY for the dev-mode short-circuit (WsIdentity.unauthenticated,
    # auth_guard.py -- settings.auth_enabled is False). start() below uses it
    # the same way ws_session_owner_denied does: fully unscoped, since there
    # is no way to prove ownership of anything in that mode. False (the
    # default) for the legacy shared device_auth_token, which also has
    # identity_user_id=None but IS a real, auth-enabled deployment -- that
    # case must still only ever resolve a template profile (owner_id=None),
    # never someone else's private one.
    identity_unauthenticated: bool = False
    # Which client this conversation belongs to (sessions.source / .client_id).
    # "device" + devices.id for a speaker, "web" + the user id for a browser. Both
    # blank means no provenance: the session is still recorded, but it is never
    # implicitly resumed and never resumes anything -- there is nothing to say
    # whose thread it would be.
    source: str = ""
    client_id: str = ""
    # Per-connection override of Opus playback pacing. None (the default, and
    # the only value api/routes/lugo.py ever produces) means "not specified,
    # inherit system_config.conversation.conversation_opus_pace" -- so
    # ESP32/RPi sessions are byte-for-byte unaffected by this field's
    # existence. api/routes/conversation.py (web) sets it from the
    # `opus_pace` query param so the web client can disable the ~300ms
    # real-time drip-feed sized for embedded ring buffers, without touching
    # the global default devices rely on. See
    # docs/superpowers/specs/2026-07-28-web-audio-jitter-buffer-design.md.
    opus_pace: bool | None = None
    # Caller-supplied persona text that replaces profile.system_prompt for
    # this session only -- never persisted, never touches the profile row.
    # Lets a plugin (e.g. livehost) offer its own "system prompt" field
    # without requiring the caller to have edit access to a gateway profile.
    # base_context (admin-configured guardrails) still always applies on top
    # of it, same as it does for a profile's own system_prompt -- see
    # resolve_system_prompt.
    persona_override: str | None = None


class ConversationSession:
    def __init__(self, cfg: SessionRuntimeConfig, emit: EmitFn, emit_audio: EmitAudioFn) -> None:
        self.cfg = cfg
        self.emit = emit
        self.emit_audio = emit_audio

        # Mutable copies of transport params that may be downgraded in start()
        # (e.g. opus -> pcm16/url when the server lacks libopus).
        self.audio_codec = cfg.audio_codec
        self.audio_out = cfg.audio_out

        # Set in start().
        self.profile = None
        self.stt_provider = None
        self.stt_model_id: str | None = None
        self.tts_provider = None
        self.opus_decoder = None
        self.opus_encoder = None
        self.responder = None
        self.tool_registry: ToolRegistry | None = None
        self.tool_ctx: ToolContext | None = None
        self.endpointer: VadEndpointer | None = None
        self.history: list[dict] = []
        self.session_ready = True
        self.base_system_prompt: str | None = None

        self.turn = 0
        self.current_turn: asyncio.Task | None = None
        # Monotonic time the assistant started emitting audio for the current
        # turn (None when it isn't speaking). Drives the barge-in grace window
        # in feed_audio so onset echo doesn't abort the turn.
        self._speaking_since: float | None = None
        # Rotation reason parked by request_rotate() while a turn is in flight,
        # consumed once that turn ends. See request_rotate().
        self._rotate_pending: str | None = None
        # A deferred rotation runs detached from the receive loop, so it can
        # otherwise overlap a second `new_session` the loop is handling and both
        # mint a session row. Serialized, the second one finds turn == 0 and is
        # the documented no-op instead.
        self._rotate_lock = asyncio.Lock()
        # Set by close() so a rotation parked behind the last turn can't mint a
        # fresh session row on a connection that is going away.
        self._closing = False
        # "Hang up once the current utterance has been played" -- a reason string,
        # or None. Armed by the idle watchdog and by the end_conversation tool;
        # ACTED ON by the transport's speaking path (lugo.py), never here, so the
        # farewell is spoken to the end before the socket goes. Putting the close
        # in the audio path rather than in the watchdog is what stops the receive
        # loop and the watchdog from racing over the same connection.
        self.close_after_speaking: str | None = None

    def is_turn_active(self) -> bool:
        return bool(self.current_turn and not self.current_turn.done())

    def add_tool_source(self, source: ToolSource) -> None:
        """Add a ToolSource after start(); used to register device MCP tools
        discovered over the WS. Creates the registry if none exists yet.

        Device tools must never be able to clobber a tool the gateway itself
        configured (local/HTTP-MCP): when a registry already exists, any
        device tool whose name collides with an already-registered tool is
        skipped (the pre-existing tool wins) and logged."""
        if self.tool_registry is None:
            self.tool_registry = ToolRegistry([source])
            return
        for tool in source.list_tools():
            if self.tool_registry.get(tool.name) is not None:
                logger.warning(
                    "device mcp: tool '%s' collides with an existing tool, skipping",
                    tool.name,
                )
                continue
            self.tool_registry.add(tool)

    @property
    def output_sample_rate_effective(self) -> int | None:
        """The output sample rate advertised in session_started (None for wav mode)."""
        return self.cfg.output_sample_rate if self.cfg.want_audio and self.audio_out == "opus" else None

    async def start(self) -> None:
        cfg = self.cfg

        # Re-resolve the profile object + LLM config from the profile name. This is
        # deterministic from profile_name (the front-end already emitted any
        # "profile not found" warning during its own query-param resolution).
        #
        # This is the actual choke point for C2 (profile IDOR --
        # docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md):
        # llm_api_key, system_prompt and the tool_registry (-> session_started's
        # active_tools) below are ALL derived from `profile`, so this resolution
        # -- not the route-level one -- is what must not hand back a profile the
        # caller doesn't own. The route (conversation.py/lugo.py/livehost.py)
        # also gates its own connect-time use of the profile (STT/TTS
        # resolution, health check), but does not need to additionally null out
        # `cfg.profile_name` for this to be safe: visible_profile_or_none here
        # re-checks visibility against cfg.identity_user_id independently.
        # bypass=cfg.identity_unauthenticated preserves the pre-existing
        # dev-mode fallback (identity.unauthenticated -> fully unscoped, see
        # SessionRuntimeConfig.identity_unauthenticated's docstring).
        profile = visible_profile_or_none(
            profile_store.get(cfg.profile_name) if cfg.profile_name else None,
            cfg.identity_user_id,
            bypass=cfg.identity_unauthenticated,
        )
        self.profile = profile
        # Same precedence the two route-level resolutions apply -- llm_config.py.
        llm_base_url, llm_api_key, llm_model, system_prompt = await resolve_llm_config(profile)
        # cfg.persona_override wins over the profile's own system_prompt, same
        # precedence relationship a profile already has over the server-wide
        # default (see resolve_system_prompt) -- one more layer, not a
        # parallel path.
        if cfg.persona_override:
            system_prompt = cfg.persona_override
        voice_optimized = bool(profile and profile.voice_optimized)

        self.stt_model_id = cfg.stt_model or resolve_default_stt_model(cfg.stt_engine)
        self.stt_provider = stt_service.get_provider(cfg.stt_engine)
        self.tts_provider = tts_service.get_provider(cfg.tts_engine)

        # Set up Opus decoding if the client negotiated it; fall back to PCM16 with a
        # warning if the server lacks libopus (so the connection still works).
        # Imported inside start(), not at module scope, so tests that
        # monkeypatch app.core.opus.opus_available still take effect -- the
        # helpers read it as their own module global.
        from app.core.opus import make_decoder_or_downgrade, make_encoder_or_downgrade

        if self.audio_codec == "opus":
            self.opus_decoder = make_decoder_or_downgrade(cfg.sample_rate)
            if self.opus_decoder is None:
                self.audio_codec = "pcm16"

        # Set up Opus encoding for pushed audio output (devices). Fall back to wav.
        if cfg.want_audio and self.audio_out == "opus":
            self.opus_encoder = make_encoder_or_downgrade(cfg.output_sample_rate)
            if self.opus_encoder is None:
                self.audio_out = "wav"

        self.responder = await build_responder_ex(
            base_url=llm_base_url,
            api_key=llm_api_key,
            model=llm_model,
            system_prompt=system_prompt,
            voice_optimized=voice_optimized,
            # Same condition the end_conversation tool is wired under, below.
            can_hang_up=cfg.want_audio,
        )

        self.tool_registry = await _build_tool_registry(profile, can_hang_up=cfg.want_audio)

        # Friendly labels for the UI: the Model Registry is the single source of
        # truth every profile/service select reads, so the chat header must show
        # the SAME human label (never the raw engine name or a verbose provider
        # "detail" string). Fall back to the engine name only when no registry
        # row matches (e.g. a built-in engine with no catalogue entry).
        async def _registry_label(kind: str, engine: str, model_id: str) -> str:
            if not engine:
                return "—"
            row = await model_registry_store.find(kind, engine, model_id or "")
            if row and row.get("label"):
                return row["label"]
            # Some engines carry only a model_id="" sentinel; try the enabled row.
            enabled = await model_registry_store.find_enabled(kind, engine)
            if enabled and enabled.get("label"):
                return enabled["label"]
            return engine

        stt_label = await _registry_label("stt", cfg.stt_engine, self.stt_model_id)
        # TTS is chosen as a whole profile in this UI; its friendly name IS the
        # profile name (TtsProfile has no separate nickname field).
        tts_label = cfg.profile_name and (profile.tts.profile_name if profile else "")
        tts_label = tts_label or await _registry_label("tts", cfg.tts_engine, cfg.tts_model)

        model_label = self.responder.model if self.responder.name == "llm" else self.responder.name
        # Friendly LLM label from the registry too, keyed on the profile's LLM row.
        if profile and profile.llm.engine and self.responder.name == "llm":
            llm_label = await _registry_label("llm", profile.llm.engine, profile.llm.model)
        else:
            llm_label = model_label

        conv_cfg = system_config_store.get().conversation
        self.endpointer = build_endpointer(cfg.sample_rate, conv_cfg)
        # Session persistence. No row is written here: the FIRST stored message
        # creates it (see _persist), the way other chat products do it, so a
        # connection nobody speaks into -- a wake with no words, a health probe,
        # a page load -- leaves nothing behind in History.
        #
        # What DOES happen here is picking which conversation this connection is
        # in: an explicitly requested one, else the client's own latest.
        self.history = []
        self.session_ready = True   # nothing has failed; the row may not exist yet
        self._row_exists = False
        # Announcements spoken before any row exists (the "fresh start" line after a
        # rotation). Flushed ahead of the first real message if one ever arrives; a
        # greeting nobody answered never becomes a History entry of its own.
        self._pending_messages: list[tuple[str, str]] = []
        try:
            resumed = None
            if cfg.resume_sid and await session_store.exists(cfg.resume_sid):
                resumed = cfg.resume_sid
            elif not cfg.resume_sid:
                # Implicit resume: continue where THIS client left off. Scoped to
                # (source, client_id), never to the user -- "the user's latest" is
                # what handed a browser the speaker's conversation and handed the
                # speaker (which remembers no id) a new one every single wake.
                latest = await session_store.latest_for_client(cfg.source, cfg.client_id)
                if latest is not None:
                    resumed = latest["id"]
                    cfg.session_id = resumed
            if resumed is not None:
                self.history = _tail(
                    [
                        {"role": m["role"], "content": m["content"]}
                        for m in await session_store.get_messages(resumed)
                    ]
                )
                self._row_exists = True
                # It is live again; close() will re-end it.
                await session_store.reopen(resumed)
        except Exception as exc:  # noqa: BLE001 - session setup must not drop the connection
            logger.warning("session setup failed for %s: %s", cfg.session_id, exc)
            self.history = []
            self.session_ready = False
        self.turn = 0

        active_tools = self.tool_registry.names() if self.tool_registry else []
        # Tell the client up front whether the models it's about to use are still
        # loading, so it can show "warming up, please wait" instead of the user
        # speaking into a cold pipeline and losing the start of their utterance.
        stt_ready = is_ready(self.stt_provider)
        tts_ready = is_ready(self.tts_provider)
        output = [m for m in ("audio", "text") if (cfg.want_audio if m == "audio" else cfg.want_text)]
        await self.emit(
            "session_started",
            session_id=cfg.session_id,
            profile=cfg.profile_name,
            active_tools=active_tools,
            stt_engine=cfg.stt_engine,
            stt_label=stt_label,
            language=cfg.language,
            tts_engine=cfg.tts_engine,
            tts_label=tts_label,
            responder=self.responder.name,
            llm_model=model_label,
            llm_label=llm_label,
            sample_rate=cfg.sample_rate,
            audio_codec=self.audio_codec,
            output=output,
            audio_out=self.audio_out,
            output_sample_rate=self.output_sample_rate_effective,
            stt_ready=stt_ready,
            tts_ready=tts_ready,
        )

        self.base_system_prompt = resolve_system_prompt(
            system_prompt, voice_optimized, can_hang_up=cfg.want_audio
        )
        self.tool_ctx = ToolContext(
            emit_command=self._emit_command,
            language=cfg.language or None,
            # Only where hanging up means something: a device disconnects, a
            # browser tab does not (see ToolContext.request_end).
            end_conversation=self._arm_close_after_speaking if cfg.want_audio else None,
        )

        # Warm TTS (and STT if it supports it, e.g. MLX graph compile) in the background
        # while the user speaks their first turn, so the first turn isn't delayed by a
        # cold model load / compile. Usually a no-op by now: the gateway already warms
        # the default engines eagerly at boot (see app.main.lifespan) — this covers
        # non-default engines picked via query params.
        async def _warm_and_notify() -> None:
            await warm_providers(self.tts_provider, self.stt_provider)
            if not (stt_ready and tts_ready):
                try:
                    await self.emit("engines_ready")
                except Exception:  # noqa: BLE001 - socket may already be closed/gone
                    pass

        # _spawn_background, not a bare create_task: nothing else holds a
        # reference to this task, so CPython is free to collect it mid-flight
        # (the exact hazard _background_tasks exists for -- see its comment).
        _spawn_background(_warm_and_notify())

    async def _emit_command(self, cmd_payload: dict) -> None:
        await self.emit("command", **cmd_payload)

    async def _persist(self, role: str, content: str, *, defer_if_new: bool = False) -> None:
        """Append a message, creating the session row on the first one.

        `defer_if_new` holds the message instead of creating a row for it: used by
        the spoken "fresh start" line, which must not turn an empty conversation
        into a History entry all by itself. Held lines are flushed, in order, ahead
        of the first message that does create the row.
        """
        if not self.session_ready:
            return
        if not self._row_exists:
            if defer_if_new:
                self._pending_messages.append((role, content))
                return
            if not await self._create_row():
                return
        try:
            for pending_role, pending_content in self._pending_messages:
                await session_store.append_message(
                    self.cfg.session_id, self.turn, pending_role, pending_content
                )
            self._pending_messages.clear()
            await session_store.append_message(self.cfg.session_id, self.turn, role, content)
        except Exception as exc:  # noqa: BLE001 - persistence must not kill the turn
            logger.warning("history persist failed: %s", exc)

    async def _create_row(self) -> bool:
        """Write the session row. Called when the conversation first has something
        in it, not when the socket opened."""
        cfg = self.cfg
        try:
            await session_store.create(
                cfg.session_id,
                profile_id=cfg.profile_name or "",
                meta={"stt_engine": cfg.stt_engine, "tts_engine": cfg.tts_engine},
                # No `or profile.owner_id` fallback: a fleet/dev-mode caller
                # (identity_user_id is None) must create an ownerless row, not one
                # silently attributed to the owner of whatever profile name was
                # passed in (H2 -- that let a named profile's owner be billed for /
                # attributed a session they never touched).
                user_id=cfg.identity_user_id,
                source=cfg.source,
                client_id=cfg.client_id,
            )
        except Exception as exc:  # noqa: BLE001 - a failed row must not kill the turn
            logger.warning("session create failed for %s: %s", cfg.session_id, exc)
            self.session_ready = False
            return False
        self._row_exists = True
        return True

    async def _refresh_memory(self, query: str) -> None:
        """Per-turn memory injection (mutates the responder's system prompt)."""
        if not hasattr(self.responder, "system_prompt"):
            return
        try:
            block = await memory_retriever.get_context(self.profile, query=query, user_id=self.cfg.identity_user_id)
            self.responder.system_prompt = inject_memories(self.base_system_prompt, block)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory retrieval failed: %s", exc)

    async def _record_llm_usage(self) -> None:
        """Best-effort usage row for the LLM call(s) in the turn just completed.

        Called AFTER the responder's reply_stream has been fully consumed (only
        then is `.last_usage` -- set as the stream reads its final SSE chunk --
        populated). Thin wrapper (Task 6 dedup) over the shared
        services/conversation/turn_usage helper -- kept as a method since
        test_session_usage_metering.py drives it directly via
        `sess._record_llm_usage()`.
        """
        await record_llm_turn_usage(
            self.responder, identity_user_id=self.cfg.identity_user_id,
            profile=self.profile, profile_name=self.cfg.profile_name,
        )

    async def _record_tts_usage(self, text: str) -> None:
        """Best-effort usage row for one synthesized utterance (billed per char).

        A method rather than the per-turn closure it used to be: speak()'s
        farewell synthesizes outside any turn and must be metered by the same
        shape, and a second copy of this is how one of the two ends up
        forgotten -- which is exactly what happened to speak().
        """
        cfg = self.cfg
        try:
            await record_usage(
                user_id=cfg.identity_user_id or "", profile_id=cfg.profile_name or "",
                kind="tts", engine=cfg.tts_engine, model_id=cfg.tts_model or "",
                unit="chars", native_amount=len(text or ""),
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break a turn
            logger.warning("tts usage metering failed: %s", exc)

    async def _farewell_quota_blocked(self) -> bool:
        """True when an applicable quota is already over its limit for this
        session's TTS engine, in which case speak() skips silently.

        Same shape as MemoryExtractor._quota_blocked, and for the same reason:
        the server initiates this work at teardown and nobody is waiting on it,
        so an over-quota state means skip and log -- never raise, never surface
        an error to the client. Resolving provider_id is wrapped separately so a
        registry hiccup degrades to user/global-scope enforcement instead of
        blocking or crashing; anything other than QuotaExceededError fails open.
        """
        cfg = self.cfg
        usage_engine, usage_model, provider_id = "", "", ""
        try:
            usage_engine, usage_model = await resolve_usage_model(
                "tts", cfg.tts_engine or "", cfg.tts_model or ""
            )
            entry = await model_registry_store.find("tts", usage_engine, usage_model)
            provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
        except Exception:  # noqa: BLE001 - never block on a lookup
            provider_id = ""
        try:
            # function-local: tests monkeypatch app.services.quota.gate.quota_gate by
            # reassigning the module attribute (see test_stt_stream_metering.py's
            # counting_gate); a top-level `from ... import quota_gate` binds the
            # name once at import time and never observes that reassignment.
            from app.services.quota.gate import QuotaExceededError, quota_gate

            await quota_gate(
                user_id=cfg.identity_user_id or "", provider_id=provider_id,
                kind="tts", engine=usage_engine, model_id=usage_model,
                profile_id=cfg.profile_name or "",
            )
        except QuotaExceededError as exc:
            logger.warning(
                "farewell utterance skipped for profile %s: %s", cfg.profile_name or "-", exc
            )
            return True
        except Exception as exc:  # noqa: BLE001 - fail-open, same as quota_gate itself
            logger.warning(
                "farewell quota check failed open for profile %s: %s", cfg.profile_name or "-", exc
            )
        return False

    async def _handle_turn(
        self, audio_pcm: bytes | None = None, text_input: str | None = None, speech_ms: float = 0.0
    ) -> None:
        try:
            await self._run_turn(audio_pcm=audio_pcm, text_input=text_input, speech_ms=speech_ms)
        except Exception as exc:  # noqa: BLE001 - keep the conversation alive
            logger.exception("conversation turn failed")
            await self.emit("error", message=str(exc))
            await self.emit("turn_done", turn=self.turn)

    async def _stream_reply(
        self, sentence_aiter, responder_name: str, *, turn: int, log_first_chunk
    ) -> list[str]:
        """Thin seam onto conversation/turn_stream.py -- see that module."""
        return await stream_reply(
            self, sentence_aiter, responder_name,
            turn=turn, log_first_chunk=log_first_chunk,
        )

    async def _run_turn(
        self, audio_pcm: bytes | None = None, text_input: str | None = None, speech_ms: float = 0.0
    ) -> None:
        cfg = self.cfg

        # Best-effort quota gate: ONCE per turn, before anything else (STT/LLM/TTS).
        # quota_gate() itself is fail-open (internal errors log + allow), so only a
        # genuine over-limit quota raises here. Shared with livehost's/chat()'s own
        # preflight (Task 6 dedup) -- see turn_quota.py's docstring for the pairing
        # rule and fail-open contract.
        blocked, quota_message = await llm_turn_quota_blocked(
            identity_user_id=cfg.identity_user_id, profile=self.profile, profile_name=cfg.profile_name,
        )
        if blocked:
            # A plain "error" notice, then close the turn out without running
            # it. The `turn_done` is not cosmetic: it is the ONLY event the
            # transports treat as "the assistant has stopped" -- lugo.py's emit
            # clears its `speaking` flag and consumes `close_after_speaking`
            # there. Returning bare (what this did) left a device that had
            # already armed a hang-up waiting for a `turn_done` that was never
            # coming, mid-utterance forever. `skipped` marks it as not-real
            # interaction, so refreshes_idle() correctly ignores it.
            await self.emit("error", message=quota_message)
            await self.emit("turn_done", turn=self.turn, skipped="quota")
            return

        self.turn += 1
        turn = self.turn
        turn_start = time.monotonic()
        self._speaking_since = None  # fresh barge-in grace for this turn
        await self.emit("processing", turn=turn)

        # Stage timing so a slow/cold first turn is diagnosable from server logs alone
        # (STT vs LLM+TTS-to-first-chunk vs total) instead of guessing whether the
        # delay is server-side or network/device-side.
        def _elapsed_ms() -> float:
            return (time.monotonic() - turn_start) * 1000

        first_chunk_logged = False

        def _log_first_chunk() -> None:
            nonlocal first_chunk_logged
            if not first_chunk_logged:
                first_chunk_logged = True
                logger.info("turn %d: first response chunk at +%.0fms", turn, _elapsed_ms())

        async def _stream_to_tts(sentence_aiter, responder_name: str) -> list[str]:
            return await self._stream_reply(
                sentence_aiter, responder_name, turn=turn, log_first_chunk=_log_first_chunk
            )

        async def _answer(user_text: str, log_note: str = "") -> None:
            """Everything a turn does once it has a non-empty user utterance.

            The two input shapes below (typed text, transcribed speech) differ
            only in how they GET that utterance -- from there the work is
            identical: history, memory, the responder stream, metering,
            persistence, turn_done. It was written out twice, so a change to
            the turn tail (a new metering call, the history cap) only ever
            landed on one of the two input paths.
            """
            self.history.append({"role": "user", "content": user_text})
            self.history = _tail(self.history)
            await self._persist("user", user_text)
            await self._refresh_memory(user_text)
            parts = await _stream_to_tts(
                self.responder.reply_stream(
                    self.history,
                    registry=self.tool_registry,
                    ctx=self.tool_ctx,
                    max_iters=settings.conversation_tool_max_iters,
                ),
                self.responder.name,
            )
            await self._record_llm_usage()
            self.history.append({"role": "assistant", "content": " ".join(parts)})
            self.history = _tail(self.history)
            await self._persist("assistant", " ".join(parts))
            logger.info("turn %d: done at +%.0fms%s", turn, _elapsed_ms(), log_note)
            await self.emit("turn_done", turn=turn)

        # Text input: skip STT, reply via the text responder (text→text / text→audio).
        if text_input is not None:
            user_text = (text_input or "").strip()
            await self.emit("user_transcript", turn=turn, text=user_text, engine="text")
            if not user_text:
                await self.emit("turn_done", turn=turn, skipped="empty text")
                return
            await _answer(user_text, " (text input)")
            return

        # Audio input: build the wav for STT.
        pcm = audio_pcm
        if cfg.denoise:
            # On a worker thread: spectral noise reduction over a whole
            # utterance is real numpy work (tens to hundreds of ms for a 10-30s
            # turn), and run inline it stalls the event loop -- i.e. every OTHER
            # live connection's audio, not just this one's. The Opus encode and
            # decode either side of this already went to_thread for exactly this
            # reason; this call was the one that didn't.
            pcm = await asyncio.to_thread(
                preprocess_pcm16,
                audio_pcm, cfg.sample_rate, True, False,
                system_config_store.get().preprocessing.stt_noise_reduce_amount,
            )
        wav = pcm16_to_wav_bytes(pcm, sample_rate=cfg.sample_rate)

        # Fast-path routing: short commands can go to a lower-latency engine.
        turn_provider = self.stt_provider
        turn_engine = cfg.stt_engine
        turn_model = self.stt_model_id
        conv_cfg = system_config_store.get().conversation
        if speech_ms and conv_cfg.conversation_fast_stt_engine:
            chosen = select_stt_engine(
                speech_ms,
                cfg.stt_engine,
                conv_cfg.conversation_fast_stt_engine,
                conv_cfg.conversation_fast_stt_max_ms,
            )
            if chosen != cfg.stt_engine:
                try:
                    turn_provider = stt_service.get_provider(chosen)
                    turn_engine = chosen
                    turn_model = None  # different engine — this session's model pin doesn't apply
                except AppError:
                    logger.info("fast STT engine %s unavailable; using %s", chosen, cfg.stt_engine)

        try:
            stt_result = await turn_provider.transcribe_bytes(wav, cfg.language, model=turn_model)
        except RuntimeError as exc:
            # turn_done for the same reason the quota branch above emits one:
            # without it the transport never learns the turn ended.
            await self.emit("error", message=f"STT failed: {exc}")
            await self.emit("turn_done", turn=turn, skipped="stt failed")
            return
        logger.info("turn %d: stt (%s) done at +%.0fms", turn, turn_engine, _elapsed_ms())
        try:
            audio_seconds = wav_duration_seconds(wav)
            await record_usage(
                user_id=self.cfg.identity_user_id or "", profile_id=cfg.profile_name or "",
                kind="stt", engine=turn_engine, model_id=turn_model or "",
                unit="seconds", native_amount=audio_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break a turn
            logger.warning("stt usage metering failed: %s", exc)
        user_text = (stt_result.text or "").strip()
        await self.emit("user_transcript", turn=turn, text=user_text, engine=turn_engine)
        if not user_text:
            await self.emit("turn_done", turn=turn, skipped="empty transcript")
            return

        await _answer(user_text)

    async def _abort_turn(self, reason: str) -> None:
        if self.current_turn and not self.current_turn.done():
            self.current_turn.cancel()
            try:
                await self.current_turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await self.emit("aborted", reason=reason)
        self.current_turn = None
        self._speaking_since = None

    async def feed_audio(self, frame: bytes) -> None:
        if self.opus_decoder is not None:
            try:
                frame = self.opus_decoder.decode(frame)
            except Exception as exc:  # noqa: BLE001 - skip a bad packet, keep going
                logger.warning("opus decode failed: %s", exc)
                return
        event = self.endpointer.accept(frame)
        if not event:
            return
        if event["event"] == "speech_start":
            grace_ms = system_config_store.get().conversation.conversation_barge_in_grace_ms
            if barge_in_suppressed(self._speaking_since, time.monotonic(), grace_ms):
                # Onset echo of the assistant's own audio, not a real barge-in.
                return
            # Barge-in: user starts talking -> cancel the assistant's turn.
            await self._abort_turn("barge-in")
            await self.emit("speech_start")
        elif event["event"] == "endpoint":
            await self._abort_turn("superseded")
            await self.emit("speech_end", speech_ms=round(event["speech_ms"]))
            self.current_turn = asyncio.create_task(
                self._handle_turn(event["audio"], speech_ms=event["speech_ms"])
            )

    async def feed_text(self, text: str) -> None:
        # Text input turn (text→text / text→audio). Supersedes any current turn,
        # then runs fire-and-forget (like the audio endpoint path) so the caller's
        # receive loop stays free to process an abort/barge-in while the turn runs.
        await self._abort_turn("superseded")
        self.current_turn = asyncio.create_task(self._handle_turn(text_input=text or ""))

    async def wait_current_turn(self) -> None:
        """Await the in-flight turn (if any) to completion. For tests/tools that
        need a deterministic point after a fire-and-forget feed_*/flush call."""
        if self.current_turn and not self.current_turn.done():
            try:
                await self.current_turn
            except asyncio.CancelledError:
                pass

    async def abort(self, reason: str) -> None:
        await self._abort_turn(reason)

    async def speak(self, text: str) -> str | None:
        """One-off spoken utterance with no STT/LLM (e.g. an idle farewell):
        synthesize `text` and stream it as a normal speaking turn
        (tts start -> audio -> stop). Best-effort; never raises. Paced in
        real time so a device's small jitter buffer doesn't overflow.

        Returns None when the utterance was spoken OR deliberately skipped (empty
        text, a session with no audio downlink, over quota), and a short reason
        string when synthesis genuinely broke. announce() turns that reason into
        an `error` the client can show: a device that goes quiet because its TTS
        engine is down should say so on its panel rather than look asleep.

        Real provider spend, so it is metered and gated -- but as a SKIP, not a
        refusal: the server initiates this at teardown and nobody is waiting on
        it, so over quota the whole utterance (text event included) is dropped
        with a warning. Silence is the honest outcome; an error event would
        report a failure for something the user never asked for."""
        text = (text or "").strip()
        cfg = self.cfg
        if not text or not cfg.want_audio:
            return None
        try:
            if await self._farewell_quota_blocked():
                return None
            if cfg.want_text:
                await self.emit("response_text", turn=self.turn, chunk_index=0, text=text, responder="system")
            request = TTSRequest(
                text=text, engine=cfg.tts_engine, model_id=cfg.tts_model, voice=cfg.voice,
                ref_audio_path=cfg.ref_audio_path, ref_text=cfg.ref_text,
                instruct=cfg.tts_instruct, speed=cfg.tts_speed, language=cfg.tts_language,
            )
            audio, media_type = await self.tts_provider.render_audio(request)
            packets = None
            if self.opus_encoder is not None:
                pcm = await asyncio.to_thread(wav_bytes_to_pcm16, audio, cfg.output_sample_rate)
                packets = await asyncio.to_thread(self.opus_encoder.encode_pcm16, pcm)

            # ONE row for the utterance, here rather than in each branch above:
            # render_audio is the single way of producing this utterance of
            # `text`, so a row per call site would just be the same charge
            # written twice on the day someone reorders things.
            await self._record_tts_usage(text)

            if packets is not None:
                await self.emit("audio_start", turn=self.turn, chunk_index=0, codec="opus",
                                sample_rate=cfg.output_sample_rate, frames=len(packets))
                frame_s = self.opus_encoder.frame / self.opus_encoder.sample_rate
                delays = pacing_delays(
                    len(packets),
                    system_config_store.get().conversation.conversation_opus_prebuffer_frames,
                    frame_s,
                )
                for delay, pkt in zip(delays, packets):
                    if delay:
                        await asyncio.sleep(delay)
                    await self.emit_audio(pkt)
                await self.emit("audio_end", turn=self.turn, chunk_index=0)
            else:
                await self.emit(
                    "audio_start", turn=self.turn, chunk_index=0,
                    codec="mp3" if media_type == "audio/mpeg" else "wav",
                )
                await self.emit_audio(audio)
                await self.emit("audio_end", turn=self.turn, chunk_index=0)
            await self.emit("turn_done", turn=self.turn)
        except Exception as exc:  # noqa: BLE001 - farewell is best-effort
            logger.warning("speak() failed: %s", exc)
            return f"tts: {exc}"
        return None

    def _arm_close_after_speaking(self, reason: str) -> None:
        logger.info("close armed after the current utterance: %s", reason)
        self.close_after_speaking = reason

    async def announce(self, event: str) -> bool:
        """Say one line, in this profile's voice, about something the SERVER just did.

        `new_session` (a conversation was rotated away) and `idle_goodbye` (the
        connection is about to be dropped) used to be silent or a single phrase from
        admin config, identical for every profile. The line is written by the
        profile's own LLM with the tail of the conversation as context -- see
        announce.py for the prompt.

        On failure it stays silent and emits `error` naming the stage that broke, so a
        device shows WHY it went quiet (its panel renders `error` already) instead of
        looking asleep. No built-in phrase to fall back on: that would be the
        hardcoded sentence this replaces, one layer down.

        Returns whether an utterance was actually spoken. Callers that hang up after
        it (the idle watchdog) need to know: a silent skip means nothing will ever
        emit `turn_done`, so nothing would close the connection."""
        if self.responder is None or self.tts_provider is None or not self.cfg.want_audio:
            return False  # nothing to speak with, or nothing to speak into

        # Real LLM spend on a line nobody asked for, so: gated before, metered after.
        # Over quota it SKIPS silently rather than erroring, exactly as speak() does
        # for the TTS half -- reporting a failure for work the user never requested
        # would be noise on their display.
        blocked, _message = await llm_turn_quota_blocked(
            identity_user_id=self.cfg.identity_user_id,
            profile=self.profile,
            profile_name=self.cfg.profile_name,
        )
        if blocked:
            logger.warning("announce %s skipped: llm quota", event)
            return False
        try:
            line = await generate_line(
                responder=self.responder,
                persona=self.base_system_prompt,
                history=self.history,
                language=self.cfg.language,
                event=event,
            )
        except Exception as exc:  # noqa: BLE001 - self-initiated; report, never raise
            logger.warning("announce %s: line generation failed: %s", event, exc)
            await self.emit("error", message=f"llm: could not write the {event} line: {exc}")
            return False
        finally:
            # In the finally, not after: a call that spent tokens and then failed on
            # a malformed answer still spent them.
            if getattr(self.responder, "last_usage", None):
                await self._record_llm_usage()

        # Persisted BEFORE speaking, and to whichever session is current: for
        # new_session that is the fresh row (this is its first utterance), for
        # idle_goodbye the row about to be ended.
        self.history.append({"role": "assistant", "content": line})
        self.history = _tail(self.history)
        # A greeting alone must not create a conversation: the fresh session after a
        # rotation stays absent from History until somebody actually says something.
        await self._persist("assistant", line, defer_if_new=(event == "new_session"))

        failure = await self.speak(line)
        if failure:
            await self.emit("error", message=f"{failure} (the {event} line went unspoken)")
            return False
        return True

    async def reset(self) -> None:
        """Clear the in-memory conversation context, keeping the SAME session row.

        Note what this deliberately does NOT do: it does not end the stored
        session, so messages from before and after a reset land in one row with a
        continuous turn counter. Callers that want a genuinely separate
        conversation -- a distinct entry in History, its own memory extraction --
        want rotate() instead. Kept as-is because it is documented wire API
        (docs/api.md) and changing what an existing message means is worse than
        adding a new one."""
        await self._abort_turn("reset")
        self.history.clear()
        self.endpointer.reset()
        await self.emit("reset")

    async def request_rotate(self, reason: str = "client") -> None:
        """Wire entry point for `new_session`: rotate now, or once the turn in
        flight has finished.

        A voice-driven "start over" is requested from INSIDE a turn -- the device's
        self.session.new MCP tool fires while the model is still waiting for that
        tool's result. Rotating right there cancels the very turn that asked for
        it: the rotation happens, but the assistant never gets to say "starting a
        new conversation" and the tool result lands on a future nobody is waiting
        for. Deferring lets that turn finish against the conversation it was in --
        which is also the session it is persisted to -- and rotates the moment it
        is done.

        A client that means "stop talking and start over NOW" (a button, not a
        voice request) sends `abort` first; with no turn left in flight this
        rotates immediately.
        """
        if self.current_turn is not None and not self.current_turn.done():
            self._rotate_pending = reason
            # Fires on completion AND on cancellation, so a barge-in in the
            # middle of the deferred turn still leaves the user in the new
            # conversation they asked for rather than silently back in the old.
            self.current_turn.add_done_callback(self._rotate_when_turn_ends)
            return
        # announce: nobody has said anything about this rotation. The deferred
        # branch above is the voice path, where the turn that asked confirmed it
        # already -- see _rotate_when_turn_ends.
        await self.rotate(reason, announce=True)

    def _rotate_when_turn_ends(self, task: asyncio.Task) -> None:
        """done-callback for the turn a rotation is parked behind.

        Runs OUTSIDE the turn task, which is what makes it safe: rotate() aborts
        the current turn, and awaiting that from inside the turn's own callback
        chain would be a self-await. By the time this runs the task is done, so
        _abort_turn() has nothing to wait for."""
        reason, self._rotate_pending = self._rotate_pending, None
        if reason is None:
            return  # a second new_session got here first; one rotation is enough
        if self._closing:
            # close() already ended this session. Rotating now would create a
            # brand-new row on the way out and leave it open forever.
            return
        _spawn_background(self._rotate_logged(reason))

    async def _rotate_logged(self, reason: str) -> None:
        """rotate() for the deferred path: it runs detached from any request, so
        a failure here has nowhere to surface except the log.

        It runs on the loop tick right after the turn ended, and rotate() aborts
        whatever turn is current -- so a turn created inside that one tick would
        be cancelled. Barge-in cannot hit that window: it cancels at speech
        START and only creates the next turn at speech END, seconds later."""
        try:
            await self.rotate(reason)
        except Exception as exc:  # noqa: BLE001 - detached; must not vanish silently
            logger.warning("deferred rotate failed for %s: %s", self.cfg.session_id, exc)

    async def rotate(self, reason: str = "client", announce: bool = False) -> None:
        """End the current conversation and start a fresh one on the same connection.

        session_id is otherwise fixed for the lifetime of a WebSocket, which is
        wrong for a mains-powered speaker: it holds one socket open for days, so
        everything it ever says lands in a single History entry, the LLM context
        grows without bound, and memory extraction -- which only runs in close() --
        never runs at all.

        Deliberately does NOT rebuild the STT/TTS providers or the tool registry.
        Rotating is about the conversation RECORD, not the audio pipeline; tearing
        the pipeline down and back up would drop audio mid-stream and re-announce
        engine state the client already has.

        A session with nothing in it rotates to itself: pressing the button twice
        must not litter the History with empty rows.

        `announce` speaks a line about the fresh start (see announce()). Off by
        default: the caller decides, because on the voice path the model has already
        said it and saying it twice is worse than not saying it at all. It is also
        ignored when this rotation was the empty-session no-op -- nothing ended, so
        there is nothing to announce.
        """
        async with self._rotate_lock:
            rotated = await self._rotate_locked(reason)
        if announce and rotated:
            # Outside the lock and after session_rotated: the line belongs to the new
            # conversation, and a slow LLM must not hold up a second rotation.
            await self.announce("new_session")

    async def _rotate_locked(self, reason: str) -> bool:
        """Returns whether a new session was actually minted (False for the
        empty-session no-op)."""
        await self._abort_turn("rotate")

        previous_id = self.cfg.session_id
        # `turn` is the count of completed turns, so 0 means nothing was ever
        # persisted under this id. session_ready False means the row does not
        # exist at all (its creation failed in start()), and minting a second one
        # would just fail the same way.
        # A row exists only once something was said (see _persist), so this is the
        # same question `turn > 0` used to approximate -- without the approximation.
        rotated = self._row_exists and self.session_ready
        if rotated:
            try:
                await session_store.mark_ended(previous_id)
            except Exception as exc:  # noqa: BLE001 - rotation must not kill the connection
                logger.warning("mark_ended failed for %s: %s", previous_id, exc)
            if self.profile is not None:
                # Same background extraction close() does. Rotating is the only
                # moment a long-lived device connection ever reaches it.
                _spawn_background(
                    memory_extractor.extract_and_upsert(
                        previous_id, self.profile, user_id=self.cfg.identity_user_id
                    )
                )
            new_id = str(uuid.uuid4())
            # No row for it yet: the new conversation is written when it first has
            # something in it, exactly like this connection's first one was. So a
            # rotation nobody follows up on leaves one ended conversation behind,
            # not an ended one plus an empty one.
            self._row_exists = False
            self._pending_messages.clear()
            # Point every later write at the new id. Nothing caches this: _persist
            # and close() both read self.cfg.session_id at call time.
            self.cfg.session_id = new_id
            # A later start() must not resurrect the conversation we just ended.
            self.cfg.resume_sid = None

        self.history.clear()
        self.endpointer.reset()
        self.turn = 0
        await self.emit(
            "session_rotated",
            session_id=self.cfg.session_id,
            previous_session_id=previous_id,
            reason=reason,
        )
        return rotated

    async def flush(self) -> None:
        audio = self.endpointer.flush()
        if audio:
            await self._abort_turn("superseded")
            await self.emit("speech_end", speech_ms=0)
            self.current_turn = asyncio.create_task(self._handle_turn(audio))

    async def close(self) -> None:
        # Before the cancel below: cancelling the turn fires any rotation parked
        # behind it (_rotate_when_turn_ends), and on a closing connection that
        # rotation must be dropped rather than open a session nothing will end.
        self._closing = True
        if self.current_turn and not self.current_turn.done():
            self.current_turn.cancel()
            # Awaited, not just cancelled: cancellation is a REQUEST, and the
            # turn still has to unwind through its own finally/except blocks --
            # which touch the responder. Closing the responder out from under a
            # turn that is still unwinding raised inside a detached task, where
            # the only trace is a log line nobody reads.
            try:
                await self.current_turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass
        if self.responder is not None:
            try:
                await self.responder.aclose()
            except Exception as exc:  # noqa: BLE001 - teardown must not fail
                logger.warning("responder aclose failed for %s: %s", self.cfg.session_id, exc)
        if self.session_ready and self._row_exists:
            try:
                await session_store.mark_ended(self.cfg.session_id)
            except Exception as exc:  # noqa: BLE001 - teardown must not fail
                logger.warning("mark_ended failed for %s: %s", self.cfg.session_id, exc)
            if self.profile is not None:
                _spawn_background(
                    memory_extractor.extract_and_upsert(
                        self.cfg.session_id, self.profile, user_id=self.cfg.identity_user_id
                    )
                )
