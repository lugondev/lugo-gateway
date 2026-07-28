import { el, wsUrl } from "./helpers.js";
import { conv, convStopAudio, updateConvEnginesInfo, annotateLabel } from "./conversation.js";
import { STREAM_SAMPLE_RATE, createMicCapture } from "./audio-capture.js";

export const CHAT_MODES = {
  "text-text":   { title: "Text Chat",      hint: "Text chat with the LLM." },
  "voice-voice": { title: "Voice Chat",     hint: "Speak — VAD detects your pause, STT transcribes, the LLM replies, TTS speaks the reply." },
  "voice-text":  { title: "Voice → Text",   hint: "Speak — VAD detects your pause, STT transcribes, the LLM replies as text (no TTS)." },
  "text-voice":  { title: "Text → Voice",   hint: "Type a message — the LLM replies and TTS speaks the reply." },
};
export let chatMode = "text-text";
export const v2t = { ws: null, capture: null, assistantBubble: null };

export const chat = { history: [], busy: false };
export let currentSessionId = null;

export function chatBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  const dialogue = el("chat-dialogue");
  if (dialogue) {
    dialogue.appendChild(div);
    dialogue.scrollTop = dialogue.scrollHeight;
  }
  return div;
}

export async function sendChat() {
  const input = el("chat-input");
  const text = input.value.trim();
  if (!text || chat.busy) return;
  chat.busy = true;
  el("chat-send").disabled = true;
  input.value = "";

  chat.history.push({ role: "user", content: text });
  chatBubble("user", text);
  const pending = chatBubble("assistant", "…");

  try {
    const profileVal = el("profile-select")?.value;
    const params = new URLSearchParams();
    if (profileVal) params.set("profile", profileVal);
    if (currentSessionId) params.set("session_id", currentSessionId);
    const qs = params.toString();
    const chatUrl = qs ? `/v1/conversation/chat?${qs}` : "/v1/conversation/chat";
    // Send only the new turn: the backend prefixes stored session context by
    // session_id, so resending chat.history here would double up persisted
    // messages and duplicate context sent to the LLM on every subsequent turn.
    const resp = await fetch(chatUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: [{ role: "user", content: text }] }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      pending.textContent = `error: ${body.error || JSON.stringify(body)}`;
      pending.classList.add("error");
    } else {
      pending.textContent = body.data.reply;
      chat.history.push({ role: "assistant", content: body.data.reply });
      if (body.data.session_id) currentSessionId = body.data.session_id;
      el("chat-hint").textContent = `Responder: ${body.data.responder}${body.data.responder === "llm" ? " · " + body.data.model : " (no LLM configured — enable one in Model Registry)"}`;
    }
  } catch (error) {
    pending.textContent = `error: ${error}`;
    pending.classList.add("error");
  } finally {
    chat.busy = false;
    el("chat-send").disabled = false;
    input.focus();
  }
}

el("chat-send").addEventListener("click", sendChat);
el("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
});
el("chat-reset").addEventListener("click", () => {
  chat.history = [];
  currentSessionId = null;
  const dialogue = el("chat-dialogue");
  if (dialogue) dialogue.innerHTML = "";
  convStopAudio();
  conv.assistantBubble = null;
  // Also clear the Text→Voice player/meta (shares this transcript + session).
  const t2vAudio = el("t2v-audio");
  if (t2vAudio) { t2vAudio.pause(); t2vAudio.classList.add("hidden"); }
  const t2vMeta = el("t2v-meta");
  if (t2vMeta) t2vMeta.textContent = "";
});


export function setCurrentSessionId(v) {
  currentSessionId = v;
}

export function setChatMode(mode) {
  chatMode = mode;
  document.querySelectorAll("#chat-mode-seg .seg-btn").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-mode") === mode);
  });
  ["text-text", "voice-voice", "voice-text", "text-voice"].forEach((m) => {
    const pane = el(`mode-${m}`);
    if (pane) pane.classList.toggle("hidden", m !== mode);
  });
  const info = CHAT_MODES[mode] || {};
  if (el("chat-section-title")) el("chat-section-title").textContent = info.title || "Chat";
  if (el("chat-hint")) el("chat-hint").textContent = info.hint || "";
  updateConvEnginesInfo(mode);
}

export function initChatModes() {
  document.querySelectorAll("#chat-mode-seg .seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => setChatMode(btn.getAttribute("data-mode")));
  });
  setChatMode("text-text");
}


export function setV2tUI(state) {
  const start = el("v2t-start");
  const stop = el("v2t-stop");
  if (start) start.disabled = state !== "idle";
  if (stop) stop.disabled = state !== "recording";
}

