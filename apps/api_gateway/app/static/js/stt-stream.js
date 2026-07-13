import { el, wsUrl } from "./helpers.js";
import { STREAM_SAMPLE_RATE, createMicCapture } from "./audio-capture.js";
import { getPreproc } from "./system-config.js";

export const sttStream = { ws: null, capture: null, transcript: "", logLines: [] };

export function setStreamStatus(text, state) {
  const node = el("stt-stream-status");
  node.textContent = text;
  node.className = state;
}

// Single source of truth for the streaming buttons.
// idle: only Start enabled · starting: both disabled · recording: only Stop enabled
export function setStreamUI(state) {
  el("stt-stream-record").disabled = state !== "idle";
  el("stt-stream-end").disabled = state !== "recording";
}

export function toggleStreamingAvailability(enabled) {
  // No realtime-capable engine (e.g. no Vosk model) -> hide the whole streaming card.
  const card = el("stt-stream-card");
  if (card) card.classList.toggle("hidden", !enabled);
  if (enabled && !sttStream.ws) setStreamUI("idle");
}
export function appendStreamLog(label, payload) {
  sttStream.logLines.push(`${label}: ${JSON.stringify(payload)}`);
  if (sttStream.logLines.length > 50) sttStream.logLines.shift();
  el("stt-stream-log").textContent = sttStream.logLines.join("\n");
}

export async function startStreaming() {
  setStreamUI("starting");
  const engine = el("stt-stream-engine").value || "vosk";
  const language = el("stt-stream-language").value.trim();
  sttStream.transcript = "";
  sttStream.logLines = [];
  el("stt-stream-transcript").textContent = "";
  el("stt-stream-partial").textContent = "—";
  el("stt-stream-log").textContent = "";

  const pp = getPreproc();
  let params = `engine=${encodeURIComponent(engine)}&sample_rate=${STREAM_SAMPLE_RATE}`;
  if (language) params += `&language=${encodeURIComponent(language)}`;
  params += `&denoise=${pp.denoise}&vad=${pp.vad}&vad_backend=${encodeURIComponent(pp.backend)}`;

  let capture;
  try {
    capture = createMicCapture({
      onframe: (pcm) => {
        if (sttStream.ws && sttStream.ws.readyState === WebSocket.OPEN) sttStream.ws.send(pcm.buffer);
      },
    });
  } catch (error) {
    setStreamStatus("mic error", "status-error");
    setStreamUI("idle");
    return;
  }

  const ws = new WebSocket(wsUrl(`/v1/stt/stream?${params}`));
  sttStream.ws = ws;

  ws.onopen = async () => {
    try {
      await capture.start();
      sttStream.capture = capture;
      setStreamStatus("● recording", "status-rec");
      setStreamUI("recording");
    } catch (error) {
      setStreamStatus("mic denied", "status-error");
      ws.close();
    }
  };

  ws.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }
    appendStreamLog(data.event_type, data.payload ?? {});
    if (data.event_type === "partial") {
      el("stt-stream-partial").textContent = data.payload?.text || "…";
    } else if (data.event_type === "final") {
      const text = (data.payload?.text || "").trim();
      if (text) {
        sttStream.transcript = `${sttStream.transcript} ${text}`.trim();
        el("stt-stream-transcript").textContent = sttStream.transcript;
      }
      el("stt-stream-partial").textContent = "—";
    } else if (data.event_type === "error") {
      setStreamStatus("error", "status-error");
    } else if (data.event_type === "done") {
      stopStreaming();
    }
  };

  ws.onerror = () => setStreamStatus("ws error", "status-error");
  ws.onclose = () => {
    setStreamUI("idle");
    if (el("stt-stream-status").className !== "status-error") setStreamStatus("idle", "status-idle");
  };
}

export function endStreaming() {
  setStreamUI("starting");
  setStreamStatus("finalizing…", "status-idle");
  if (sttStream.capture) {
    sttStream.capture.stop();
    sttStream.capture = null;
  }
  if (sttStream.ws && sttStream.ws.readyState === WebSocket.OPEN) {
    sttStream.ws.send(JSON.stringify({ type: "end" }));
  }
}
export function stopStreaming() {
  if (sttStream.capture) {
    sttStream.capture.stop();
    sttStream.capture = null;
  }
  if (sttStream.ws) {
    if (sttStream.ws.readyState === WebSocket.OPEN) sttStream.ws.close();
    sttStream.ws = null;
  }
}

el("stt-stream-record").addEventListener("click", startStreaming);
el("stt-stream-end").addEventListener("click", endStreaming);

