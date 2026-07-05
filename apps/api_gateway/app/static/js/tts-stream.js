import { el, print } from "./helpers.js";

export const ttsStream = { source: null, queue: [], playing: false, lines: [] };

export function stopEventSource() {
  if (ttsStream.source) {
    ttsStream.source.close();
    ttsStream.source = null;
  }
}
export function appendStreamEvent(label, payload) {
  ttsStream.lines.push(`${label}: ${JSON.stringify(payload)}`);
  el("tts-stream-events").textContent = ttsStream.lines.join("\n");
}
export function playNextChunk() {
  const audio = el("tts-stream-audio");
  if (ttsStream.playing) return;
  const next = ttsStream.queue.shift();
  if (!next) return;
  ttsStream.playing = true;
  audio.src = next;
  audio.classList.remove("hidden");
  audio.play().catch(() => {
    ttsStream.playing = false;
  });
}
el("tts-stream-audio").addEventListener("ended", () => {
  ttsStream.playing = false;
  playNextChunk();
});
el("tts-stream-stop").addEventListener("click", () => {
  stopEventSource();
  el("tts-job").textContent = "Stopped listening SSE";
});

el("tts-stream-start").addEventListener("click", async () => {
  const text = el("tts-stream-text").value.trim();
  if (!text) {
    print(el("tts-stream-events"), "Please provide stream text", true);
    return;
  }
  stopEventSource();
  ttsStream.queue = [];
  ttsStream.playing = false;
  ttsStream.lines = [];
  el("tts-stream-chunks").innerHTML = "";
  el("tts-stream-audio").classList.add("hidden");
  print(el("tts-stream-events"), "Creating stream job...");

  try {
    const response = await fetch("/v1/tts/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, engine: el("tts-stream-engine").value || "omnivoice" }),
    });
    const body = await response.json();
    if (!response.ok || !body.success) {
      print(el("tts-stream-events"), body, true);
      return;
    }
    const jobId = body.data.job_id;
    el("tts-job").textContent = `Job ID: ${jobId}`;

    const source = new EventSource(`/v1/events/jobs/${jobId}`);
    ttsStream.source = source;

    source.addEventListener("queued", (e) => appendStreamEvent("queued", JSON.parse(e.data)));
    source.addEventListener("audio_chunk", (e) => {
      const payload = JSON.parse(e.data).payload;
      appendStreamEvent("audio_chunk", payload);
      renderChunkItem(payload);
      if (payload.audio_url) {
        ttsStream.queue.push(payload.audio_url);
        playNextChunk();
      }
    });
    source.addEventListener("error", (e) => {
      try {
        appendStreamEvent("error", JSON.parse(e.data).payload);
      } catch {
        ttsStream.lines.push("error: SSE connection closed or failed");
        el("tts-stream-events").textContent = ttsStream.lines.join("\n");
      }
    });
    source.addEventListener("done", (e) => {
      appendStreamEvent("done", JSON.parse(e.data));
      stopEventSource();
    });
  } catch (error) {
    print(el("tts-stream-events"), { error: String(error) }, true);
  }
});

export function renderChunkItem(payload) {
  const li = document.createElement("li");
  const text = document.createElement("span");
  text.className = "chunk-text";
  text.textContent = payload.text || "(chunk)";
  li.appendChild(text);
  if (payload.mock) {
    const badge = document.createElement("span");
    badge.className = "badge mock";
    badge.textContent = "mock";
    li.appendChild(badge);
  }
  if (payload.audio_url) {
    const play = document.createElement("button");
    play.className = "mini";
    play.textContent = "▶";
    play.addEventListener("click", () => {
      const audio = el("tts-stream-audio");
      audio.src = payload.audio_url;
      audio.classList.remove("hidden");
      audio.play().catch(() => {});
    });
    li.appendChild(play);
  }
  el("tts-stream-chunks").appendChild(li);
}

