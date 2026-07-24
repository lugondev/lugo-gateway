import { el, wsUrl } from "./helpers.js";
import { conv, convStopAudio } from "./conversation.js";
import { STREAM_SAMPLE_RATE, createMicCapture } from "./audio-capture.js";

export const CHAT_MODES = {
  "text-text":   { title: "Text Chat",      hint: "Text chat with the configured LLM." },
  "voice-voice": { title: "Voice Chat",     hint: "Speak — VAD detects your pause, transcribes, LLM replies, TTS plays back." },
  "voice-text":  { title: "Voice → Text",   hint: "Live microphone transcription. No LLM, no TTS." },
  "text-voice":  { title: "Text → Voice",   hint: "Type text to synthesize and play back." },
};
export let chatMode = "text-text";
export const v2t = { ws: null, capture: null };

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
  const enginesInfo = el("conv-engines-info");
  if (enginesInfo) enginesInfo.classList.toggle("hidden", mode !== "voice-voice");
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

export async function startV2t() {
  if (el("v2t-partial")) el("v2t-partial").textContent = "—";
  if (el("v2t-log")) el("v2t-log").textContent = "";

  // No manual engine/language picker here — omitting them lets the backend use
  // its configured default STT engine (and auto language detection).
  const params = `sample_rate=${STREAM_SAMPLE_RATE}`;

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

  const ws = new WebSocket(wsUrl(`/v1/stt/stream?${params}`));
  v2t.ws = ws;

  ws.onopen = async () => {
    try {
      await capture.start();
      v2t.capture = capture;
      setV2tStatus("● recording", "status-rec");
      setV2tUI("recording");
    } catch {
      setV2tStatus("mic denied", "status-error");
      ws.close();
    }
  };

  ws.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (el("v2t-log")) {
      const lines = el("v2t-log").textContent.split("\n");
      lines.push(`${data.event_type}: ${JSON.stringify(data.payload ?? {})}`);
      if (lines.length > 30) lines.shift();
      el("v2t-log").textContent = lines.join("\n");
    }
    if (data.event_type === "partial") {
      if (el("v2t-partial")) el("v2t-partial").textContent = data.payload?.text || "…";
    } else if (data.event_type === "final") {
      const text = (data.payload?.text || "").trim();
      if (text) chatBubble("user", text);
      if (el("v2t-partial")) el("v2t-partial").textContent = "—";
    } else if (data.event_type === "done") {
      stopV2t();
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
if (el("t2v-submit")) {
  el("t2v-submit").addEventListener("click", async () => {
    const text = el("t2v-text")?.value.trim();
    const audio = el("t2v-audio");
    const meta = el("t2v-meta");
    if (!text) return;
    if (meta) meta.textContent = "Synthesizing…";
    if (audio) audio.classList.add("hidden");
    try {
      // No manual engine/voice picker here — resolve the server's default TTS
      // engine (TTSRequest otherwise defaults to a hardcoded "omnivoice").
      let engine;
      try {
        const d = await (await fetch("/v1/model_registry/defaults")).json();
        engine = d?.data?.tts?.engine || undefined;
      } catch {
        engine = undefined;
      }
      const resp = await fetch("/v1/tts/synthesize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, engine }),
      });
      const body = await resp.json();
      if (!resp.ok) { if (meta) meta.textContent = body.error || "TTS error"; return; }
      if (body.data?.audio_url && audio) {
        audio.src = body.data.audio_url;
        audio.classList.remove("hidden");
        audio.play().catch(() => {});
        if (meta) meta.textContent = `${body.data.duration_seconds ?? "?"}s @ ${body.data.sample_rate}Hz${body.data.process_seconds != null ? ` · synthesized in ${body.data.process_seconds}s` : ""}`;
      }
    } catch (error) {
      if (meta) meta.textContent = String(error);
    }
  });
}
