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

from app.core.audio import pcm16_to_wav_bytes, preprocess_pcm16, wav_file_to_pcm16
from app.core.errors import AppError
from app.core.settings import settings
from app.schemas.tts import TTSRequest
from app.services.conversation.endpointer import VadEndpointer
from app.services.conversation.responder import (
    build_responder_ex,
    get_active_llm_model,
    resolve_system_prompt,
)
from app.services.conversation.tools.base import ToolContext, ToolRegistry
from app.services.conversation.tools.local import LocalToolSource
from app.services.conversation.tools.mcp import McpToolSource
from app.services.history.store import session_store
from app.services.mcp.pool import mcp_pool
from app.services.mcp.server_store import mcp_server_store
from app.services.memory.extractor import memory_extractor
from app.services.memory.retriever import inject_memories, memory_retriever
from app.services.profiles.store import profile_store
from app.services.stt.providers.whisper_provider import get_active_whisper_model
from app.services.stt.routing import select_stt_engine
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service
from app.services.tts.streaming import pacing_delays, prefetch_synthesis
from app.services.warmup import is_ready, warm_providers

logger = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]  # emit(event: str, **payload)
EmitAudioFn = Callable[[bytes], Awaitable[None]]  # emit_audio(opus_packet: bytes)

# Fire-and-forget background tasks (e.g. memory extraction) must be retained
# somewhere or CPython may garbage-collect them mid-flight.
_background_tasks: set[asyncio.Task] = set()


