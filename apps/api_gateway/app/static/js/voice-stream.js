import { el, wsUrl, restoreAndBind } from "./helpers.js";
import { STREAM_SAMPLE_RATE, createMicCapture } from "./audio-capture.js";
import { currentSessionId, setCurrentSessionId, conversationMode } from "./conversation.js";
import { profileData } from "./profiles.js";

export const conv = { ws: null, capture: null, log: [], ctx: null, nextTime: 0, sources: [], chain: null, assistantBubble: null, opusMode: false, opusDec: null, opusTs: 0, outRate: 24000, outCodec: "wav", audioGen: 0 };

export function setConvStatus(text, state) {
  const node = el("conv-status");
  node.textContent = text;
  node.className = state;
}
export function convLog(line) {
  conv.log.push(line);
  if (conv.log.length > 60) conv.log.shift();
  el("conv-log").textContent = conv.log.join("\n");
}
export function addBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  el("conversation-dialogue").appendChild(div);
  el("conversation-dialogue").scrollTop = el("conversation-dialogue").scrollHeight;
  return div;
}
export function convAudioCtx() {
  if (!conv.ctx) conv.ctx = new (window.AudioContext || window.webkitAudioContext)();
  return conv.ctx;
}
// True while assistant audio is still scheduled/playing (+small tail).
export function convIsSpeaking() {
  return !!conv.ctx && (conv.nextTime || 0) > conv.ctx.currentTime + 0.15;
}
export function convStopAudio() {
  conv.audioGen = (conv.audioGen || 0) + 1; // invalidate any in-flight convEnqueueAudioBytes decode
  (conv.sources || []).forEach((s) => {
    try {
      s.stop();
    } catch {}
  });
  conv.sources = [];
  conv.nextTime = 0;
  conv.chain = Promise.resolve();
  convResetOpus(); // flush any in-flight Opus decode so stale audio can't play
}
// Gapless playback: decode each chunk and schedule it back-to-back on the audio
// timeline (no <audio> src-swap gaps, no queue underrun between sentences).
export function convScheduleBuffer(buf) {
  const ctx = convAudioCtx();
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(ctx.destination);
  const start = Math.max(ctx.currentTime + 0.05, conv.nextTime || 0);
  src.start(start);
  conv.nextTime = start + buf.duration;
  conv.sources.push(src);
  src.onended = () => {
    conv.sources = conv.sources.filter((s) => s !== src);
  };
}
// Reply audio arrives as one complete WAV/MP3 per sentence on a binary frame.
// A decode can still be in flight when convStopAudio() runs (barge-in);
// conv.audioGen (bumped there) is checked after the awaited decode so a
// stale sentence never gets scheduled after the interrupt -- the Opus path
// gets the same guarantee for free from convResetOpus() tearing down the
// decoder outright.
export function convEnqueueAudioBytes(bytes) {
  const gen = conv.audioGen;
  conv.chain = (conv.chain || Promise.resolve())
    .then(async () => {
      const ctx = convAudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      const buf = await ctx.decodeAudioData(bytes);
      if (gen !== conv.audioGen) return; // superseded by a barge-in/reset while decoding
      convScheduleBuffer(buf);
    })
    .catch((e) => convLog("audio error: " + e));
}

// ---- Opus downlink (WebCodecs): decode streamed Opus frames in-browser ----
// Same gapless scheduling as convEnqueueAudioBytes, but the audio arrives as
// raw 60ms Opus packets (binary WS frames) decoded by AudioDecoder instead of
// a full WAV/MP3 container.
export function convOpusSupported() {
  return typeof window.AudioDecoder === "function" && typeof window.EncodedAudioChunk === "function";
}
export function convInitOpusDecoder() {
  if (conv.opusDec) {
    try {
      conv.opusDec.close();
    } catch {}
  }
  conv.opusTs = 0;
  const ctx = convAudioCtx();
  const dec = new AudioDecoder({
    output: (audioData) => {
      try {
        const frames = audioData.numberOfFrames;
        const buf = ctx.createBuffer(1, frames, audioData.sampleRate);
        const arr = new Float32Array(frames);
        audioData.copyTo(arr, { planeIndex: 0, format: "f32-planar" });
        buf.copyToChannel(arr, 0);
        convScheduleBuffer(buf);
      } catch (e) {
        convLog("opus output error: " + e);
      } finally {
        audioData.close();
      }
    },
    error: (e) => convLog("opus decoder error: " + e),
  });
  dec.configure({ codec: "opus", sampleRate: conv.outRate, numberOfChannels: 1 });
  conv.opusDec = dec;
}
export function convFeedOpus(data) {
  if (!conv.opusDec || conv.opusDec.state === "closed") convInitOpusDecoder();
  try {
    conv.opusDec.decode(new EncodedAudioChunk({ type: "key", timestamp: conv.opusTs, data }));
    conv.opusTs += 60000; // 60ms frames (microseconds)
  } catch (e) {
    convLog("opus feed error: " + e);
  }
}
export function convResetOpus() {
  if (conv.opusDec) {
    try {
      conv.opusDec.close();
    } catch {}
    conv.opusDec = null;
  }
  conv.opusTs = 0;
}

