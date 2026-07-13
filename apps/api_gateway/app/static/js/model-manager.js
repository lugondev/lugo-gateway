import { el, fmtBytes, print, setBadge } from "./helpers.js";
import { loadSttEngines } from "./stt-engines.js";
import { loadTtsEngines } from "./tts-engines.js";
import { loadSystemStatus } from "./system-status.js";
import { renderLlmRow } from "./model-recommender.js";
import { confirmDialog } from "./modal.js";

export let modelPollTimer = null;

export async function loadModels() {
  const voskHost = el("model-suggestions");
  try {
    const body = await (await fetch("/v1/models")).json();
    const v = body.data.vosk;
    const w = body.data.whisper;
    const jobs = v.jobs || {};
    const installedNames = new Set(v.installed.map((m) => m.name));

    // ---- Vosk ----
    const rows = [];
    v.suggestions.forEach((s) => rows.push(renderModelRow(s.name, s.label, s.size || "", installedNames, jobs, v.installed, v.active)));
    v.installed
      .filter((m) => !v.suggestions.some((s) => s.name === m.name))
      .forEach((m) => rows.push(renderModelRow(m.name, "custom", fmtBytes(m.size_bytes), installedNames, jobs, v.installed, v.active)));
    voskHost.innerHTML = rows.join("");

    // ---- Whisper ----
    el("whisper-models").innerHTML = w.models.map(renderWhisperRow).join("");

    // Per-engine install truth + whether the runtime Install button is enabled.
    const ttsEngines = body.data.tts_engines || {};
    const installEnabled = body.data.install_enabled;
    const omniAvail = !!(ttsEngines.omnivoice && ttsEngines.omnivoice.available);
    const vieneuAvail = !!(ttsEngines.vieneu && ttsEngines.vieneu.available);

    // ---- OmniVoice (TTS) ----
    const omni = body.data.omnivoice;
    el("omnivoice-models").innerHTML =
      engineBanner("omnivoice", ttsEngines, installEnabled) +
      omni.models.map((m) => renderOmniRow(m, omniAvail)).join("");

    // ---- VieNeu (TTS) ----
    const vieneu = body.data.vieneu;
    el("vieneu-models").innerHTML =
      engineBanner("vieneu", ttsEngines, installEnabled) +
      vieneu.modes.map((m) => renderVieneuRow(m, vieneuAvail)).join("");

    // ---- Conversation LLM (Ollama) ----
    const llm = body.data.llm;
    if (el("llm-hint")) {
      if (llm.remote && llm.available) {
        el("llm-hint").textContent = `Cloud API: ${llm.base_url} — model: ${llm.active} ✓`;
      } else if (llm.remote) {
        el("llm-hint").textContent = `Cloud API: ${llm.base_url} — set CONVERSATION_LLM_API_KEY to use.`;
      } else if (llm.available) {
        el("llm-hint").textContent = `Ollama at ${llm.base_url} — active: ${llm.active} ${llm.running ? "(running)" : "(idle)"}`;
      } else {
        el("llm-hint").textContent = "Ollama not reachable. Install & run Ollama, then set CONVERSATION_LLM_BASE_URL=http://localhost:11434/v1.";
      }
    }
    const llmBtn = el("llm-start");
    if (llmBtn) {
      llmBtn.hidden = !!llm.remote;
      if (!llm.remote) {
        llmBtn.dataset.mode = llm.available ? "stop" : "start";
        llmBtn.textContent = llm.available ? "Stop service" : "Start service";
      }
    }
    setBadge("badge-llm", llm.available); setBadge("foot-llm", llm.available);
    const llmNames = new Set(llm.suggestions.map((s) => s.model));
    const llmRows = llm.suggestions.map((s) => renderLlmRow(s, llm.jobs, llm.available));
    llm.installed
      .filter((m) => !llmNames.has(m.model))
      .forEach((m) => llmRows.push(renderLlmRow({ ...m, installed: true }, llm.jobs, llm.available)));
    el("llm-models").innerHTML = llmRows.join("");

    bindModelButtons();

    const busy =
      Object.values(jobs).some((j) => j.state === "downloading") ||
      w.models.some((m) => m.job && m.job.state === "downloading") ||
      omni.models.some((m) => m.job && m.job.state === "downloading") ||
      vieneu.modes.some((m) => m.job && m.job.state === "downloading") ||
      Object.values(llm.jobs || {}).some((j) => j.state === "downloading");
    if (busy) {
      clearTimeout(modelPollTimer);
      modelPollTimer = setTimeout(loadModels, 1000);
    } else if (modelPollTimer) {
      clearTimeout(modelPollTimer);
      modelPollTimer = null;
      loadSystemStatus();
      loadTtsEngines();
    }
  } catch (error) {
    voskHost.innerHTML = `<p class="meta error">Cannot load models: ${String(error)}</p>`;
  }
}

