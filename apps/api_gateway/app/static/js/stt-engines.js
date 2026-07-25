import { el, print, restoreAndBind } from "./helpers.js";
import { toggleStreamingAvailability } from "./stt-stream.js";

export let engineDetails = {};

// The playground STT pickers select a specific Model Registry row, not just an
// engine: one engine (e.g. http_stt) can back several enabled rows pointing
// at different service base URLs or model ids, so an engine-only pick is
// ambiguous and picks a non-deterministic row server-side. Each option value
// is "engine|model_id", mirroring the TTS/LLM model pickers. `/v1/stt/engines`
// is still fetched for availability/realtime status and per-engine detail text.
export function sttEngineOf(selId) {
  const [engine = ""] = (el(selId)?.value || "").split("|");
  return engine;
}

export function updateEngineDetail(selectId, detailId) {
  const det = el(detailId);
  if (!det) return;
  const engine = sttEngineOf(selectId);
  const detail = engineDetails[engine];
  det.textContent = detail ? `model: ${detail}` : "";
}

export async function loadSttEngines() {
  try {
    const [enginesBody, optionsBody] = await Promise.all([
      (await fetch("/v1/stt/engines")).json(),
      (await fetch("/v1/model_registry/options?kind=stt")).json(),
    ]);
    if (!enginesBody.success) throw new Error("Cannot load engines");
    const options = optionsBody.data || [];

    engineDetails = {};
    enginesBody.data.forEach((e) => (engineDetails[e.engine] = e.detail));
    // Only valid + available engines (vosk needs a model, remote needs config).
    const availableEngines = new Set(enginesBody.data.filter((e) => e.available).map((e) => e.engine));
    // Streaming only lists rows whose engine has native realtime (live partials).
    const realtimeEngines = new Set(enginesBody.data.filter((e) => e.realtime).map((e) => e.engine));

    [["stt-engine", "stt-engine-detail", false], ["stt-stream-engine", "stt-stream-engine-detail", true]].forEach(
      ([selId, detId, streamOnly]) => {
        const select = el(selId);
        if (!select) return;
        const items = streamOnly ? options.filter((o) => realtimeEngines.has(o.engine)) : options;
        const prev = select.value;
        select.innerHTML = "";
        // One option per selectable registry row; disable rows whose engine
        // isn't installed/available yet.
        items.forEach((o) => {
          const opt = document.createElement("option");
          opt.value = `${o.engine}|${o.model_id}`;
          const ok = availableEngines.has(o.engine);
          const label = `${o.engine} — ${o.model_id}`;
          opt.textContent = ok ? label : `${label} (not installed)`;
          opt.disabled = !ok;
          select.appendChild(opt);
        });
        if ([...select.options].some((o) => o.value === prev)) {
          select.value = prev;
        } else {
          const firstOk = items.find((o) => availableEngines.has(o.engine));
          if (firstOk) select.value = `${firstOk.engine}|${firstOk.model_id}`;
        }
        restoreAndBind(selId);
        updateEngineDetail(selId, detId);
        if (!select.dataset.bound) {
          select.addEventListener("change", () => updateEngineDetail(selId, detId));
          select.dataset.bound = "1";
        }
      }
    );

    // Disable streaming when no realtime engine is available.
    const realtimeCount = options.filter((o) => realtimeEngines.has(o.engine) && availableEngines.has(o.engine)).length;
    toggleStreamingAvailability(realtimeCount > 0);

    if (!availableEngines.size) {
      print(el("stt-result"), "No STT engine available. Install a Vosk model or configure a remote engine.", true);
    }
  } catch (error) {
    print(el("stt-result"), { error: String(error) }, true);
  }
}

