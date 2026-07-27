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
    const body = await response.json();
    const quota = quotaMessage(response, body);
    if (quota) {
      print(ttsResult, quota, true);
    } else {
      print(ttsResult, body, !response.ok);
      if (response.ok && body.data?.audio_url) {
        ttsAudio.src = body.data.audio_url;
        ttsAudio.classList.remove("hidden");
        const proc = body.data.process_seconds != null ? ` · synthesized in ${body.data.process_seconds}s` : "";
        ttsMeta.textContent = `${body.data.duration_seconds ?? "?"}s @ ${body.data.sample_rate}Hz${proc}`;
      }
    }
  } catch (error) {
    print(ttsResult, { error: String(error) }, true);
  }
});