async def _build_tool_registry(profile) -> ToolRegistry | None:
    """Merge global + per-profile MCP servers (profile wins on name collision),
    skip disabled entries, and fetch each enabled server's tools."""
    global_servers = mcp_server_store.list()
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

    def is_turn_active(self) -> bool:
        return bool(self.current_turn and not self.current_turn.done())

    def add_tool_source(self, source) -> None:
        """Add a ToolSource after start(); used to register device MCP tools
        discovered over the WS. Creates the registry if none exists yet."""
        if self.tool_registry is None:
            self.tool_registry = ToolRegistry([source])
        else:
            self.tool_registry.add_source(source)

    @property
    def output_sample_rate_effective(self) -> int | None:
        """The output sample rate advertised in session_started (None for url mode)."""
        return self.cfg.output_sample_rate if self.cfg.want_audio and self.audio_out != "url" else None

    async def start(self) -> None:
        cfg = self.cfg

        # Re-resolve the profile object + LLM config from the profile name. This is
        # deterministic from profile_name (the front-end already emitted any
        # "profile not found" warning during its own query-param resolution).
        profile = profile_store.get(cfg.profile_name) if cfg.profile_name else None
        self.profile = profile
        llm_base_url = (profile.llm.base_url or None) if (profile and profile.llm.base_url) else None
        llm_api_key = profile.llm.api_key if (profile and profile.llm.base_url) else None
        llm_model = (profile.llm.model or None) if (profile and profile.llm.model) else None
        system_prompt = (profile.system_prompt or None) if (profile and profile.system_prompt) else None
        voice_optimized = bool(profile and profile.voice_optimized)

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

        self.responder = build_responder_ex(
            base_url=llm_base_url,
            api_key=llm_api_key,
            model=llm_model,
            system_prompt=system_prompt,
            voice_optimized=voice_optimized,
        )

        self.tool_registry = await _build_tool_registry(profile)

        # Detail strings so the UI can show exactly WHICH models are active this session.
        if hasattr(self.stt_provider, "detail"):
            stt_detail = self.stt_provider.detail()  # whisper_mlx / whisper_gemma expose this
        elif cfg.stt_engine in {"whisper", "whisper_local"}:
            stt_detail = get_active_whisper_model()
        else:
            stt_detail = cfg.stt_engine
        tts_detail = self.tts_provider.detail() if hasattr(self.tts_provider, "detail") else cfg.tts_engine

        model_label = get_active_llm_model() if self.responder.name == "llm" else self.responder.name

        self.endpointer = VadEndpointer(
            cfg.sample_rate,
            silence_ms=settings.conversation_silence_ms,
            min_speech_ms=settings.conversation_min_speech_ms,
            rms_threshold=settings.conversation_rms_threshold,
            max_utterance_ms=settings.conversation_max_utterance_ms,
            min_silence_ms=settings.conversation_min_silence_ms,
            adaptive_full_ms=settings.conversation_adaptive_full_ms,
            preroll_ms=settings.conversation_preroll_ms,
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
            stt_detail=stt_detail,
            language=cfg.language,
            tts_engine=cfg.tts_engine,
            tts_detail=tts_detail,
            responder=self.responder.name,
            llm_model=model_label,
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
            block = await memory_retriever.get_context(self.profile, query=query)
            self.responder.system_prompt = inject_memories(self.base_system_prompt, block)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory retrieval failed: %s", exc)

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
        self.turn += 1
        turn = self.turn
        turn_start = time.monotonic()
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
                result = await self.tts_provider.synthesize(
                    TTSRequest(
                        text=sentence, engine=cfg.tts_engine, voice=cfg.voice,
                        ref_audio_path=cfg.ref_audio_path, ref_text=cfg.ref_text,
                        instruct=cfg.tts_instruct, speed=cfg.tts_speed, language=cfg.tts_language,
                    )
                )
                if self.opus_encoder is not None:
                    # Decode the TTS wav -> PCM16 @ output_sr -> Opus packets (off-loop).
                    path = result.audio_url.lstrip("/")
                    pcm = await asyncio.to_thread(wav_file_to_pcm16, path, cfg.output_sample_rate)
                    packets = await asyncio.to_thread(self.opus_encoder.encode_pcm16, pcm)
                    return result, packets
                return result, None

            async with aclosing(
                prefetch_synthesis(
                    sentence_aiter, _synth, lookahead=settings.conversation_tts_lookahead
                )
            ) as pipeline:
                async for index, sentence, (result, packets) in pipeline:
                    _log_first_chunk()
                    parts.append(sentence)
                    if want_text:
                        await self.emit("response_text", turn=turn, chunk_index=index, text=sentence, responder=responder_name)
                    if packets is not None:
                        # Push Opus binary frames bracketed by audio_start/audio_end (devices).
                        await self.emit(
                            "audio_start", turn=turn, chunk_index=index,
                            text=sentence if want_text else None,
                            codec="opus", sample_rate=cfg.output_sample_rate, frames=len(packets),
                        )
                        # Pace packets at real-time after an initial burst so the
                        # device buffer isn't flooded on long replies.
                        if settings.conversation_opus_pace and packets:
                            frame_s = self.opus_encoder.frame / self.opus_encoder.sample_rate
                            delays = pacing_delays(
                                len(packets), settings.conversation_opus_prebuffer_frames, frame_s
                            )
                        else:
                            delays = [0.0] * len(packets)
                        for delay, pkt in zip(delays, packets):
                            if delay:
                                await asyncio.sleep(delay)
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
                amount=settings.stt_noise_reduce_amount,
            )
        wav = pcm16_to_wav_bytes(pcm, sample_rate=cfg.sample_rate)

        # Fast-path routing: short commands can go to a lower-latency engine.
        turn_provider = self.stt_provider
        turn_engine = cfg.stt_engine
        if speech_ms and settings.conversation_fast_stt_engine:
            chosen = select_stt_engine(
                speech_ms,
                cfg.stt_engine,
                settings.conversation_fast_stt_engine,
                settings.conversation_fast_stt_max_ms,
            )
            if chosen != cfg.stt_engine:
                try:
                    turn_provider = stt_service.get_provider(chosen)
                    turn_engine = chosen
                except AppError:
                    logger.info("fast STT engine %s unavailable; using %s", chosen, cfg.stt_engine)

        try:
            stt_result = await turn_provider.transcribe_bytes(wav, cfg.language)
        except RuntimeError as exc:
            await self.emit("error", message=f"STT failed: {exc}")
            return
        logger.info("turn %d: stt (%s) done at +%.0fms", turn, turn_engine, _elapsed_ms())
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
        real time so a device's small jitter buffer doesn't overflow."""
        text = (text or "").strip()
        cfg = self.cfg
        if not text or not cfg.want_audio:
            return
        try:
            if cfg.want_text:
                await self.emit("response_text", turn=self.turn, chunk_index=0, text=text, responder="system")
            result = await self.tts_provider.synthesize(
                TTSRequest(
                    text=text, engine=cfg.tts_engine, voice=cfg.voice,
                    ref_audio_path=cfg.ref_audio_path, ref_text=cfg.ref_text,
                    instruct=cfg.tts_instruct, speed=cfg.tts_speed, language=cfg.tts_language,
                )
            )
            if self.opus_encoder is not None:
                pcm = await asyncio.to_thread(wav_file_to_pcm16, result.audio_url.lstrip("/"), cfg.output_sample_rate)
                packets = await asyncio.to_thread(self.opus_encoder.encode_pcm16, pcm)
                await self.emit("audio_start", turn=self.turn, chunk_index=0, codec="opus",
                                sample_rate=cfg.output_sample_rate, frames=len(packets))
                frame_s = self.opus_encoder.frame / self.opus_encoder.sample_rate
                delays = pacing_delays(len(packets), settings.conversation_opus_prebuffer_frames, frame_s)
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
        if self.session_ready:
            try:
                await session_store.mark_ended(self.cfg.session_id)
            except Exception as exc:  # noqa: BLE001 - teardown must not fail
                logger.warning("mark_ended failed for %s: %s", self.cfg.session_id, exc)
            if self.profile is not None:
                _spawn_background(memory_extractor.extract_and_upsert(self.cfg.session_id, self.profile))
