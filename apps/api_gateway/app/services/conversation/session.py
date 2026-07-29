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
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass

from app.core.audio import (
    pcm16_to_wav_bytes,
    preprocess_pcm16,
    wav_bytes_to_pcm16,
    wav_duration_seconds,
    wav_file_to_pcm16,
)
from app.core.errors import AppError
from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.conversation.endpointer import VadEndpointer, barge_in_suppressed
from app.services.conversation.responder import (
    build_responder_ex,
    resolve_llm_override_from_registry,
    resolve_system_prompt,
)
from app.services.conversation.tools.base import ToolContext, ToolRegistry, ToolSource
from app.services.conversation.tools.local import LocalToolSource
from app.services.conversation.tools.mcp import McpToolSource
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
from app.services.tts.base import RenderingTTSProvider
from app.services.tts.service import tts_service
from app.services.tts.streaming import pacing_delays, prefetch_synthesis
from app.services.usage.attribution import resolve_llm_pair, resolve_usage_model
from app.services.usage.recorder import record_usage
from app.services.warmup import is_ready, warm_providers

logger = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]  # emit(event: str, **payload)
EmitAudioFn = Callable[[bytes], Awaitable[None]]  # emit_audio(opus_packet: bytes)

# Fire-and-forget background tasks (e.g. memory extraction) must be retained
# somewhere or CPython may garbage-collect them mid-flight.
_background_tasks: set[asyncio.Task] = set()


async def _build_tool_registry(profile) -> ToolRegistry | None:
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
    if settings.conversation_tools_enabled:
        tool_sources.append(LocalToolSource())
    for srv in merged_servers.values():
        if not srv.enabled:
            continue
        tools = await mcp_pool.get_tools(srv.url, headers=srv.headers)
        if tools:
            tool_sources.append(
                McpToolSource(
                    tools,
                    invoker=lambda n, a, u=srv.url, h=srv.headers: mcp_pool.invoke(u, n, a, headers=h),
                )
            )
    return ToolRegistry(tool_sources) if tool_sources else None


