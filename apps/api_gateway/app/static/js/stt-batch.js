import { el, print } from "./helpers.js";
import { createMicCapture } from "./audio-capture.js";

export let sttBatchRecorder = null;
export let sttRecordedBlob = null;
export let sttMode = "upload";

export function initSttMode() {
  document.querySelectorAll("#stt-mode .seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      sttMode = btn.getAttribute("data-mode");
      document.querySelectorAll("#stt-mode .seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
      el("stt-upload-pane").classList.toggle("hidden", sttMode !== "upload");
      el("stt-record-pane").classList.toggle("hidden", sttMode !== "record");
    });
  });
}

el("stt-record").addEventListener("click", async () => {
  const btn = el("stt-record");
  const status = el("stt-record-status");
  if (sttBatchRecorder) {
    // stop
    sttBatchRecorder.stop();
    const seconds = sttBatchRecorder.durationSeconds().toFixed(1);
    sttRecordedBlob = sttBatchRecorder.toWavBlob();
    sttBatchRecorder = null;
    btn.textContent = "● Record";
    btn.classList.remove("recording");
    status.textContent = `recorded ${seconds}s (WAV 16kHz)`;
    status.className = "status-idle";
    const preview = el("stt-record-preview");
    preview.src = URL.createObjectURL(sttRecordedBlob);
    preview.classList.remove("hidden");
    return;
  }
  try {
    sttBatchRecorder = createMicCapture();
    await sttBatchRecorder.start();
    btn.textContent = "■ Stop";
    btn.classList.add("recording");
    status.textContent = "● recording...";
    status.className = "status-rec";
  } catch (error) {
    sttBatchRecorder = null;
    status.textContent = "mic denied";
    status.className = "status-error";
  }
});

el("stt-submit").addEventListener("click", async () => {
  const sttResult = el("stt-result");
  const file = el("stt-audio").files && el("stt-audio").files[0];

  const form = new FormData();
  if (sttMode === "upload") {
    if (!file) {
      print(sttResult, "Select an audio file (or switch to Record mic)", true);
      return;
    }
    form.append("audio", file);
  } else {
    if (!sttRecordedBlob) {
      print(sttResult, "Record audio first", true);
      return;
    }
    form.append("audio", sttRecordedBlob, "recording.wav");
  }
  const [engine = "vosk", model = ""] = (el("stt-engine").value || "").split("|");
  form.append("engine", engine);
  if (model) form.append("model", model);
  if (el("stt-language").value.trim()) form.append("language", el("stt-language").value.trim());

  print(sttResult, `Transcribing ${file ? "file" : "recording"}...`);
  try {
    const response = await fetch("/v1/stt/transcribe", { method: "POST", body: form });
    const body = await response.json();
    print(sttResult, body, !response.ok);
  } catch (error) {
    print(sttResult, { error: String(error) }, true);
  }
});

