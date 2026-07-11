import { el, restoreAndBind } from "./helpers.js";
import { modelRow } from "./model-manager.js";

export let ttsEngineDetails = {};

export async function loadTtsEngines() {
  try {
    const body = await (await fetch("/v1/tts/engines")).json();
    const items = body.data.filter((e) => e.available);
    ttsEngineDetails = {};
    body.data.forEach((e) => (ttsEngineDetails[e.engine] = e.detail));
    const def = (body.data.find((e) => e.default) || items[0] || {}).engine;

    renderTtsEnginesStatus(body.data);

    [["tts-engine", "tts-engine-detail"], ["tts-stream-engine", "tts-stream-engine-detail"], ["t2v-tts-engine", "t2v-engine-detail"], ["tp-engine", null]].forEach(
      ([selId, detId]) => {
        const select = el(selId);
        if (!select) return;
        select.innerHTML = "";
        // Show ALL engines; disable the ones that aren't installed yet.
        body.data.forEach((item) => {
          const opt = document.createElement("option");
          opt.value = item.engine;
          opt.textContent = item.available
            ? `${item.engine} — ${item.detail}`
            : `${item.engine} — (not installed)`;
          opt.disabled = !item.available;
          select.appendChild(opt);
        });
        if (def) select.value = def;
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
    .map((e) =>
      modelRow({
        title: e.engine,
        code: e.detail,
        badges: e.available ? `<span class="badge mock">ready</span>` : `<span class="badge">not installed</span>`,
        err: !e.available && e.install_hint ? `<span class="model-err">${e.install_hint}</span>` : "",
      })
    )
    .join("");
}

export function updateTtsEngine(selId, detId) {
  const engine = el(selId).value;
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
    const voices = body.data || [];
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

