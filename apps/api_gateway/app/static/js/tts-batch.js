import { el, print, quotaMessage } from "./helpers.js";

el("tts-submit").addEventListener("click", async () => {
  const ttsText = el("tts-text");
  const ttsResult = el("tts-result");
  const ttsAudio = el("tts-audio");
  const ttsMeta = el("tts-meta");
  if (!ttsText.value.trim()) {
    print(ttsResult, "Please provide text", true);
    return;
  }
  print(ttsResult, "Synthesizing...");
  ttsAudio.classList.add("hidden");
  ttsMeta.textContent = "";
  try {
    const [engine = "", model = ""] = (el("tts-engine").value || "").split("|");
    const response = await fetch("/v1/tts/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: ttsText.value.trim(),
        engine: engine || "omnivoice",
        model_id: model,
        voice: el("tts-voice").value || null,
      }),
    });
    if (!response.ok) {
      // Error responses are still JSON (see app.core.errors's AppError
      // handler); only the 200 body is now raw audio bytes.
      const body = await response.json();
      const quota = quotaMessage(response, body);
      print(ttsResult, quota || body, true);
      return;
    }
    // POST /v1/tts/synthesize now returns the audio bytes directly (see
    // routes/tts.py), not a JSON body with an /artifacts/ URL. Metadata
    // travels in X-TTS-* response headers instead.
    const blob = await response.blob();
    if (ttsAudio.dataset.objectUrl) URL.revokeObjectURL(ttsAudio.dataset.objectUrl);
    const objectUrl = URL.createObjectURL(blob);
    ttsAudio.dataset.objectUrl = objectUrl;
    ttsAudio.src = objectUrl;
    ttsAudio.classList.remove("hidden");
    const duration = response.headers.get("X-TTS-Duration-Seconds");
    const sampleRate = response.headers.get("X-TTS-Sample-Rate");
    const processSeconds = response.headers.get("X-TTS-Process-Seconds");
    const proc = processSeconds != null ? ` · synthesized in ${processSeconds}s` : "";
    print(ttsResult, `OK: ${blob.size} bytes`);
    ttsMeta.textContent = `${duration ?? "?"}s @ ${sampleRate}Hz${proc}`;
  } catch (error) {
    print(ttsResult, { error: String(error) }, true);
  }
});

