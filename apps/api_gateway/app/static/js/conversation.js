import { el, wsUrl, restoreAndBind, savePref } from "./helpers.js";
import { STREAM_SAMPLE_RATE, createMicCapture } from "./audio-capture.js";
import { currentSessionId, setCurrentSessionId } from "./chat.js";
import { profileData } from "./profiles.js";

export const conv = { ws: null, capture: null, log: [], ctx: null, nextTime: 0, sources: [], chain: null, assistantBubble: null, opusMode: false, opusDec: null, opusTs: 0, outRate: 24000 };

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
  el("chat-dialogue").appendChild(div);
  el("chat-dialogue").scrollTop = el("chat-dialogue").scrollHeight;
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
export function convEnqueueAudio(url) {
  conv.chain = (conv.chain || Promise.resolve())
    .then(async () => {
      const ctx = convAudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      const data = await (await fetch(url)).arrayBuffer();
      const buf = await ctx.decodeAudioData(data);
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
    })
    .catch((e) => convLog("audio error: " + e));
}

// ---- Opus downlink (WebCodecs): decode streamed Opus frames in-browser ----
// Same gapless scheduling as convEnqueueAudio, but the audio arrives as raw 60ms
// Opus packets (binary WS frames) decoded by AudioDecoder instead of fetched WAVs.
export function convOpusSupported() {
  return typeof window.AudioDecoder === "function" && typeof window.EncodedAudioChunk === "function";
}
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

// Model Registry options cached for client-side label resolution, so the chat
// header can show the ACTIVE profile's friendly STT/LLM labels before the
// session even starts (mirrors what the server resolves in session_started).
// Keyed the same way the /options endpoint returns: [{engine, model_id, label}].
export const convCatalog = { stt: [], llm: [] };

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

export function updateConvEnginesInfo() {
  const info = el("conv-engines-info");
  if (!info) return;
  const profileName = el("profile-select")?.value || "";
  if (profileName) {
    // A profile is authoritative for STT/LLM/TTS: show ITS models as friendly
    // registry labels (resolved client-side, same as the server would), so the
    // user sees exactly what's active without having to start the session.
    const p = profileData[profileName] || {};
    const sttLabel = catalogLabel("stt", p.stt?.engine || "", p.stt?.model || "") || "server default";
    const llmLabel = catalogLabel("llm", p.llm?.engine || "", p.llm?.model || "")
      || p.llm?.model || "server default";
    // TTS is a whole profile; its friendly name IS the linked TTS profile name.
    const ttsLabel = p.tts?.profile_name || "server default";
    info.textContent = `STT: ${sttLabel} · LLM: ${llmLabel} · TTS: ${ttsLabel}`;
    return;
  }
  // No profile — the user's own manual selections (or server defaults) apply.
  const sttEng = el("conv-stt-engine")?.value || "";
  const ttsProfileName = el("conv-tts-profile")?.value || "";
  const sttLabel = catalogLabel("stt", sttEng, "") || "server default";
  const sttPart = `STT: ${sttEng ? sttLabel : "server default"}`;
  const llmPart = convDetails.llm ? `LLM: ${convDetails.llm}` : "LLM: server default";
  const ttsPart = `TTS: ${ttsProfileName || "server default"}`;
  info.textContent = `${sttPart} · ${llmPart} · ${ttsPart}`;
}

// When a profile is selected, its STT/TTS/LLM are authoritative and the user
// must NOT hand-pick engines that would override them — the chat then simply
// "follows the profile". Only "(none — server defaults)" lets the user choose.
// LLM has no chat control, so it already follows the profile/server unconditionally.
export function applyConvProfileLock() {
  const profileActive = !!(el("profile-select")?.value || "");
  const sttSel = el("conv-stt-engine");
  const ttsSel = el("conv-tts-profile");
  const langInput = el("conv-language");
  [sttSel, ttsSel, langInput].forEach((sel) => {
    if (!sel) return;
    sel.disabled = profileActive;
    sel.title = profileActive
      ? "Following the selected profile — pick “(none — server defaults)” to choose engines manually"
      : "";
  });
  const hint = el("conv-engines-locked-hint");
  if (hint) hint.hidden = !profileActive;
}