export const convDetails = { stt: {}, llm: "", sttAvailable: true };

// Model Registry options cached for client-side label resolution, so the
// conversation header can show the ACTIVE profile's friendly STT/LLM labels before the
// session even starts (mirrors what the server resolves in session_started).
// Keyed the same way the /options endpoint returns: [{engine, model_id, label}].
export const convCatalog = { stt: [], llm: [] };

// Server-resolved defaults (what the server would pick when nothing explicit
// is set), keyed by kind: { engine, model_id, label } | null. Populated best-
// effort alongside convCatalog; stays null (falls back to "server default"
// text) if the endpoint is unavailable.
export const convServerDefaults = { stt: null, tts: null, llm: null };

export async function loadConvCatalog() {
  const fetchOpts = async (kind) => {
    try {
      const body = await (await fetch(`/v1/model_registry/options?kind=${kind}`)).json();
      return body.data || [];
    } catch {
      return [];
    }
  };
  const [stt, llm] = await Promise.all([fetchOpts("stt"), fetchOpts("llm")]);
  convCatalog.stt = stt;
  convCatalog.llm = llm;
  try {
    const body = await (await fetch("/v1/model_registry/defaults")).json();
    if (body.success && body.data) {
      convServerDefaults.stt = body.data.stt || null;
      convServerDefaults.tts = body.data.tts || null;
      convServerDefaults.llm = body.data.llm || null;
    }
  } catch { /* leave nulls -> falls back to "server default" text */ }
}

// Resolve (engine, model) to the registry label the SAME way the server does:
// exact (engine, model_id) match first, else the first enabled row for the
// engine, else the raw engine name. Returns "" when no engine is set.
function catalogLabel(kind, engine, model) {
  if (!engine) return "";
  const opts = convCatalog[kind] || [];
  const exact = opts.find((o) => o.engine === engine && o.model_id === (model || ""));
  if (exact?.label) return exact.label;
  const byEngine = opts.find((o) => o.engine === engine);
  if (byEngine?.label) return byEngine.label;
  return engine;
}

// The server default for a kind, shown with a "(default)" marker so the user
// can tell it apart from an explicit per-profile/manual selection.
function defaultLabel(kind) {
  const d = convServerDefaults[kind];
  const lbl = d && d.label;
  return lbl ? `${lbl} (default)` : "server default";
}

// Each mode only runs a subset of engines — show just the parts that mode
// actually uses (voice-voice: STT+LLM+TTS, voice-text: STT+LLM, text-voice:
// LLM+TTS, text-text: LLM only) instead of always listing all three.
function enginesForMode(mode) {
  return {
    stt: mode === "voice-voice" || mode === "voice-text",
    tts: mode === "voice-voice" || mode === "text-voice",
  };
}

// True when `kind` would fall back to the server default under the currently
// selected profile (no profile selected, or the profile leaves this kind
// unpinned) — the same condition updateConvEnginesInfo() uses to decide
// whether to append defaultLabel()'s "(default)" marker. Session_started's
// authoritative labels need the same marker so the "(default)" tag doesn't
// disappear the moment a live session overwrites the pre-session estimate.
function isServerDefault(kind) {
  const profileName = el("profile-select")?.value || "";
  if (!profileName) return true;
  const p = profileData[profileName] || {};
  if (kind === "stt") return !p.stt?.engine;
  if (kind === "llm") return !p.llm?.engine;
  if (kind === "tts") return !p.tts?.profile_name;
  return false;
}

export function annotateLabel(kind, label) {
  return isServerDefault(kind) ? `${label} (default)` : label;
}

