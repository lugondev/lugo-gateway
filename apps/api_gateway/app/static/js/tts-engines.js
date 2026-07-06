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
  // Voice selector only applies to the batch VieNeu engine.
  if (selId === "tts-engine") {
    const isVieneu = engine === "vieneu";
    el("tts-voice-wrap").classList.toggle("hidden", !isVieneu);
    if (isVieneu && !el("tts-voice").dataset.loaded) loadTtsVoices();
  }
  if (selId === "t2v-tts-engine") {
    const isVieneu = engine === "vieneu";
    const wrap = el("t2v-voice-wrap");
    if (wrap) wrap.classList.toggle("hidden", !isVieneu);
    if (isVieneu) {
      fetch("/v1/tts/voices?engine=vieneu").then(r => r.json()).then(b => {
        const sel = el("t2v-tts-voice");
        if (!sel) return;
        sel.innerHTML = '<option value="">(auto)</option>';
        b.data.forEach(v => { const o = document.createElement("option"); o.value = v.voice; o.textContent = v.label; sel.appendChild(o); });
      }).catch(() => {});
    }
  }
}

export async function loadTtsVoices() {
  try {
    const body = await (await fetch("/v1/tts/voices?engine=vieneu")).json();
    const sel = el("tts-voice");
    sel.innerHTML = '<option value="">(auto)</option>';
    body.data.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.voice;
      opt.textContent = v.label;
      sel.appendChild(opt);
    });
    sel.dataset.loaded = "1";
    restoreAndBind("tts-voice");
  } catch (error) {
    /* voices optional */
  }
}

