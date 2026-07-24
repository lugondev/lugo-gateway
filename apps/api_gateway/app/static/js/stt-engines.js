import { el, print, restoreAndBind } from "./helpers.js";
import { toggleStreamingAvailability } from "./stt-stream.js";

export let engineDetails = {};

export function updateEngineDetail(selectId, detailId) {
  const det = el(detailId);
  if (!det) return;
  const engine = el(selectId).value;
  const detail = engineDetails[engine];
  det.textContent = detail ? `model: ${detail}` : "";
}

export async function loadSttEngines() {
  const pairs = [
    ["stt-engine", "stt-engine-detail"],
    ["stt-stream-engine", "stt-stream-engine-detail"],
  ];
  try {
    const body = await (await fetch("/v1/stt/engines")).json();
    if (!body.success) throw new Error("Cannot load engines");

    // Only valid + available engines (vosk needs a model, remote needs config).
    const available = body.data.filter((e) => e.available);
    engineDetails = {};
    body.data.forEach((e) => (engineDetails[e.engine] = e.detail));

    pairs.forEach(([selId, detId]) => {
      const select = el(selId);
      if (!select) return;
      // Streaming only lists engines with native realtime (live partials).
      const items = selId === "stt-stream-engine" ? available.filter((e) => e.realtime) : available;
      const prev = select.value;
      select.innerHTML = "";
      items.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.engine;
        option.textContent = item.detail
          ? `${item.engine} (${item.mode}) — ${item.detail}`
          : `${item.engine} (${item.mode})`;
        select.appendChild(option);
      });
      if (items.some((e) => e.engine === prev)) select.value = prev;
      restoreAndBind(selId);
      updateEngineDetail(selId, detId);
      if (!select.dataset.bound) {
        select.addEventListener("change", () => updateEngineDetail(selId, detId));
        select.dataset.bound = "1";
      }
    });

    // Disable streaming when no realtime engine is available.
    const realtimeCount = available.filter((e) => e.realtime).length;
    toggleStreamingAvailability(realtimeCount > 0);

    if (!available.length) {
      print(el("stt-result"), "No STT engine available. Install a Vosk model or configure a remote engine.", true);
    }
  } catch (error) {
    print(el("stt-result"), { error: String(error) }, true);
  }
}