export function updateConvEnginesInfo(mode = conversationMode) {
  const info = el("conv-engines-info");
  if (!info) return;
  const { stt: wantStt, tts: wantTts } = enginesForMode(mode);
  const profileName = el("profile-select")?.value || "";
  const parts = [];
  if (profileName) {
    // A profile is authoritative for STT/LLM/TTS: show ITS models as friendly
    // registry labels (resolved client-side, same as the server would), so the
    // user sees exactly what's active without having to start the session.
    const p = profileData[profileName] || {};
    if (wantStt) parts.push(`STT: ${catalogLabel("stt", p.stt?.engine || "", p.stt?.model || "") || defaultLabel("stt")}`);
    parts.push(`LLM: ${catalogLabel("llm", p.llm?.engine || "", p.llm?.model || "") || p.llm?.model || defaultLabel("llm")}`);
    // TTS is a whole profile; its friendly name IS the linked TTS profile name.
    if (wantTts) parts.push(`TTS: ${p.tts?.profile_name || defaultLabel("tts")}`);
  } else {
    // No profile — the server defaults apply (STT/TTS/language are no longer
    // hand-pickable here; a profile is the way to override them). This whole
    // branch IS the default path, so every part gets the "(default)" marker —
    // including convDetails.llm, which used to bypass defaultLabel() and show
    // a bare "LLM: openai/gpt-4o-mini" with no marker at all.
    if (wantStt) parts.push(`STT: ${defaultLabel("stt")}`);
    parts.push(`LLM: ${convDetails.llm ? `${convDetails.llm} (default)` : defaultLabel("llm")}`);
    if (wantTts) parts.push(`TTS: ${defaultLabel("tts")}`);
  }
  info.textContent = parts.join(" · ");
}

export async function loadConversationEngines() {
  try {
    const [stt, models] = await Promise.all([
      (await fetch("/v1/stt/engines")).json(),
      (await fetch("/v1/models")).json().catch(() => null),
      loadConvCatalog(),
    ]);
    stt.data.forEach((e) => (convDetails.stt[e.engine] = e.detail));
    convDetails.llm = models?.data?.llm?.active || "";
    convDetails.sttAvailable = stt.data.some((e) => e.available);
    restoreAndBind("conv-opus");
    updateConvEnginesInfo();
  } catch (error) {
    convLog(`engines error: ${error}`);
  }
}

export function setConvUI(state) {
  el("conv-start").disabled = state !== "idle";
  el("conv-stop").disabled = state === "idle";
}