// Shared row builder + small action fragments for every model list.
export function modelRow({ title, code, badges = "", size = "", err = "", action = "" }) {
  return `<div class="model-row"><div class="model-info"><strong>${title}</strong><code>${code}</code>${badges}<span class="model-size">${size}</span>${err}</div><div class="model-action">${action}</div></div>`;
}
export const ACTIVE_BADGE = `<span class="badge mock">active</span>`;
export const SPINNER_ACTION = `<div class="progress indeterminate"><div class="bar"></div></div><span class="pct">…</span>`;
export const jobErr = (job) => (job && job.state === "error" ? `<span class="model-err">${job.error}</span>` : "");
export const isErr = (job) => job && job.state === "error";
export const useOrActive = (active, attr, key) =>
  active ? ACTIVE_BADGE : `<button class="mini" data-${attr}="${key}">Use</button>`;
export const dlBtn = (attr, key, job) =>
  `<button class="mini" data-${attr}="${key}">${isErr(job) ? "Retry" : "Download"}</button>`;
// Shown when a model/mode is the *selected* one but its engine package isn't installed
// (so it is not actually running).
export const SELECTED_OFF = `<span class="badge mock">selected · engine not installed</span>`;
// Managed TTS engines that are installable via the pip allowlist (/v1/models/install).
export const TTS_PIP = { vieneu: "vieneu" };

// Per-section banner showing whether the engine PACKAGE is installed (the real
// "can it run" truth), with a one-click Install when available + enabled.
export function engineBanner(engine, ttsEngines, installEnabled) {
  const e = ttsEngines && ttsEngines[engine];
  if (!e) return "";
  if (e.available) return `<p class="meta"><span class="badge ok">engine installed</span></p>`;
  const hint = e.install_hint || "";
  const pkg = TTS_PIP[engine];
  const btn = pkg && installEnabled ? ` <button class="mini" data-pip-install="${pkg}">Install</button>` : "";
  return `<p class="meta"><span class="badge danger">engine not installed</span> ${hint}${btn}</p>`;
}

export function renderOmniRow(m, available) {
  // OmniVoice weights download via HF (no package import needed), so Download stays
  // usable; only the "active" label must reflect whether the engine is installed.
  let action;
  if (m.job && m.job.state === "downloading") action = SPINNER_ACTION;
  else if (m.cached) {
    const use = m.active
      ? (available ? ACTIVE_BADGE : SELECTED_OFF)
      : `<button class="mini" data-o-select="${m.id}">Use</button>`;
    action = `${use}<button class="mini danger" data-o-delete="${m.id}">Delete</button>`;
  } else action = dlBtn("o-download", m.id, m.job);
  return modelRow({ title: m.label, code: m.id, size: m.cached ? fmtBytes(m.size_bytes) : "", err: jobErr(m.job), action });
}

export function renderVieneuRow(m, available) {
  // VieNeu Download warms the model, which imports the `vieneu` package — so without
  // the package it would crash ("No module named 'vieneu'"). Gate it; the section
  // banner offers Install.
  let action;
  if (m.job && m.job.state === "downloading") action = SPINNER_ACTION;
  else if (!available) action = m.active ? SELECTED_OFF : `<span class="meta">install vieneu first</span>`;
  else if (m.active) action = ACTIVE_BADGE;
  else if (m.cpu || m.cached) {
    const del = m.cached ? `<button class="mini danger" data-vn-delete="${m.mode}">Delete</button>` : "";
    const dl = m.cached ? "" : `<button class="mini" data-vn-download="${m.mode}">Download</button>`;
    action = `<button class="mini" data-vn-select="${m.mode}">Use</button>${dl}${del}`;
  } else action = `<button class="mini" data-vn-download="${m.mode}">Download</button>`;
  const badges = m.cpu ? `<span class="badge">cpu</span>` : `<span class="badge mock">gpu</span>`;
  return modelRow({ title: m.label, code: m.mode, badges, size: m.cached ? fmtBytes(m.size_bytes) : "", err: jobErr(m.job), action });
}

