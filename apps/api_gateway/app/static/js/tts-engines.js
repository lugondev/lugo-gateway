import { el, restoreAndBind } from "./helpers.js";
import { modelRow } from "./model-manager.js";

export let ttsEngineDetails = {};

// The playground TTS pickers select a specific Model Registry row, not just an
// engine: one engine (e.g. http_tts) can back several enabled rows pointing
// at different service base URLs, so an engine-only pick is ambiguous and picks
// a non-deterministic row server-side. Each option value is "engine|model_id",
// mirroring the STT/LLM model pickers. `/v1/tts/engines` is still fetched for
// the install/status panel, per-engine detail text, and voice lists.
export function ttsEngineOf(selId) {
  const [engine = ""] = (el(selId)?.value || "").split("|");
  return engine;
}

export async function loadTtsEngines() {
  try {
    const [enginesBody, optionsBody] = await Promise.all([
      (await fetch("/v1/tts/engines")).json(),
      (await fetch("/v1/model_registry/options?kind=tts")).json(),
    ]);
    const options = optionsBody.data || [];
    ttsEngineDetails = {};
    enginesBody.data.forEach((e) => (ttsEngineDetails[e.engine] = e.detail));
    const availableEngines = new Set(enginesBody.data.filter((e) => e.available).map((e) => e.engine));

    renderTtsEnginesStatus(enginesBody.data);

    [["tts-engine", "tts-engine-detail"], ["tts-stream-engine", "tts-stream-engine-detail"], ["t2v-tts-engine", "t2v-engine-detail"]].forEach(
      ([selId, detId]) => {
        const select = el(selId);
        if (!select) return;
        const prev = select.value;
        select.innerHTML = "";
        // One option per selectable registry row; disable rows whose engine
        // isn't installed/available yet.
        options.forEach((o) => {
          const opt = document.createElement("option");
          opt.value = `${o.engine}|${o.model_id}`;
          const ok = availableEngines.has(o.engine);
          opt.textContent = ok ? o.label : `${o.label} — (not installed)`;
          opt.disabled = !ok;
          select.appendChild(opt);
        });
        if ([...select.options].some((o) => o.value === prev)) {
          select.value = prev;
        } else {
          const firstOk = options.find((o) => availableEngines.has(o.engine));
          if (firstOk) select.value = `${firstOk.engine}|${firstOk.model_id}`;
        }
        restoreAndBind(selId);
        updateTtsEngine(selId, detId);
        if (!select.dataset.bound) {
          select.addEventListener("change", () => updateTtsEngine(selId, detId));
          select.dataset.bound = "1";
        }
      }
    );
  } catch (error) {
    /* engines optional */
  }
}

export function renderTtsEnginesStatus(engines) {
  const host = el("tts-engines-status");
  if (!host) return;
  host.innerHTML = engines
    .map((e) => {
      const canInstall = !e.available && e.install_package && e.install_enabled;
      const btn = canInstall ? ` <button class="mini" data-pip-install="${e.install_package}">Install</button>` : "";
      return modelRow({
        title: e.engine,
        code: e.detail,
        badges: e.available ? `<span class="badge mock">ready</span>` : `<span class="badge">not installed</span>`,
        err: !e.available && e.install_hint ? `<span class="model-err">${e.install_hint}${btn}</span>` : "",
      });
    })
    .join("");
}

export function updateTtsEngine(selId, detId) {
  const engine = ttsEngineOf(selId);
  const det = el(detId);
  if (det) det.textContent = ttsEngineDetails[engine] ? `model: ${ttsEngineDetails[engine]}` : "";
  // Voice selector applies to any engine that exposes a voice list (vieneu, edge_tts, kokoro_vi, ...).
  if (selId === "tts-engine") loadTtsVoices(engine);
  if (selId === "t2v-tts-engine") loadTtsVoices(engine, { wrapId: "t2v-voice-wrap", selectId: "t2v-tts-voice", restore: false });
}

export async function loadTtsVoices(engine, { wrapId = "tts-voice-wrap", selectId = "tts-voice", restore = true } = {}) {
  const wrap = el(wrapId);
  const sel = el(selectId);
  if (!sel) return;
  try {
    const body = await (await fetch(`/v1/tts/voices?engine=${encodeURIComponent(engine)}`)).json();
    const voices = body.data?.voices || [];
    if (wrap) wrap.classList.toggle("hidden", voices.length === 0);
    sel.innerHTML = '<option value="">(auto)</option>';
    voices.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.voice;
      opt.textContent = v.label;
      sel.appendChild(opt);
    });
    sel.dataset.loaded = engine;
    if (restore) restoreAndBind(selectId);
  } catch (error) {
    /* voices optional */
  }
}