export async function startConversation() {
  setConvUI("starting");
  convStopAudio();
  conv.assistantBubble = null;
  conv.outCodec = "wav"; // re-announced by the first audio_start of the new session
  el("conversation-dialogue").innerHTML = "";
  conv.log = [];
  el("conv-log").textContent = "";

  if (!convDetails.sttAvailable) {
    setConvStatus("No STT engine available", "status-error");
    setConvUI("idle");
    return;
  }
  const activeProfile = el("profile-select")?.value;

  // Warm up the STT engine so the first turn doesn't stall loading the model.
  // The /warm endpoint is a fast no-op for engines with no warm() method.
  setConvStatus("⏳ starting STT engine…", "status-idle");
  try {
    const warmParams = activeProfile ? `profile=${encodeURIComponent(activeProfile)}` : "";
    const warmRes = await fetch(`/v1/stt/warm?${warmParams}`, { method: "POST" });
    if (!warmRes.ok) {
      setConvStatus("STT engine (server default) not ready", "status-error");
      setConvUI("idle");
      return;
    }
  } catch {
    setConvStatus("Could not connect to STT engine", "status-error");
    setConvUI("idle");
    return;
  }

  let params = `sample_rate=${STREAM_SAMPLE_RATE}`;
  if (activeProfile) params += `&profile=${encodeURIComponent(activeProfile)}`;
  if (currentSessionId) params += `&session_id=${encodeURIComponent(currentSessionId)}`;

  // Opus downlink: stream reply audio as Opus frames decoded in-browser (WebCodecs).
  // Falls back to one-WAV-per-sentence binary frames if unchecked or unsupported.
  conv.opusMode = !!el("conv-opus")?.checked && convOpusSupported();
  if (el("conv-opus")?.checked && !conv.opusMode) {
    convLog("Opus downlink unsupported in this browser — using WAV.");
  }
  if (conv.opusMode) {
    conv.outRate = 24000;
    params += `&output=audio,text&audio_out=opus&output_sample_rate=${conv.outRate}`;
  }
  convResetOpus();

  let capture;
  try {
    capture = createMicCapture({
      onframe: (pcm) => {
        if (!conv.ws || conv.ws.readyState !== WebSocket.OPEN) return;
        // Half-duplex: while the assistant is speaking, don't send mic audio —
        // otherwise its own voice (speaker echo) is mistaken for the user
        // barging in and the reply gets cut off after 1-2 words.
        if (convIsSpeaking()) return;
        conv.ws.send(pcm.buffer);
      },
    });
  } catch (error) {
    setConvStatus("mic error", "status-error");
    setConvUI("idle");
    return;
  }

  const ws = new WebSocket(wsUrl(`/v1/conversation/stream?${params}`));
  conv.ws = ws;
  ws.binaryType = "arraybuffer";

  ws.onopen = async () => {
    try {
      await capture.start();
      conv.capture = capture;
      setConvStatus("● listening", "status-rec");
      setConvUI("recording");
    } catch (error) {
      setConvStatus("mic denied", "status-error");
      ws.close();
    }
  };

  ws.onmessage = (event) => {
    // Binary frames are reply audio: Opus packets when audio_out=opus, else a
    // complete WAV/MP3 per sentence -- routed by the codec the server
    // announced in the preceding audio_start event.
    if (typeof event.data !== "string") {
      if (conv.outCodec === "opus") convFeedOpus(event.data);
      else convEnqueueAudioBytes(event.data);
      return;
    }
    let d;
    try {
      d = JSON.parse(event.data);
    } catch {
      return;
    }
    convLog(`${d.event}: ${d.text ? d.text.slice(0, 60) : JSON.stringify({ ...d, event: undefined })}`);
    switch (d.event) {
      case "session_started": {
        if (d.session_id) setCurrentSessionId(d.session_id);
        if (d.output_sample_rate) conv.outRate = d.output_sample_rate;
        // Authoritative: show exactly which models this session is using, using
        // the friendly Model Registry labels the server resolved (never raw
        // engine names or verbose provider "detail" strings).
        const info = el("conv-engines-info");
        if (info) {
          const sttPart = `STT: ${annotateLabel("stt", d.stt_label || d.stt_engine)}`;
          const llmPart = d.responder === "llm" ? `LLM: ${annotateLabel("llm", d.llm_label || d.llm_model)}` : "LLM: echo (no LLM configured)";
          const ttsPart = `TTS: ${annotateLabel("tts", d.tts_label || d.tts_engine)}`;
          info.textContent = `${sttPart} · ${llmPart} · ${ttsPart}`;
        }
        // Engines may still be cold-loading — tell the user to hold off speaking
        // instead of letting them talk into a pipeline that'll lose the start of
        // their utterance. Cleared by the "engines_ready" event below.
        if (d.stt_ready === false || d.tts_ready === false) {
          setConvStatus("⏳ engines warming up, please wait…", "status-idle");
        }
        break;
      }
      case "engines_ready":
        setConvStatus("● listening", "status-rec");
        break;
      case "speech_start":
        setConvStatus("● you're speaking", "status-rec");
        break;
      case "speech_end":
        setConvStatus("… thinking", "status-idle");
        break;
      case "user_transcript":
        if (d.text) addBubble("user", d.text);
        break;
      case "response_text":
        // Sentences stream in; build one assistant bubble per turn.
        if (d.chunk_index === 0 || !conv.assistantBubble) {
          conv.assistantBubble = addBubble("assistant", d.text);
        } else {
          conv.assistantBubble.textContent += " " + d.text;
          el("conversation-dialogue").scrollTop = el("conversation-dialogue").scrollHeight;
        }
        break;
      case "audio_start":
        conv.outCodec = d.codec || "wav";
        if (d.codec === "opus" && d.sample_rate) conv.outRate = d.sample_rate;
        break;
      case "turn_done":
        setConvStatus("● listening", "status-rec");
        break;
      case "aborted":
        convStopAudio();  // barge-in / interrupt: stop playback immediately
        break;
      case "tts_error":
        // The reply text already streamed in via response_text; only the audio
        // failed. Flag it on the bubble (persistent) and log it, WITHOUT the fatal
        // status-error treatment -- the turn keeps going (turn_done still follows).
        convLog(`tts_error: ${d.message || ""}`);
        if (conv.assistantBubble) conv.assistantBubble.textContent += " 🔇 (lỗi TTS — chỉ hiển thị văn bản)";
        break;
      case "error":
        setConvStatus(`error: ${d.message || ""}`, "status-error");
        break;
    }
  };

  ws.onerror = () => setConvStatus("ws error", "status-error");
  ws.onclose = () => {
    setConvUI("idle");
    if (el("conv-status").className !== "status-error") setConvStatus("idle", "status-idle");
  };
}

export function stopConversation() {
  if (conv.capture) {
    conv.capture.stop();
    conv.capture = null;
  }
  if (conv.ws) {
    if (conv.ws.readyState === WebSocket.OPEN) conv.ws.send(JSON.stringify({ type: "end" }));
    try {
      conv.ws.close();
    } catch {}
    conv.ws = null;
  }
  setConvUI("idle");
}

el("conv-start").addEventListener("click", startConversation);
el("conv-stop").addEventListener("click", () => {
  convStopAudio();
  stopConversation();
});
el("conv-reset").addEventListener("click", () => {
  convStopAudio();
  conv.assistantBubble = null;
  el("conversation-dialogue").innerHTML = "";
  if (conv.ws && conv.ws.readyState === WebSocket.OPEN) conv.ws.send(JSON.stringify({ type: "reset" }));
});