def _spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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
    audio_out: str  # "url" | "opus"
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
        """The output sample rate advertised in session_started (None for url mode)."""
        return self.cfg.output_sample_rate if self.cfg.want_audio and self.audio_out != "url" else None

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
        llm_base_url = (profile.llm.base_url or None) if (profile and profile.llm.base_url) else None
        llm_api_key = profile.llm.api_key if (profile and profile.llm.base_url) else None
        llm_model = (profile.llm.model or None) if (profile and profile.llm.model) else None
        if profile and profile.llm.engine and profile.llm.model:
            registry_override = await resolve_llm_override_from_registry(profile.llm.engine, profile.llm.model)
            if registry_override:
                llm_base_url, llm_api_key = registry_override
                llm_model = profile.llm.model
        system_prompt = (profile.system_prompt or None) if (profile and profile.system_prompt) else None
        voice_optimized = bool(profile and profile.voice_optimized)

        self.stt_model_id = cfg.stt_model or resolve_default_stt_model(cfg.stt_engine)
        self.stt_provider = stt_service.get_provider(cfg.stt_engine)
        self.tts_provider = tts_service.get_provider(cfg.tts_engine)

        # Set up Opus decoding if the client negotiated it; fall back to PCM16 with a
        # warning if the server lacks libopus (so the connection still works).
        if self.audio_codec == "opus":
            from app.core.opus import OpusFrameDecoder, opus_available

            if opus_available():
                self.opus_decoder = OpusFrameDecoder(sample_rate=cfg.sample_rate, channels=1)
            else:
                self.audio_codec = "pcm16"
                logger.warning("client requested opus but server has no libopus; using pcm16")

        # Set up Opus encoding for pushed audio output (devices). Fall back to url.
        if cfg.want_audio and self.audio_out == "opus":
            from app.core.opus import OpusFrameEncoder, opus_available

            if opus_available():
                self.opus_encoder = OpusFrameEncoder(sample_rate=cfg.output_sample_rate, channels=1)
            else:
                self.audio_out = "url"
                logger.warning("client requested opus output but server has no libopus; using url")

        self.responder = await build_responder_ex(
            base_url=llm_base_url,
            api_key=llm_api_key,
            model=llm_model,
            system_prompt=system_prompt,
            voice_optimized=voice_optimized,
        )

        self.tool_registry = await _build_tool_registry(profile)

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
        self.endpointer = VadEndpointer(
            cfg.sample_rate,
            silence_ms=conv_cfg.conversation_silence_ms,
            min_speech_ms=conv_cfg.conversation_min_speech_ms,
            rms_threshold=conv_cfg.conversation_rms_threshold,
            max_utterance_ms=conv_cfg.conversation_max_utterance_ms,
            min_silence_ms=conv_cfg.conversation_min_silence_ms,
            adaptive_full_ms=conv_cfg.conversation_adaptive_full_ms,
            preroll_ms=conv_cfg.conversation_preroll_ms,
        )
        # Session persistence: resume seeds history from the DB; new sessions are recorded.
        self.history = []
        self.session_ready = True
        try:
            if cfg.resume_sid and await session_store.exists(cfg.resume_sid):
                self.history = [
                    {"role": m["role"], "content": m["content"]}
                    for m in await session_store.get_messages(cfg.resume_sid)
                ]
            else:
                await session_store.create(
                    cfg.session_id,
                    profile_id=cfg.profile_name or "",
                    meta={"stt_engine": cfg.stt_engine, "tts_engine": cfg.tts_engine},
                    # No `or profile.owner_id` fallback: a fleet/dev-mode caller
                    # (identity_user_id is None) must create an ownerless row, not
                    # one silently attributed to the owner of whatever profile
                    # name was passed in (H2 -- that let a named profile's owner
                    # be billed for / attributed a session they never touched).
                    user_id=cfg.identity_user_id,
                )
        except Exception as exc:  # noqa: BLE001 - session setup must not drop the connection
            logger.warning("session setup failed for %s: %s", cfg.session_id, exc)
            self.history = []
            self.session_ready = False
        self.turn = 0

        active_tools = list(self.tool_registry._tools.keys()) if self.tool_registry else []
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

        self.base_system_prompt = resolve_system_prompt(system_prompt, voice_optimized)
        self.tool_ctx = ToolContext(emit_command=self._emit_command, language=cfg.language or None)

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

        asyncio.create_task(_warm_and_notify())

    async def _emit_command(self, cmd_payload: dict) -> None:
        await self.emit("command", **cmd_payload)

    async def _persist(self, role: str, content: str) -> None:
        if not self.session_ready:
            return
        try:
            await session_store.append_message(self.cfg.session_id, self.turn, role, content)
        except Exception as exc:  # noqa: BLE001 - persistence must not kill the turn
            logger.warning("history persist failed: %s", exc)

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
        populated). Must never raise into the turn: record_usage itself already
        swallows its own errors, but building the args (profile may be None,
        last_usage may be None or missing keys) must not raise either.
        """
        try:
            last_usage = getattr(self.responder, "last_usage", None) or {}
            prompt_tokens = last_usage.get("prompt_tokens")
            completion_tokens = last_usage.get("completion_tokens")
            native_amount = (prompt_tokens or 0) + (completion_tokens or 0)
            engine, model_id = resolve_llm_pair(
                self.responder,
                (self.profile.llm.engine if self.profile else "") or "",
                (self.profile.llm.model if self.profile else "") or "",
            )
            await record_usage(
                user_id=self.cfg.identity_user_id or "", profile_id=self.cfg.profile_name or "",
                kind="llm", engine=engine, model_id=model_id, unit="tokens",
                native_amount=native_amount, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never break a turn
            logger.warning("llm usage metering failed: %s", exc)

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

    async def _run_turn(
        self, audio_pcm: bytes | None = None, text_input: str | None = None, speech_ms: float = 0.0
    ) -> None:
        cfg = self.cfg
        want_audio = cfg.want_audio
        want_text = cfg.want_text

        # Best-effort quota gate: ONCE per turn, before anything else (STT/LLM/TTS).
        # quota_gate() itself is fail-open (internal errors log + allow), so only a
        # genuine over-limit quota raises here. Resolving the LLM provider_id is
        # wrapped separately so a registry lookup issue can never block the turn --
        # it just falls back to "" (user/global scope quotas still apply).
        provider_id = ""
        try:
            pinned_model = (self.profile.llm.model if self.profile else "") or ""
            # Only pair the profile's engine with a model the profile actually
            # pinned. With no pin, build_responder_ex() runs the registry
            # default -- whose engine is usually a different row -- so passing
            # this engine would make the gate check one provider while metering
            # bills another (see resolve_llm_pair, which applies the same rule
            # to the usage row). Both blank -> resolve_usage_model() returns the
            # active default pair, which is what actually runs.
            pinned_engine = ((self.profile.llm.engine if self.profile else "") or "") if pinned_model else ""
            llm_engine, llm_model = await resolve_usage_model("llm", pinned_engine, pinned_model)
            entry = await model_registry_store.find("llm", llm_engine, llm_model)
            provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
        except Exception:  # noqa: BLE001 - provider_id resolution must never block the turn
            llm_engine, llm_model, provider_id = "", "", ""
        try:
            # function-local: tests monkeypatch app.services.quota.gate.quota_gate by
            # reassigning the module attribute (see test_stt_stream_metering.py's
            # counting_gate); a top-level `from ... import quota_gate` binds the
            # name once at import time and never observes that reassignment.
            from app.services.quota.gate import QuotaExceededError, quota_gate

            await quota_gate(
                user_id=cfg.identity_user_id or "", provider_id=provider_id,
                kind="llm", engine=llm_engine, model_id=llm_model,
                profile_id=cfg.profile_name or "",
            )
        except QuotaExceededError as exc:
            # Mirror the existing STT-failure pattern: a plain "error" notice,
            # then return without running the turn at all.
            await self.emit("error", message=str(exc))
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

        # Stream a sentence iterator through TTS. For audio output we synthesize up to
        # `conversation_tts_lookahead` sentences AHEAD of sending (prefetch_synthesis),
        # so the next sentence's audio is usually ready before the current finishes ->
        # gapless playback. Text-only just emits sentences as the LLM streams them.
        async def _stream_to_tts(sentence_aiter, responder_name: str) -> list[str]:
            parts: list[str] = []

            if not want_audio:
                index = 0
                async for sentence in sentence_aiter:
                    _log_first_chunk()
                    parts.append(sentence)
                    if want_text:
                        await self.emit("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                    index += 1
                return parts

            async def _synth(sentence: str):
                # Returns (result, packets, error). A TTS failure is caught HERE and
                # returned as the third element instead of raised, so the pipeline
                # still yields this sentence -- the consumer emits its `response_text`
                # (the LLM's words must survive a TTS outage) and a `tts_error`, then
                # keeps the turn going. Raising instead would unwind the whole turn to
                # the generic `error` handler and swallow the already-generated text.
                logger.info("DEBUG_HANG _synth: starting engine=%s sentence=%r", cfg.tts_engine, sentence)
                try:
                    # Built INSIDE the guard: cfg.ref_audio_path comes from a
                    # stored TtsProfile, which now validates this at save
                    # time too -- but constructing TTSRequest here as well
                    # means any future validation error (or any other
                    # TTSRequest construction failure) still degrades to
                    # tts_error below instead of raising outside this try and
                    # unwinding the whole turn, swallowing already-generated
                    # text the comment above warns against losing.
                    request = TTSRequest(
                        text=sentence, engine=cfg.tts_engine, model_id=cfg.tts_model, voice=cfg.voice,
                        ref_audio_path=cfg.ref_audio_path, ref_text=cfg.ref_text,
                        instruct=cfg.tts_instruct, speed=cfg.tts_speed, language=cfg.tts_language,
                    )
                    if self.opus_encoder is not None and isinstance(self.tts_provider, RenderingTTSProvider):
                        # Nothing downstream reads `result` once we have Opus packets (see
                        # the `packets is not None` branch below), so synthesize()'s
                        # write-then-immediately-read-back through the artifact store is
                        # pure overhead on this latency-critical path. render_wav() is the
                        # same real synthesis with no artifact side effect.
                        wav = await self.tts_provider.render_wav(request)
                        logger.info("DEBUG_HANG _synth: got result for sentence=%r", sentence)
                        await self._record_tts_usage(sentence)
                        pcm = await asyncio.to_thread(wav_bytes_to_pcm16, wav, cfg.output_sample_rate)
                        packets = await asyncio.to_thread(self.opus_encoder.encode_pcm16, pcm)
                        return None, packets, None
                    result = await self.tts_provider.synthesize(request)
                    logger.info("DEBUG_HANG _synth: got result for sentence=%r", sentence)
                    await self._record_tts_usage(sentence)
                    if self.opus_encoder is not None:
                        # Fallback for engines that aren't a RenderingTTSProvider (e.g.
                        # edge_tts, which is test-UI/batch only and produces MP3) -- keep
                        # the artifact-backed path rather than crashing on a missing
                        # render_wav().
                        path = result.audio_url.lstrip("/")
                        pcm = await asyncio.to_thread(wav_file_to_pcm16, path, cfg.output_sample_rate)
                        packets = await asyncio.to_thread(self.opus_encoder.encode_pcm16, pcm)
                        return result, packets, None
                    return result, None, None
                except asyncio.CancelledError:
                    raise  # barge-in / turn supersede -- must propagate to unwind the turn
                except Exception as exc:  # noqa: BLE001 - degrade to text-only, don't lose the reply
                    logger.warning("TTS synth failed (engine=%s) for %r: %s", cfg.tts_engine, sentence, exc)
                    return None, None, exc

            async with aclosing(
                prefetch_synthesis(
                    sentence_aiter, _synth,
                    lookahead=system_config_store.get().conversation.conversation_tts_lookahead,
                )
            ) as pipeline:
                logger.info("DEBUG_HANG _stream_to_tts: entering pipeline consume loop")
                # Global real-time pacer for the WHOLE reply: prebuffer the first
                # few frames, then release every frame on one monotonic clock.
                # Per-sentence pacing used to prebuffer-burst at each sentence, so
                # multi-sentence replies accumulated in the device jitter buffer
                # and overflowed (dropped words on long replies). A single clock
                # keeps the device buffer ~prebuffer-deep for the entire reply.
                _conv_cfg = system_config_store.get().conversation
                _do_pace = cfg.opus_pace if cfg.opus_pace is not None else _conv_cfg.conversation_opus_pace
                _prebuf = _conv_cfg.conversation_opus_prebuffer_frames
                _pace_t0 = None
                _pace_n = 0
                tts_error_reported = False
                async for index, sentence, (result, packets, tts_error) in pipeline:
                    logger.info("DEBUG_HANG _stream_to_tts: pipeline yielded index=%d", index)
                    _log_first_chunk()
                    parts.append(sentence)
                    if want_text:
                        await self.emit("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                    if tts_error is not None:
                        # Synth failed for this sentence: text already went out above.
                        # Report the TTS failure once per turn (a fully-down engine
                        # would otherwise emit one per sentence) and skip audio -- the
                        # client falls back to showing text only.
                        if not tts_error_reported:
                            tts_error_reported = True
                            await self.emit(
                                "tts_error", turn=turn, chunk_index=index,
                                engine=cfg.tts_engine, message=str(tts_error),
                            )
                        continue
                    if packets is not None:
                        # Mark when the assistant first starts speaking this turn,
                        # so feed_audio can ignore onset echo as barge-in.
                        if self._speaking_since is None:
                            self._speaking_since = time.monotonic()
                        # Push Opus binary frames bracketed by audio_start/audio_end (devices).
                        await self.emit(
                            "audio_start", turn=turn, chunk_index=index,
                            text=sentence if want_text else None,
                            codec="opus", sample_rate=cfg.output_sample_rate, frames=len(packets),
                        )
                        # Release on the single global clock (see _pace_* above).
                        # First _prebuf frames of the reply go out immediately to
                        # fill the device jitter buffer; every frame after that is
                        # paced to real time, so a fast synth can't flood the
                        # device and a slow one just catches up (no per-sentence
                        # burst accumulation).
                        #
                        # Frame duration is read HERE, not before the loop: a
                        # session that negotiated no Opus (browser PCM /
                        # audio_url mode) has self.opus_encoder is None for the
                        # whole turn, and touching it eagerly crashed every such
                        # turn. Inside this branch the encoder is guaranteed --
                        # packets only exist when it does -- same as speak().
                        _frame_s = self.opus_encoder.frame / self.opus_encoder.sample_rate
                        for pkt in packets:
                            if _do_pace:
                                if _pace_t0 is None:
                                    _pace_t0 = time.monotonic()
                                if _pace_n >= _prebuf:
                                    target = _pace_t0 + (_pace_n - _prebuf) * _frame_s
                                    now = time.monotonic()
                                    if target > now:
                                        await asyncio.sleep(target - now)
                                _pace_n += 1
                            await self.emit_audio(pkt)
                        await self.emit("audio_end", turn=turn, chunk_index=index)
                    else:
                        await self.emit(
                            "audio_chunk", turn=turn, chunk_index=index, text=sentence,
                            audio_url=result.audio_url, sample_rate=result.sample_rate,
                        )
            return parts

        # Text input: skip STT, reply via the text responder (text→text / text→audio).
        if text_input is not None:
            user_text = (text_input or "").strip()
            await self.emit("user_transcript", turn=turn, text=user_text, engine="text")
            if not user_text:
                await self.emit("turn_done", turn=turn, skipped="empty text")
                return
            self.history.append({"role": "user", "content": user_text})
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
            await self._persist("assistant", " ".join(parts))
            logger.info("turn %d: done at +%.0fms (text input)", turn, _elapsed_ms())
            await self.emit("turn_done", turn=turn)
            return

        # Audio input: build the wav for STT.
        pcm = audio_pcm
        if cfg.denoise:
            pcm = preprocess_pcm16(
                audio_pcm, cfg.sample_rate, denoise=True, vad=False,
                amount=system_config_store.get().preprocessing.stt_noise_reduce_amount,
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
            await self.emit("error", message=f"STT failed: {exc}")
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

        self.history.append({"role": "user", "content": user_text})
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
        await self._persist("assistant", " ".join(parts))
        logger.info("turn %d: done at +%.0fms", turn, _elapsed_ms())
        await self.emit("turn_done", turn=turn)

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

    async def speak(self, text: str) -> None:
        """One-off spoken utterance with no STT/LLM (e.g. an idle farewell):
        synthesize `text` and stream it as a normal speaking turn
        (tts start -> audio -> stop). Best-effort; never raises. Paced in
        real time so a device's small jitter buffer doesn't overflow.

        Real provider spend, so it is metered and gated -- but as a SKIP, not a
        refusal: the server initiates this at teardown and nobody is waiting on
        it, so over quota the whole utterance (text event included) is dropped
        with a warning. Silence is the honest outcome; an error event would
        report a failure for something the user never asked for."""
        text = (text or "").strip()
        cfg = self.cfg
        if not text or not cfg.want_audio:
            return
        try:
            if await self._farewell_quota_blocked():
                return
            if cfg.want_text:
                await self.emit("response_text", turn=self.turn, chunk_index=0, text=text, responder="system")
            request = TTSRequest(
                text=text, engine=cfg.tts_engine, model_id=cfg.tts_model, voice=cfg.voice,
                ref_audio_path=cfg.ref_audio_path, ref_text=cfg.ref_text,
                instruct=cfg.tts_instruct, speed=cfg.tts_speed, language=cfg.tts_language,
            )
            result = None
            packets = None
            if self.opus_encoder is not None and isinstance(self.tts_provider, RenderingTTSProvider):
                # Same no-disk seam as _synth() above -- no artifact URL is ever
                # read once we're producing Opus packets.
                wav = await self.tts_provider.render_wav(request)
                pcm = await asyncio.to_thread(wav_bytes_to_pcm16, wav, cfg.output_sample_rate)
                packets = await asyncio.to_thread(self.opus_encoder.encode_pcm16, pcm)
            else:
                result = await self.tts_provider.synthesize(request)
                if self.opus_encoder is not None:
                    # Fallback for non-RenderingTTSProvider engines (see _synth()).
                    pcm = await asyncio.to_thread(wav_file_to_pcm16, result.audio_url.lstrip("/"), cfg.output_sample_rate)
                    packets = await asyncio.to_thread(self.opus_encoder.encode_pcm16, pcm)

            # ONE row for the utterance, here rather than in each branch above:
            # render_wav and synthesize are the two ways of producing this single
            # utterance of `text`, never both, so a row per call site would just
            # be the same charge written twice on the day someone reorders them.
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
                await self.emit("audio_chunk", turn=self.turn, chunk_index=0, text=text,
                                audio_url=result.audio_url, sample_rate=result.sample_rate)
            await self.emit("turn_done", turn=self.turn)
        except Exception as exc:  # noqa: BLE001 - farewell is best-effort
            logger.warning("speak() failed: %s", exc)

    async def reset(self) -> None:
        await self._abort_turn("reset")
        self.history.clear()
        self.endpointer.reset()
        await self.emit("reset")

    async def flush(self) -> None:
        audio = self.endpointer.flush()
        if audio:
            await self._abort_turn("superseded")
            await self.emit("speech_end", speech_ms=0)
            self.current_turn = asyncio.create_task(self._handle_turn(audio))

    async def close(self) -> None:
        if self.current_turn and not self.current_turn.done():
            self.current_turn.cancel()
        if self.responder is not None:
            try:
                await self.responder.aclose()
            except Exception as exc:  # noqa: BLE001 - teardown must not fail
                logger.warning("responder aclose failed for %s: %s", self.cfg.session_id, exc)
        if self.session_ready:
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