// Called when the active profile changes: manual STT/TTS overrides must not
// silently keep beating the newly-selected profile's own config, so drop back
// to "follow the profile" (empty selection) and forget the sticky localStorage
// value that caused it to persist across profile switches.
export function resetConvManualOverrides() {
  const sttSel = el("conv-stt-engine");
  if (sttSel) sttSel.value = "";
  savePref("conv-stt-engine", "");
  const ttsSel = el("conv-tts-profile");
  if (ttsSel) ttsSel.value = "";
  savePref("conv-tts-profile", "");
  applyConvProfileLock();
  updateConvEnginesInfo();
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
    const fill = (id, items, label) => {
      const sel = el(id);
      if (!sel) return;
      sel.innerHTML = "";
      items.forEach((e) => {
        const opt = document.createElement("option");
        opt.value = e.engine;
        opt.textContent = label(e);
        sel.appendChild(opt);
      });
    };
    fill("conv-stt-engine", stt.data.filter((e) => e.available), (e) => `${e.engine}`);
    // Default to "follow profile / server default" rather than guessing an
    // engine — an explicit choice here always overrides the selected profile's
    // own STT config server-side, so it must stay empty unless the user opts in.
    const sttSel = el("conv-stt-engine");
    if (sttSel) {
      const sentinel = document.createElement("option");
      sentinel.value = "";
      sentinel.textContent = "(profile / server default)";
      sttSel.insertBefore(sentinel, sttSel.firstChild);
      sttSel.value = "";
    }
    restoreAndBind("conv-stt-engine");
    restoreAndBind("conv-language");
    restoreAndBind("conv-opus");
    restoreAndBind("conv-tts-profile");
    el("conv-stt-engine").addEventListener("change", updateConvEnginesInfo);
    el("conv-tts-profile")?.addEventListener("change", updateConvEnginesInfo);
    applyConvProfileLock();
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
  el("chat-dialogue").innerHTML = "";
  conv.log = [];
  el("conv-log").textContent = "";

  if (!convDetails.sttAvailable) {
    setConvStatus("No STT engine available", "status-error");
    setConvUI("idle");
    return;
  }
  // Empty selection means "follow the selected profile / server default" —
  // only a concrete choice here should override the profile's own STT config.
  const sttEngine = el("conv-stt-engine").value;
  const activeProfile = el("profile-select")?.value;

  // Warm up the STT engine so the first turn doesn't stall loading the model.
  // The /warm endpoint is a fast no-op for engines with no warm() method.
  setConvStatus("⏳ starting STT engine…", "status-idle");
  try {
    const warmParams = sttEngine
      ? `engine=${encodeURIComponent(sttEngine)}`
      : activeProfile
        ? `profile=${encodeURIComponent(activeProfile)}`
        : "";
    const warmRes = await fetch(`/v1/stt/warm?${warmParams}`, { method: "POST" });
    if (!warmRes.ok) {
      setConvStatus(`STT engine '${sttEngine || "(profile default)"}' not ready`, "status-error");
      setConvUI("idle");
      return;
    }
  } catch {
    setConvStatus("Could not connect to STT engine", "status-error");
    setConvUI("idle");
    return;
  }

  let params = `sample_rate=${STREAM_SAMPLE_RATE}`;
  if (sttEngine) params += `&stt_engine=${encodeURIComponent(sttEngine)}`;
  if (activeProfile) params += `&profile=${encodeURIComponent(activeProfile)}`;
  const ttsProfile = el("conv-tts-profile")?.value;
  if (ttsProfile) params += `&tts_profile=${encodeURIComponent(ttsProfile)}`;
  if (currentSessionId) params += `&session_id=${encodeURIComponent(currentSessionId)}`;
  // A locked language input (profile active) must not override the profile's
  // own STT language — only send it when the user can actually edit it.
  const langVal = el("conv-language");
  if (langVal && !langVal.disabled && langVal.value.trim()) {
    params += `&language=${encodeURIComponent(langVal.value.trim())}`;
  }

  // Opus downlink: stream reply audio as Opus frames decoded in-browser (WebCodecs).
  // Falls back to the default URL/WAV path if unchecked or unsupported.
  conv.opusMode = !!el("conv-opus")?.checked && convOpusSupported();
  if (el("conv-opus")?.checked && !conv.opusMode) {
    convLog("Opus downlink unsupported in this browser — using WAV/URL.");
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
  if (conv.opusMode) ws.binaryType = "arraybuffer";

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
    // Binary frames are Opus packets (audio_out=opus) — decode + play in-browser.
    if (typeof event.data !== "string") {
      convFeedOpus(event.data);
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
          const sttPart = `STT: ${d.stt_label || d.stt_engine}`;
          const llmPart = d.responder === "llm" ? `LLM: ${d.llm_label || d.llm_model}` : "LLM: echo (no LLM configured)";
          const ttsPart = `TTS: ${d.tts_label || d.tts_engine}`;
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
          el("chat-dialogue").scrollTop = el("chat-dialogue").scrollHeight;
        }
        break;
      case "audio_start":
        // Opus stream chunk begins; ensure the decoder matches the server's rate.
        if (d.codec === "opus" && d.sample_rate) conv.outRate = d.sample_rate;
        break;
      case "audio_chunk":
        if (d.audio_url) convEnqueueAudio(d.audio_url); // URL/WAV mode (non-opus)
        break;
      case "turn_done":
        setConvStatus("● listening", "status-rec");
        break;
      case "aborted":
        convStopAudio();  // barge-in / interrupt: stop playback immediately
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
  el("chat-dialogue").innerHTML = "";
  if (conv.ws && conv.ws.readyState === WebSocket.OPEN) conv.ws.send(JSON.stringify({ type: "reset" }));
});