export function setV2tStatus(text, cls) {
  const e = el("v2t-status");
  if (e) { e.textContent = text; e.className = cls; }
}

// Voice→Text drives the SAME conversation pipeline as Voice→Voice (endpointer
// -> STT -> LLM) via /v1/conversation/stream, just with output=text so the
// server skips TTS synthesis entirely. This keeps LLM replies, history, and
// session_id shared across all chat modes instead of Voice→Text being a bare
// STT passthrough with no LLM turn.
export async function startV2t() {
  if (el("v2t-log")) el("v2t-log").textContent = "";
  v2t.assistantBubble = null;

  const activeProfile = el("profile-select")?.value;

  setV2tStatus("⏳ starting STT engine…", "status-idle");
  try {
    const warmParams = activeProfile ? `profile=${encodeURIComponent(activeProfile)}` : "";
    const warmRes = await fetch(`/v1/stt/warm?${warmParams}`, { method: "POST" });
    if (!warmRes.ok) {
      setV2tStatus("STT engine (server default) not ready", "status-error");
      return;
    }
  } catch {
    setV2tStatus("Could not connect to STT engine", "status-error");
    return;
  }

  let params = `sample_rate=${STREAM_SAMPLE_RATE}&output=text`;
  if (activeProfile) params += `&profile=${encodeURIComponent(activeProfile)}`;
  if (currentSessionId) params += `&session_id=${encodeURIComponent(currentSessionId)}`;

  let capture;
  try {
    capture = createMicCapture({
      onframe: (pcm) => {
        if (v2t.ws && v2t.ws.readyState === WebSocket.OPEN) v2t.ws.send(pcm.buffer);
      },
    });
  } catch {
    setV2tStatus("mic error", "status-error");
    return;
  }

  const ws = new WebSocket(wsUrl(`/v1/conversation/stream?${params}`));
  v2t.ws = ws;

  ws.onopen = async () => {
    try {
      await capture.start();
      v2t.capture = capture;
      setV2tStatus("● listening", "status-rec");
      setV2tUI("recording");
    } catch {
      setV2tStatus("mic denied", "status-error");
      ws.close();
    }
  };

  ws.onmessage = (event) => {
    let d;
    try { d = JSON.parse(event.data); } catch { return; }
    if (el("v2t-log")) {
      const lines = el("v2t-log").textContent.split("\n");
      lines.push(`${d.event}: ${d.text ? d.text.slice(0, 60) : JSON.stringify({ ...d, event: undefined })}`);
      if (lines.length > 30) lines.shift();
      el("v2t-log").textContent = lines.join("\n");
    }
    switch (d.event) {
      case "session_started": {
        if (d.session_id) currentSessionId = d.session_id;
        // Authoritative: show exactly which STT/LLM this session resolved to
        // (mirrors Voice→Voice's session_started handling in conversation.js).
        const info = el("conv-engines-info");
        if (info) {
          const llmPart = d.responder === "llm" ? `LLM: ${annotateLabel("llm", d.llm_label || d.llm_model)}` : "LLM: echo (no LLM configured)";
          info.textContent = `STT: ${annotateLabel("stt", d.stt_label || d.stt_engine)} · ${llmPart}`;
        }
        break;
      }
      case "engines_ready":
      case "turn_done":
        setV2tStatus("● listening", "status-rec");
        break;
      case "speech_start":
        setV2tStatus("● you're speaking", "status-rec");
        break;
      case "speech_end":
        setV2tStatus("… thinking", "status-idle");
        break;
      case "user_transcript":
        if (d.text) chatBubble("user", d.text);
        break;
      case "response_text":
        if (d.chunk_index === 0 || !v2t.assistantBubble) {
          v2t.assistantBubble = chatBubble("assistant", d.text);
        } else {
          v2t.assistantBubble.textContent += " " + d.text;
        }
        break;
      case "error":
        setV2tStatus(`error: ${d.message || ""}`, "status-error");
        break;
    }
  };

  ws.onerror = () => setV2tStatus("ws error", "status-error");
  ws.onclose = () => {
    setV2tUI("idle");
    if (el("v2t-status")?.className !== "status-error") setV2tStatus("idle", "status-idle");
  };
}

export function stopV2t() {
  if (v2t.capture) { v2t.capture.stop(); v2t.capture = null; }
  if (v2t.ws) {
    if (v2t.ws.readyState === WebSocket.OPEN) v2t.ws.send(JSON.stringify({ type: "end" }));
    v2t.ws = null;
  }
}