export function renderWhisperRow(m) {
  let action;
  if (m.job && m.job.state === "downloading") action = SPINNER_ACTION;
  else if (m.cached) action = `${useOrActive(m.active, "w-select", m.size)}<button class="mini danger" data-w-delete="${m.size}">Delete</button>`;
  else action = dlBtn("w-download", m.size, m.job);
  return modelRow({ title: m.label, code: m.size, size: m.cached ? fmtBytes(m.size_bytes) : "", err: jobErr(m.job), action });
}

export function renderModelRow(name, label, size, installedNames, jobs, installed, activeName) {
  const job = jobs[name];
  const installedEntry = installed.find((m) => m.name === name);
  let action;
  if (job && job.state === "downloading") {
    const pct = Math.round((job.progress || 0) * 100);
    action = `<div class="progress"><div class="bar" style="width:${pct}%"></div></div><span class="pct">${pct}%</span>`;
  } else if (installedNames.has(name)) {
    action = `${useOrActive(name === activeName, "v-select", name)}<button class="mini danger" data-delete="${name}">Delete</button>`;
  } else action = dlBtn("download", name, job);
  return modelRow({
    title: label,
    code: name,
    size: installedEntry ? fmtBytes(installedEntry.size_bytes) : size,
    err: jobErr(job),
    action,
  });
}

// engine -> request shape. STT engines refresh the STT selector; TTS the TTS one.
// vosk/whisper expose DELETE by path param; omnivoice/vieneu use POST /delete.
export const MODEL_ENGINES = {
  vosk: { keyName: "name", deleteVerb: "DELETE", refresh: () => loadSttEngines() },
  whisper: { keyName: "size", deleteVerb: "DELETE", refresh: () => loadSttEngines() },
  omnivoice: { keyName: "id", deleteVerb: "POST", refresh: () => loadTtsEngines() },
  vieneu: { keyName: "mode", deleteVerb: "POST", refresh: () => loadTtsEngines() },
  llm: { keyName: "model", deleteVerb: "POST", refresh: () => {} },
};

export const MODEL_ATTRS = [
  ["data-download", "vosk", "download"], ["data-delete", "vosk", "delete"], ["data-v-select", "vosk", "select"],
  ["data-w-download", "whisper", "download"], ["data-w-delete", "whisper", "delete"], ["data-w-select", "whisper", "select"],
  ["data-o-download", "omnivoice", "download"], ["data-o-delete", "omnivoice", "delete"], ["data-o-select", "omnivoice", "select"],
  ["data-vn-download", "vieneu", "download"], ["data-vn-delete", "vieneu", "delete"], ["data-vn-select", "vieneu", "select"],
  ["data-llm-download", "llm", "download"], ["data-llm-delete", "llm", "delete"], ["data-llm-select", "llm", "select"],
];

export function bindModelButtons() {
  MODEL_ATTRS.forEach(([attr, engine, action]) => {
    document.querySelectorAll(`[${attr}]`).forEach((btn) => {
      btn.addEventListener("click", () => runModelAction(engine, action, btn.getAttribute(attr)));
    });
  });
}

export async function runModelAction(engine, action, key) {
  const cfg = MODEL_ENGINES[engine];
  if (action === "delete" && !(await confirmDialog(`Delete ${engine} "${key}"?`, { danger: true }))) return;
  print(el("model-msg"), `${engine} ${key}: ${action}...`);
  try {
    let resp;
    if (action === "delete" && cfg.deleteVerb === "DELETE") {
      resp = await fetch(`/v1/models/${engine}/${encodeURIComponent(key)}`, { method: "DELETE" });
    } else {
      resp = await fetch(`/v1/models/${engine}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [cfg.keyName]: key }),
      });
    }
    const body = await resp.json();
    if (!resp.ok) {
      print(el("model-msg"), body.error || body, true);
      return;
    }
    el("model-msg").textContent = `${engine} ${key}: ${action} ok`;
    loadModels();
    if (action !== "download") {
      loadSystemStatus();
      cfg.refresh();
    }
  } catch (error) {
    print(el("model-msg"), String(error), true);
  }
}

export function bindDownloadByName(btnId, inputId, engine) {
  el(btnId).addEventListener("click", () => {
    const key = el(inputId).value.trim();
    if (!key) {
      print(el("model-msg"), "Enter a model name / repo id", true);
      return;
    }
    runModelAction(engine, "download", key);
  });
}
bindDownloadByName("omni-download", "omni-name", "omnivoice");
bindDownloadByName("model-download", "model-name", "vosk");
bindDownloadByName("llm-download", "llm-name", "llm");

