"""Lugo device protocol route: WS /v1/lugo/stream.

Handshake: the first client message must be a ``wakeup`` declaring a profile
name. The server resolves engines/TTS params from that profile (the device
never sends raw engine/voice choices), builds a protocol-neutral
``ConversationSession``, and replies with ``welcome``. From there, neutral
core events are translated to the Lugo wire format and Opus audio packets are
wrapped with the v3 binary frame header.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.auth_guard import resolve_ws_identity, ws_session_owner_denied
from app.core.errors import AppError
from app.core.identity_watch import identity_still_valid
from app.core.opus import parse_opus_sample_rate
from app.core.settings import settings
from app.services.conversation.lugo_frame import LUGO_FRAME_OPUS, encode_frame
from app.services.conversation.session import ConversationSession, SessionRuntimeConfig
from app.services.conversation.tts_params import TtsParams, tts_params_from_profile
from app.services.conversation.tools.device_mcp import (
    DeviceMcpToolSource, DeviceMcpTransport, discover_device_tools,
)
from app.services.health import check_resolved_engines
from app.services.auth.device_profile import resolve_bound_profile
from app.services.profile_visibility import visible_profile_or_none, visible_tts_profile_or_none
from app.services.profiles.store import profile_store
from app.services.stt.profile import resolve_stt
from app.services.system_config import system_config_store
from app.services.tts.profile_store import tts_profile_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/lugo", tags=["lugo"])

# How often the idle watchdog wakes to check for inactivity (test-tunable).
_IDLE_TICK_S = 1.0

# How often the disabled/revoked re-check wakes (test-tunable, same idea as
# _IDLE_TICK_S). Independent of idle/turn-active state -- a disabled account
# is cut off even mid-turn.
_IDENTITY_RECHECK_INTERVAL_S = 30.0

# Ceiling on how long the idle watchdog spends producing its farewell (LLM +
# synthesis + paced audio) before hanging up regardless. Generous, because
# cutting the goodbye off is the bug this whole path exists to fix; bounded,
# because a hung LLM or TTS engine must not pin the socket open -- announce()
# awaits both with no deadline of its own, so without this a stuck engine kept
# the device connected and the watchdog parked in an await forever.
_FAREWELL_BUDGET_S = 45.0

# How long the server waits for the opening `wakeup` frame. A client that
# passes auth and then says nothing would otherwise hold a socket (and its
# handler task) open indefinitely: the idle watchdog does not exist yet at that
# point in the handshake, so nothing else would ever reclaim it.
_WAKEUP_TIMEOUT_S = 10.0

# How far past the idle deadline a hold may push the disconnect before it is
# overridden. A turn that never finishes must not keep a device connected forever,
# and a device's own idle watchdog fires at idle_timeout_s + ~5s anyway, so a
# server hold longer than that just loses the race and drops the farewell.
_MAX_IDLE_HOLD_S = 20.0


def refreshes_idle(event: str, payload: dict) -> bool:
    """Does this server-side event count as interaction for the idle countdown?

    What the VAD *guesses* does not. A room with background sound produces
    speech_start -> endpoint -> a turn whose transcript comes back empty, over and
    over; observed on the speaker as three "turns" in twenty seconds, none of them
    anything the user said. Counting those kept the countdown permanently reset, so
    the idle timeout -- and the farewell that hangs off it -- never arrived.

    `processing` is excluded for the same reason (it fires before anyone knows
    whether there are words in the audio); a turn that IS running is held open by
    `session.is_turn_active()` in the watchdog rather than by refreshing here.
    """
    if event in ("speech_start", "speech_end", "processing"):
        return False
    if event == "turn_done" and payload.get("skipped"):
        return False
    if event == "user_transcript" and not str(payload.get("text") or "").strip():
        return False
    return True

def _resolve(profile_name: str | None, caller_id: str | None = None, *, bypass: bool = False):
    """Resolve engines/tts params from a profile (server owns everything).

    C2 fix: visible_profile_or_none()/visible_tts_profile_or_none() collapse
    "doesn't exist" and "exists but belongs to someone else" to the same
    None, so a device authenticated as `caller_id` can never stream against
    another user's private profile (its llm.api_key/system_prompt) or tts
    profile (voice/ref_audio_path) -- see finding C2 in
    docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md.
    `bypass` is for the pre-existing dev-mode fallback only (see
    resolve_visible_profile's docstring / WsIdentity.unauthenticated);
    callers not resolving a real WS identity leave it False.
    """
    profile = visible_profile_or_none(
        profile_store.get(profile_name) if profile_name else None, caller_id, bypass=bypass
    )
    # Resolve STT from the profile's SttConfig (engine/language or a language
    # preset), falling back to server defaults — same single source of truth the
    # conversation stream uses, so a device that sends only a profile id streams
    # against that profile's STT. No query params on the Lugo wire.
    stt_engine, language, stt_model = resolve_stt(profile)
    tts_name = (profile.tts.profile_name if profile else "") or None
    tts_profile = visible_tts_profile_or_none(
        tts_profile_store.get(tts_name) if tts_name else None, caller_id, bypass=bypass
    )
    # Same mapping conversation.py/livehost.py use -- see
    # services/conversation/tts_params.py. No fallback_voice: the Lugo wire has
    # no query params, so a profile that names no voice leaves it to the engine.
    tts = tts_params_from_profile(tts_profile) or TtsParams(
        engine=system_config_store.get().engines.default_tts_engine,
        model_id="", voice=None, ref_audio_path=None, ref_text=None,
        instruct=None, speed=None, language=None,
    )
    idle = profile.session.idle_timeout_s if profile else 30
    return profile, stt_engine, language, stt_model, tts, idle


@router.websocket("/stream")
async def lugo_stream(websocket: WebSocket) -> None:
    identity = await resolve_ws_identity(websocket)
    if identity is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
    # Handshake: first frame must be a `wakeup`, and it must arrive promptly.
    try:
        message = await asyncio.wait_for(websocket.receive(), _WAKEUP_TIMEOUT_S)
    except TimeoutError:
        logger.info("lugo: no wakeup within %.0fs; closing", _WAKEUP_TIMEOUT_S)
        with contextlib.suppress(RuntimeError):
            await websocket.send_json({"type": "error", "message": "expected wakeup"})
        with contextlib.suppress(RuntimeError):
            await websocket.close()
        return
    if message.get("type") == "websocket.disconnect":
        return
    if message.get("bytes") is not None:
        # Binary first frame is not a valid wakeup.
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return
    try:
        hello = json.loads(message.get("text") or "")
    except json.JSONDecodeError:
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return
    if not isinstance(hello, dict):
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return
    if hello.get("type") != "wakeup":
        await websocket.send_json({"type": "error", "message": "expected wakeup"})
        await websocket.close()
        return

    profile_name = hello.get("profile")
    # Same rule as conversation.py's stream: a paired device's server-side
    # binding outranks the profile its own config declared in the wakeup, and an
    # unbound device is left exactly as it was so existing fleets keep working.
    profile_name, binding_warning, from_binding = await resolve_bound_profile(
        identity, profile_name
    )
    if binding_warning:
        await websocket.send_json({"type": "warning", "message": binding_warning})
    profile, stt_engine, language, stt_model, tts, idle = _resolve(
        profile_name, identity.user_id, bypass=identity.unauthenticated
    )
    if profile_name and not profile:
        if from_binding:
            # The SERVER chose this name, so an unresolvable one is our stale
            # state, not the caller's mistake -- closing here would brick a
            # speaker over a soft setting. Warn and run on defaults instead.
            await websocket.send_json({
                "type": "warning",
                "message": f"assigned profile '{profile_name}' is unavailable, using defaults",
            })
            profile_name = None
        else:
            await websocket.send_json(
                {"type": "error", "message": f"profile '{profile_name}' not found"}
            )
            await websocket.close()
            return

    requested_sid = hello.get("session_id")
    if not isinstance(requested_sid, str) or not requested_sid:
        requested_sid = None
    # Same IDOR conversation.py's WS /stream guards against (see
    # ws_session_owner_denied in auth_guard.py): requested_sid flows straight
    # into cfg.resume_sid below and is consumed by ConversationSession.start()
    # with no ownership check, so any device (or anyone driving the Lugo wire
    # protocol directly) that could guess or observe another user's session
    # id could resume -- read and corrupt -- their private conversation.
    # Checked before requested_sid is used for anything.
    if requested_sid and await ws_session_owner_denied(requested_sid, identity):
        # Lugo's own wire error shape ({"type": "error", ...}), NOT
        # conversation.py's {"event": "error", ...} -- same shape as every
        # other error send in this handler above/below.
        await websocket.send_json({
            "type": "error",
            "message": f"Session '{requested_sid}' not found",
        })
        await websocket.close()
        return
    session_id = requested_sid or str(uuid.uuid4())
    # Refused, not silently defaulted. The Lugo wire is Opus in both directions,
    # so a rate libopus rejects (anything outside OPUS_SAMPLE_RATES) makes
    # OpusFrameDecoder/OpusFrameEncoder raise from inside session.start() --
    # after accept(), with nothing on the wire to say why -- and sample_rate=0
    # additionally divides by zero in VadEndpointer. Same contract
    # api/routes/conversation.py already enforced on its own socket via
    # parse_sample_rate; this route was simply missed.
    audio_params = hello.get("audio_params")
    if not isinstance(audio_params, dict):
        audio_params = {}
    try:
        in_sr = parse_opus_sample_rate(
            audio_params.get("sample_rate"), settings.stt_stream_sample_rate
        )
        out_sr = parse_opus_sample_rate(
            audio_params.get("output_sample_rate"), 24000, name="output_sample_rate"
        )
    except AppError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return
    # The device wire protocol has only OPUS and JSON frames (lugo_frame.py), so a
    # server with no libopus cannot send this client audio at all. Ask for a
    # text-only session rather than letting session.start()'s opus->wav downgrade
    # push WAV bytes that emit_audio would frame as Opus (LUGO_FRAME_OPUS) --
    # corrupting the downlink instead of the old silent-audio-drop behavior.
    # Imported locally (not at module scope) so tests can monkeypatch
    # app.core.opus.opus_available and have it take effect here -- the same
    # reason session.py imports its make_*_or_downgrade helpers inside start()
    # rather than at module scope (those read opus_available as their own
    # module global, so a patch reaches them wherever they are called from).
    from app.core.opus import opus_available

    want_audio = opus_available()
    if not want_audio:
        logger.warning("lugo: no libopus on this server; device session is text-only")
    cfg = SessionRuntimeConfig(
        session_id=session_id, profile_name=profile_name, stt_engine=stt_engine, language=language,
        tts_engine=tts.engine, voice=tts.voice, ref_audio_path=tts.ref_audio_path,
        ref_text=tts.ref_text, tts_instruct=tts.instruct, tts_speed=tts.speed,
        tts_language=tts.language, sample_rate=in_sr, output_sample_rate=out_sr,
        audio_codec="opus", want_audio=want_audio, want_text=True, audio_out="opus",
        denoise=False, resume_sid=requested_sid, stt_model=stt_model, tts_model=tts.model_id,
        identity_user_id=identity.user_id,
        identity_unauthenticated=identity.unauthenticated,
        # A paired device is its own client: its thread follows the devices row, so
        # it survives reboots, NVS wipes and reflashes -- none of which the device
        # itself remembers across. Unpaired (fleet token, dev mode) means no
        # provenance and therefore no implicit resume, which is the honest answer
        # when there is nothing to say whose conversation it would be.
        source="device" if identity.device_id else "",
        client_id=identity.device_id or "",
    )

    speaking = False  # one tts{start} on first response/audio, one tts{stop} at turn end/abort
    # Idle countdown baseline. Refreshed on every server-side event (esp. the
    # turn's final turn_done/aborted) so the idle timer only starts AFTER the bot
    # finishes replying — a slow LLM/TTS or slow network never counts toward idle
    # (that window is also covered by session.is_turn_active() in the watchdog).
    last_activity = time.monotonic()
    engine_status = {"stt_ready": True, "tts_ready": True}

    async def emit(event: str, **payload) -> None:
        nonlocal speaking, last_activity
        # Real interaction refreshes the idle countdown; a VAD guess does not.
        # See refreshes_idle() for why, and _watchdog_body for how a sentence
        # longer than the idle window is still not cut off.
        if refreshes_idle(event, payload):
            last_activity = time.monotonic()
        if event == "user_transcript":
            await websocket.send_json({"type": "stt", "text": payload.get("text", ""), "final": True})
        elif event in ("response_text", "audio_start"):
            # First sign the bot is responding (text or audio) opens the turn.
            # response_text always precedes audio_start in the core, so tts{start}
            # is the first tts frame; this also works in the no-libopus,
            # text-only path (want_audio=False, see above) where audio_start
            # never fires at all -- only response_text does.
            if not speaking:
                speaking = True
                await websocket.send_json({"type": "tts", "state": "start"})
            if event == "response_text":
                await websocket.send_json({"type": "tts", "state": "sentence_start", "text": payload.get("text", "")})
        elif event in ("turn_done", "aborted"):
            if speaking:
                speaking = False
                stop_msg = {"type": "tts", "state": "stop"}
                if event == "aborted" and payload.get("reason"):
                    stop_msg["reason"] = payload["reason"]
                await websocket.send_json(stop_msg)
            # The hang-up lives HERE, in the speaking path, not in the watchdog:
            # whoever armed it (idle, or the end_conversation tool) is long gone by
            # now, and only this point knows the goodbye has actually been said.
            if session.close_after_speaking:
                if event == "aborted":
                    # The user cut in -- they are back, so there is nobody to say
                    # goodbye to. Stay.
                    logger.info("close disarmed: the user spoke over the goodbye")
                    session.close_after_speaking = None
                    last_activity = time.monotonic()
                else:
                    await _hang_up(session.close_after_speaking)
        elif event == "speech_start":
            await websocket.send_json({"type": "speech_start"})
        elif event == "speech_end":
            await websocket.send_json({"type": "speech_end", "speech_ms": payload.get("speech_ms", 0)})
        elif event == "processing":
            await websocket.send_json({"type": "processing", "turn": payload.get("turn", 0)})
        elif event == "command":
            await websocket.send_json({"type": "command", **payload})
        elif event == "error":
            await websocket.send_json({"type": "error", "message": payload.get("message", "")})
        elif event == "session_started":
            engine_status["stt_ready"] = bool(payload.get("stt_ready", True))
            engine_status["tts_ready"] = bool(payload.get("tts_ready", True))
        elif event == "session_rotated":
            # The device MUST see this: it persists session_id to disk and
            # resumes it on reconnect (rpi-assistant session_state.py), so a
            # device that missed the new id would reconnect straight back into
            # the conversation it just asked to leave.
            await websocket.send_json({
                "type": "session_new",
                "session_id": payload.get("session_id", ""),
                "previous_session_id": payload.get("previous_session_id", ""),
            })
        elif event == "engines_ready":
            await websocket.send_json({"type": "engines_ready"})
        # audio_end / reset: not on the wire

    async def emit_audio(packet: bytes) -> None:
        await websocket.send_bytes(encode_frame(LUGO_FRAME_OPUS, packet))

    async def _hang_up(reason: str, *, drain: bool = True) -> None:
        """Goodbye + close, once the last thing said has had time to be heard.

        `drain=False` when nothing was spoken (a connection that idled out without
        a conversation, a revoked account): there is no audio in flight, so waiting
        is just a delayed disconnect.

        Audio is paced in real time, so when the final packet is SENT the device
        still holds its prebuffer -- closing on the heels of the last packet cuts
        the goodbye off mid-word. The wait is computed from what the device is
        actually holding (prebuffer frames + 2 for jitter, at the encoder's frame
        size) rather than guessed, plus conversation_farewell_drain_s as a
        deliberate beat of silence before the line drops.
        """
        nonlocal closing, hung_up
        closing = True
        session.close_after_speaking = None
        conv_cfg = system_config_store.get().conversation
        frame_s = 0.06
        if session.opus_encoder is not None:
            frame_s = session.opus_encoder.frame / session.opus_encoder.sample_rate
        wait_s = 0.0
        if drain:
            wait_s = (conv_cfg.conversation_opus_prebuffer_frames + 2) * frame_s
            wait_s += conv_cfg.conversation_farewell_drain_s
        logger.info("hanging up (%s) after %.1fs of drain", reason, wait_s)
        if wait_s:
            await asyncio.sleep(wait_s)
        with contextlib.suppress(RuntimeError):
            await websocket.send_json({"type": "goodbye", "reason": reason})
        with contextlib.suppress(RuntimeError):
            await websocket.close()
        hung_up = True

    stt_health, tts_health = await check_resolved_engines(
        stt_engine, stt_model, tts.engine, tts.model_id
    )
    for health in (stt_health, tts_health):
        if health.blocks_session:
            await websocket.send_json({
                "type": "error",
                "message": f"{health.engine} is unavailable: {health.detail}",
            })
            await websocket.close()
            return

    session = ConversationSession(cfg, emit, emit_audio)
    transport: DeviceMcpTransport | None = None
    discovery_task: asyncio.Task | None = None
    wd: asyncio.Task | None = None
    closing = False      # a hang-up has been committed to; ignore further input
    hung_up = False      # ...and _hang_up has finished, so the socket is gone
    last_identity_check = time.monotonic()
    identity_owned = identity.user_id is not None or identity.device_id is not None

    async def _discover() -> None:
        defs = await discover_device_tools(
            transport, discovery_timeout=settings.device_mcp_discovery_timeout_s
        )
        if defs:
            session.add_tool_source(DeviceMcpToolSource(defs, transport))
            logger.info("device mcp: registered %d tool(s)", len(defs))

    async def _watchdog() -> None:
        """Wrapper so a failure in here is visible.

        The main loop treats `wd.done()` as "time to close" and never awaits the
        task, so an exception raised in the watchdog body used to kill the idle
        path AND the connection without a single line in the log: the socket just
        dropped at the idle mark with no goodbye. Anything that goes wrong while
        deciding to disconnect must say so."""
        try:
            await _watchdog_body()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - log, then let the connection close
            logger.exception("lugo idle watchdog failed; connection will close without goodbye")

    async def _watchdog_body() -> None:
        nonlocal closing, last_identity_check
        # Why the idle countdown is (not) advancing, logged once per streak rather
        # than per tick. Without it, "the timeout never fired" is indistinguishable
        # from "something reset it", and both look like total silence in the log --
        # which is how the pre-idle farewell stayed unexplained across several
        # rounds of hardware testing.
        held_reason: str | None = None
        while True:
            await asyncio.sleep(_IDLE_TICK_S)
            now = time.monotonic()
            if identity_owned and now - last_identity_check >= _IDENTITY_RECHECK_INTERVAL_S:
                last_identity_check = now
                if not await identity_still_valid(identity):
                    # No farewell here on purpose: a disabled or revoked account is
                    # cut off, not seen off.
                    session.close_after_speaking = None
                    await _hang_up("account_disabled", drain=False)
                    return
            # Hold (don't refresh) while a turn runs or the endpointer is
            # mid-utterance: a sentence longer than idle_timeout_s must not be cut
            # off, but noise that opens and closes an utterance must not push the
            # countdown forward either -- it resumes from the last real exchange.
            # NOT held on `endpointer.speaking`. Measured against the real VAD, an
            # open mic in a room with sustained sound leaves that flag set ~100% of
            # the time (loud constant noise: speaking 40s out of 40s, the endpoint
            # firing only at max_utterance_ms and speech_start reopening
            # immediately; TV-like bursts: 31.7s out of 40s). Holding on it meant
            # the idle countdown never completed on exactly the device this exists
            # for -- observed as 28 seconds of server silence after a real turn,
            # with the speaker hanging up on its own watchdog. A long real
            # utterance is protected by the turn it produces, and by the device's
            # own timeout being longer than this one.
            hold = "a turn is running" if session.is_turn_active() else None
            # A hold is a pause, never a veto: an endpointer that never closes its
            # utterance (an open mic in a noisy room) would otherwise keep the
            # connection alive forever, which is the failure this whole idle path
            # exists to prevent.
            if hold and now - last_activity >= idle + _MAX_IDLE_HOLD_S:
                logger.warning("idle hold overrun (%s); timing out anyway", hold)
                hold = None
            if hold != held_reason:
                logger.info(
                    "idle countdown %s (%.0fs since last interaction)",
                    f"held: {hold}" if hold else f"resumed after {held_reason}",
                    now - last_activity,
                )
                held_reason = hold
            if hold:
                continue
            if idle > 0 and now - last_activity >= idle:
                logger.info("idle timeout reached after %.0fs; disconnecting", now - last_activity)
                # Only when there is a conversation to take leave of. `history`
                # rather than `turn`: a turn counter is per-CONNECTION, so a device
                # that dropped and reconnected mid-conversation would be judged to
                # have said nothing and leave in silence.
                spoke = False
                if session.history:
                    # Arm BEFORE speaking: the hang-up now belongs to the speaking
                    # path (see emit's turn_done branch), which is the only place
                    # that knows the goodbye was actually said. The watchdog just
                    # starts it and gets out of the way -- no flag for the receive
                    # loop to trip over, no two tasks racing over one socket.
                    session.close_after_speaking = "idle_timeout"
                    # Buy time on the DEVICE's own watchdog before generating. It
                    # closes the link after idle_timeout_s + a few seconds of hearing
                    # nothing from the server, and writing this farewell costs an LLM
                    # call plus synthesis -- long enough that the device can hang up
                    # mid-sentence. Any server event resets that timer (rpi/esp32
                    # both treat it as activity), and `processing` is the honest one:
                    # something IS being prepared.
                    await session.emit("processing", turn=session.turn)
                    # Bounded: announce() awaits an LLM call and a synthesis
                    # with no deadline of its own, so a hung engine would park
                    # the watchdog here and keep the device connected forever.
                    try:
                        spoke = await asyncio.wait_for(
                            session.announce("idle_goodbye"), _FAREWELL_BUDGET_S
                        )
                    except TimeoutError:
                        logger.warning(
                            "farewell exceeded %.0fs; hanging up without it",
                            _FAREWELL_BUDGET_S,
                        )
                        spoke = False
                if not spoke:
                    # Nothing was said, so no turn_done is coming and nothing else
                    # would ever close this connection.
                    session.close_after_speaking = None
                    await _hang_up("idle_timeout", drain=False)
                return

    # start() is INSIDE the try, not before it. It resolves providers, builds
    # the tool registry and spawns a background warm-up, any of which can raise
    # (an engine that disappeared between the health probe and here, a codec the
    # negotiated rate can't support). Raising outside the try skipped the whole
    # finally: the session row was never marked ended, memory extraction never
    # ran, and the responder's httpx client leaked along with the warm-up task.
    try:
        await session.start()
        await websocket.send_json({
            # cfg.session_id, not the id minted above: start() may have resolved this
            # connection onto the client's existing conversation instead. The device
            # persists whatever id arrives here (rpi-assistant writes it to disk), so
            # reporting the pre-resume id would hand it one that nothing ever wrote to.
            "type": "welcome", "session_id": cfg.session_id, "transport": "websocket",
            "audio_params": {"sample_rate": out_sr}, "idle_timeout_s": idle,
            "stt_ready": engine_status["stt_ready"], "tts_ready": engine_status["tts_ready"],
        })

        if bool((hello.get("features") or {}).get("mcp")) and settings.device_mcp_enabled:
            transport = DeviceMcpTransport(
                websocket.send_json,
                request_timeout=settings.device_mcp_request_timeout_s,
            )
            discovery_task = asyncio.create_task(_discover())

        # Schedule the watchdog whenever there's something for it to watch: a real
        # idle timeout (idle > 0) or an identity to periodically recheck
        # (identity_owned). Skip scheduling entirely when neither applies, rather
        # than having it return immediately, since a completed task would make
        # `wd.done()` true on the very next loop check below and tear down the
        # connection mid-turn.
        #
        # Note: idle <= 0 ("never idle-disconnect") can still leave the watchdog
        # running when identity_owned is true — that's fine, because the
        # idle-check branch above is separately guarded with `idle > 0`, so it
        # can never fire in that case; only the identity re-check can.
        wd = asyncio.create_task(_watchdog()) if (idle > 0 or identity_owned) else None
        while True:
            if wd is not None and wd.done():
                break
            recv = asyncio.create_task(websocket.receive())
            waitables = {recv, wd} if wd is not None else {recv}
            done, _pending = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
            if closing:
                # _hang_up has committed: the goodbye is sent (or about to be) and
                # the socket is going. Drop whatever arrived instead of acting on
                # it, but keep looping -- the close itself ends this loop when
                # `recv` comes back as a disconnect. Tearing down from here is what
                # used to cut the farewell off mid-word on a device that streams
                # mic frames continuously, since `recv` completes constantly.
                recv.cancel()
                dropped = None
                with contextlib.suppress(asyncio.CancelledError):
                    dropped = await recv
                if hung_up or (wd is not None and wd.done()):
                    break
                # ...but a disconnect is not something to drain past. The peer is
                # gone, so there is no farewell left to protect, and Starlette
                # forbids a second receive() once one has reported a disconnect
                # (WebSocket.receive raises RuntimeError from its DISCONNECTED
                # branch) -- continuing here would take that path on the very next
                # iteration, and the RuntimeError would escape `except
                # WebSocketDisconnect` and cancel the in-flight _hang_up. Reaching
                # this needs the device to drop inside the drain window, which its
                # own watchdog can do; I could not stage it through TestClient, so
                # this is reasoned from Starlette's source rather than reproduced.
                if dropped is not None and dropped.get("type") == "websocket.disconnect":
                    break
                continue
            if wd is not None and wd in done:
                recv.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recv
                break
            message = recv.result()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                # Deliberately NOT activity. An always-listening device streams mic
                # frames continuously, so counting each frame kept last_activity
                # permanently fresh and the idle timeout below could never fire on
                # the very devices it exists for: the device's own watchdog
                # (idle_timeout_s + grace) closed the link first, which is why no
                # farewell was ever heard. Activity is what the emit wrapper above
                # sees -- speech detected by the VAD, a turn, audio playing -- which
                # is what docs/api.md has always said it was.
                await session.feed_audio(message["bytes"])
            if message.get("text") is not None:
                # A control message IS the client doing something on purpose.
                last_activity = time.monotonic()
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    control = {}
                ctype = control.get("type")
                if ctype == "text":
                    await session.feed_text(control.get("text") or "")
                elif ctype == "abort":
                    await session.abort("barge-in")
                elif ctype == "listen":
                    pass  # Phase 1 auto mode: server VAD drives turns
                elif ctype == "new_session":
                    # Start a fresh conversation without dropping the socket. A
                    # mains-powered speaker never disconnects, so without this its
                    # whole life is one session (see ConversationSession.rotate).
                    # request_rotate, not rotate: a device asks for this from its
                    # self.session.new tool mid-turn, and that turn has to be
                    # allowed to finish saying so.
                    await session.request_rotate("client")
                elif ctype == "mcp":
                    if transport is not None:
                        transport.on_message(control.get("payload") or {})
    except WebSocketDisconnect:
        pass
    finally:
        # `except Exception` around each await, not just CancelledError: a task
        # that had ALREADY failed re-raises its stored exception here, and an
        # exception escaping a finally block skips everything after it -- which
        # is session.close(), i.e. mark_ended + memory extraction + the
        # responder's httpx client. Teardown must reach the end no matter what
        # these two tasks did.
        for task in (wd, discovery_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass
        if transport is not None:
            transport.close()
        await session.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