if (el("v2t-start")) el("v2t-start").addEventListener("click", startV2t);
if (el("v2t-stop")) el("v2t-stop").addEventListener("click", stopV2t);
setV2tUI("idle");

// ============================================================ text→voice (in chat section)
// A chat turn with a spoken answer (NOT raw text-to-speech): type text -> LLM
// replies -> the reply is synthesized and played. History/session/transcript
// are handled IDENTICALLY to Text→Text -- same chat.history, same shared
// #chat-dialogue bubbles, same currentSessionId (so New session / Sessions /
// Reset and multi-turn context all work the same across modes). It just adds a
// spoken reply on top via the server's default TTS engine. Pure STT/TTS
// utilities live in Voice→Text.
async function sendTextToVoice() {
  const text = el("t2v-text")?.value.trim();
  const audio = el("t2v-audio");
  const meta = el("t2v-meta");
  if (!text || chat.busy) return;
  chat.busy = true;
  el("t2v-submit").disabled = true;
  if (el("t2v-text")) el("t2v-text").value = "";
  if (audio) audio.classList.add("hidden");
  if (meta) meta.textContent = "";

  // Same transcript + history as sendChat().
  chat.history.push({ role: "user", content: text });
  chatBubble("user", text);
  const pending = chatBubble("assistant", "…");

  try {
    // 1) text -> LLM (same endpoint/params as sendChat: profile + session apply).
    const profileVal = el("profile-select")?.value;
    const params = new URLSearchParams();
    if (profileVal) params.set("profile", profileVal);
    if (currentSessionId) params.set("session_id", currentSessionId);
    const qs = params.toString();
    const chatResp = await fetch(qs ? `/v1/conversation/chat?${qs}` : "/v1/conversation/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: [{ role: "user", content: text }] }),
    });
    const chatBody = await chatResp.json();
    if (!chatResp.ok) {
      pending.textContent = `error: ${chatBody.error || JSON.stringify(chatBody)}`;
      pending.classList.add("error");
      return;
    }
    const reply = (chatBody.data?.reply || "").trim();
    pending.textContent = reply || "(no reply)";
    chat.history.push({ role: "assistant", content: reply });
    if (chatBody.data?.session_id) currentSessionId = chatBody.data.session_id;
    if (!reply) return;

    // 2) reply -> TTS (server's default engine; omit voice so vieneu uses the
    //    system default voice).
    if (meta) meta.textContent = "Synthesizing reply…";
    let engine;
    try {
      const d = await (await fetch("/v1/model_registry/defaults")).json();
      engine = d?.data?.tts?.engine || undefined;
    } catch {
      engine = undefined;
    }
    const ttsResp = await fetch("/v1/tts/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: reply, engine }),
    });
    if (!ttsResp.ok) {
      // Error responses are still JSON (see app.core.errors's AppError handler);
      // only the 200 body is now raw audio bytes.
      let ttsBody = {};
      try { ttsBody = await ttsResp.json(); } catch { /* non-JSON body */ }
      if (meta) meta.textContent = ttsBody.error || "TTS error";
      return;
    }
    if (audio) {
      // POST /v1/tts/synthesize now returns the audio bytes directly (see
      // routes/tts.py), not a JSON body with an /artifacts/ URL. Metadata
      // travels in X-TTS-* response headers instead.
      const blob = await ttsResp.blob();
      if (audio.dataset.objectUrl) URL.revokeObjectURL(audio.dataset.objectUrl);
      const objectUrl = URL.createObjectURL(blob);
      audio.dataset.objectUrl = objectUrl;
      audio.src = objectUrl;
      audio.classList.remove("hidden");
      audio.play().catch(() => {});
      const duration = ttsResp.headers.get("X-TTS-Duration-Seconds");
      const sampleRate = ttsResp.headers.get("X-TTS-Sample-Rate");
      const processSeconds = ttsResp.headers.get("X-TTS-Process-Seconds");
      if (meta) meta.textContent = `${duration ?? "?"}s @ ${sampleRate}Hz${processSeconds != null ? ` · synthesized in ${processSeconds}s` : ""}`;
    }
  } catch (error) {
    pending.textContent = `error: ${error}`;
    pending.classList.add("error");
  } finally {
    chat.busy = false;
    el("t2v-submit").disabled = false;
    if (el("t2v-text")) el("t2v-text").focus();
  }
}

if (el("t2v-submit")) {
  el("t2v-submit").addEventListener("click", sendTextToVoice);
  el("t2v-text")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendTextToVoice();
    }
  });
}
