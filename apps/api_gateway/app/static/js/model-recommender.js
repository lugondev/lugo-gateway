import { el, print, fmtBytes } from "./helpers.js";
import { loadModels, useOrActive, dlBtn, modelRow, jobErr } from "./model-manager.js";
import { loadSystemStatus } from "./system-status.js";
import { loadSttEngines } from "./stt-engines.js";
import { loadTtsEngines } from "./tts-engines.js";
import { loadConversationEngines } from "./conversation.js";

export let recommendData = null;
export let recommendActions = [];
export const REC_CHIP_GROUPS = [
  ["apple_silicon", "Apple Silicon (MLX)"],
  ["cpu", "CPU"],
  ["nvidia_gpu", "NVIDIA GPU"],
];
export const REC_CAT_LABELS = { stt: "STT", tts: "TTS", llm: "LLM", vad: "VAD" };

export function recCapChips(c) {
  const chips = [
    c.os,
    c.arch,
    c.cpu_count ? `${c.cpu_count} cores` : null,
    c.ram_total_gb ? `${c.ram_total_gb} GB RAM` : null,
    c.disk_free_gb ? `${c.disk_free_gb} GB free` : null,
    c.mlx ? "mlx" : "no-mlx",
    c.cuda ? "cuda" : "no-cuda",
    c.libopus ? "libopus" : "no-libopus",
    c.ollama ? "ollama" : "no-ollama",
  ].filter(Boolean);
  return chips.map((x) => `<span class="badge">${x}</span>`).join("");
}

export function recStatusBadge(it) {
  if (it.status.startsWith("incompatible")) return `<span class="badge danger">${it.status}</span>`;
  if (it.status.startsWith("needs")) return `<span class="badge mock">${it.status}</span>`;
  if (it.status === "installed") return `<span class="badge ok">installed</span>`;
  return `<span class="badge ok">runnable</span>`;
}

export function recRow(it) {
  let action;
  if (it.active) {
    action = `<span class="badge ok">active</span>`;
  } else if (it.status === "installed" && it.select) {
    recommendActions.push({ type: "select", ...it.select });
    action = `<button class="mini" data-rec-idx="${recommendActions.length - 1}">Use</button>`;
  } else if (it.status === "installed") {
    action = `<span class="badge ok">installed</span>`;
  } else if (it.status === "runnable" && it.action.kind === "download") {
    recommendActions.push({ type: "download", ...it.action });
    action = `<button class="mini" data-rec-idx="${recommendActions.length - 1}">Download</button>`;
  } else if (it.status === "runnable") {
    // Runnable but not a download (configured remote/online, built-in).
    action = `<span class="badge ok">ready</span>`;
  } else if (it.installable) {
    // needs:<pip pkg> and runtime install is enabled — one-click pip install.
    recommendActions.push({ type: "install", method: "POST", path: "/v1/models/install", payload: { package: it.install_package } });
    action = `<button class="mini" data-rec-idx="${recommendActions.length - 1}">Install</button>`;
  } else {
    // needs:<x> (engine/runtime missing) or incompatible:<x> — guidance only.
    action = `<span class="meta">${it.action.hint || it.reason || ""}</span>`;
  }
  const badges = `${recStatusBadge(it)}<span class="badge">fit ${it.fit_score}</span>`;
  const titleHint = it.reason ? ` title="${it.reason.replace(/"/g, "&quot;")}"` : "";
  const row = `<div class="model-row${it.status.startsWith("incompatible") ? " dim" : ""}"${titleHint}>`
    + `<div class="model-info"><strong>${it.label}</strong><code>${it.id}</code>${badges}`
    + `<span class="model-size">${it.size_estimate}</span></div>`
    + `<div class="model-action">${action}</div></div>`;
  return row;
}

export function renderRecommend() {
  if (!recommendData) return;
  recommendActions = [];
  el("recommend-caps").innerHTML = recCapChips(recommendData.capabilities);
  const only = el("recommend-only").checked;
  const parts = [];
  for (const cat of ["stt", "tts", "llm", "vad"]) {
    const items = recommendData.categories[cat] || [];
    parts.push(`<h3 class="sub">${REC_CAT_LABELS[cat]}</h3>`);
    if (only) {
      const rec = items.filter((i) => i.recommended);
      parts.push(
        `<div class="model-list">${rec.length ? rec.map(recRow).join("") : '<p class="meta">No models recommended for this host.</p>'}</div>`,
      );
    } else {
      for (const [chip, label] of REC_CHIP_GROUPS) {
        const group = items.filter((i) => i.chip === chip);
        if (!group.length) continue;
        parts.push(`<div class="rec-group-label meta">${label}</div>`);
        parts.push(`<div class="model-list">${group.map(recRow).join("")}</div>`);
      }
    }
  }
  el("recommend-list").innerHTML = parts.join("");
}

export async function loadRecommend() {
  try {
    const body = await (await fetch("/v1/models/recommend")).json();
    recommendData = body.data;
    renderRecommend();
  } catch (error) {
    el("recommend-list").innerHTML = `<p class="meta error">${error}</p>`;
  }
}

export async function recAct(action) {
  const verb = { select: "use", install: "install" }[action.type] || "download";
  print(el("model-msg"), `${verb} ${JSON.stringify(action.payload)}...`);
  try {
    const resp = await fetch(action.path, {
      method: action.method || "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action.payload),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(el("model-msg"), body.error || body, true);
      return;
    }
    const msg = {
      select: "active model updated",
      install: "installing package… refreshing shortly (a restart may be needed)",
    }[action.type] || "download started — see the lists below for progress";
    el("model-msg").textContent = msg;
    loadModels();
    if (action.type === "select") {
      loadSystemStatus();
      if (typeof loadSttEngines === "function") loadSttEngines();
      if (typeof loadTtsEngines === "function") loadTtsEngines();
      if (typeof loadConversationEngines === "function") loadConversationEngines();
    }
    if (action.type === "install") setTimeout(loadTtsEngines, 8000);
    setTimeout(loadRecommend, { select: 300, install: 8000 }[action.type] || 1500);
  } catch (error) {
    print(el("model-msg"), String(error), true);
  }
}

el("recommend-list").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-rec-idx]");
  if (btn) recAct(recommendActions[Number(btn.getAttribute("data-rec-idx"))]);
});

// Install buttons in the model-manager section banners (re-rendered by loadModels),
// delegated at document level so they survive re-renders.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-pip-install]");
  if (!btn) return;
  recAct({ type: "install", method: "POST", path: "/v1/models/install", payload: { package: btn.getAttribute("data-pip-install") } });
});
el("recommend-only").addEventListener("change", renderRecommend);

el("llm-start").addEventListener("click", async () => {
  const hint = el("llm-hint");
  const stopping = el("llm-start").dataset.mode === "stop";
  hint.textContent = stopping ? "Stopping Ollama service…" : "Starting Ollama service…";
  hint.classList.remove("error");
  try {
    const resp = await fetch(`/v1/models/llm/${stopping ? "stop" : "start"}`, { method: "POST" });
    const body = await resp.json();
    if (!resp.ok) {
      print(hint, body.error || body, true);
      return;
    }
    const d = body.data;
    hint.textContent = stopping
      ? "Ollama service stopped — conversation/chat offline until you Start"
      : `Ollama ready — active: ${d.active}${d.started ? " (started)" : ""}${d.warmed ? " · warmed" : ""}`;
    loadModels();
  } catch (error) {
    print(hint, String(error), true);
  }
});

// A local Ollama endpoint is managed in the Ollama list above, not in this online
// form — so we treat localhost endpoints as "local" and keep the form for hosted only.
export function isLocalLlm(url) {
  return /\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0)/.test(url || "");
}

export function renderLlmOnlineStatus(d) {
  const s = el("llm-online-status");
  if (!s) return;
  s.classList.remove("error");
  if (d.responder !== "llm") {
    s.textContent = "Active: echo (no LLM endpoint configured)";
  } else if (isLocalLlm(d.base_url)) {
    s.textContent = `Currently using local Ollama (${d.model}) — change it in the list above. Pick a provider here to switch online.`;
  } else {
    s.textContent = `Active (online): ${d.model || "(no model)"} @ ${d.base_url}${d.api_key_set ? " · key set" : " · no key"}`;
  }
}

// Quick-switch presets: prefill URL + a sensible default model (editable). The
// model is only a suggestion — change it to any model the provider exposes.
export const LLM_PRESETS = {
  openai: { url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  groq: { url: "https://api.groq.com/openai/v1", model: "llama-3.3-70b-versatile" },
  together: { url: "https://api.together.xyz/v1", model: "meta-llama/Llama-3.3-70B-Instruct-Turbo" },
};

if (el("llm-online-preset")) {
  el("llm-online-preset").addEventListener("change", (e) => {
    const p = LLM_PRESETS[e.target.value];
    if (!p) return;
    el("llm-online-url").value = p.url;
    el("llm-online-model").value = p.model;
    el("llm-online-key").focus();
    print(el("llm-online-status"), `${e.target.value}: enter API key then “Use this LLM”`);
  });
}

export async function loadLlmOnlineConfig() {
  try {
    const body = await (await fetch("/v1/conversation/llm")).json();
    const d = body.data || {};
    // Only prefill the form for a HOSTED endpoint. A local Ollama config must not
    // leak into the online form (it's managed in the Ollama list above).
    if (!isLocalLlm(d.base_url)) {
      if (el("llm-online-url") && !el("llm-online-url").value) el("llm-online-url").value = d.base_url || "";
      if (el("llm-online-model") && !el("llm-online-model").value) el("llm-online-model").value = d.model || "";
    }
    renderLlmOnlineStatus(d);
  } catch (error) {
    /* status stays blank if offline */
  }
}

if (el("llm-online-apply")) {
  el("llm-online-apply").addEventListener("click", async () => {
    const s = el("llm-online-status");
    const payload = {
      base_url: el("llm-online-url").value.trim(),
      model: el("llm-online-model").value.trim(),
      api_key: el("llm-online-key").value,
    };
    if (!payload.base_url) {
      print(s, "Enter a base URL (e.g. https://api.openai.com/v1)", true);
      return;
    }
    s.textContent = "Applying…";
    try {
      const resp = await fetch("/v1/conversation/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await resp.json();
      if (!resp.ok) {
        print(s, body.error || body, true);
        return;
      }
      el("llm-online-key").value = ""; // clear the key field once sent
      renderLlmOnlineStatus(body.data);
    } catch (error) {
      print(s, String(error), true);
    }
  });
}

if (el("llm-online-reset")) {
  el("llm-online-reset").addEventListener("click", async () => {
    try {
      const body = await (await fetch("/v1/conversation/llm/reset", { method: "POST" })).json();
      el("llm-online-url").value = body.data.base_url || "";
      el("llm-online-model").value = body.data.model || "";
      el("llm-online-key").value = "";
      renderLlmOnlineStatus(body.data);
    } catch (error) {
      print(el("llm-online-status"), String(error), true);
    }
  });
}

export function renderLlmRow(m, jobs, available) {
  const job = jobs[m.model];
  let action;
  if (job && job.state === "downloading") {
    const pct = Math.round((job.progress || 0) * 100);
    action = `<div class="progress"><div class="bar" style="width:${pct}%"></div></div><span class="pct">${pct}%</span>`;
  } else if (m.installed) {
    action = `${useOrActive(m.active, "llm-select", m.model)}<button class="mini danger" data-llm-delete="${m.model}">Delete</button>`;
  } else if (!available) {
    // Pulling a model needs a running Ollama; without it the pull errors. Point the
    // user at the Ollama status banner above instead of a Download that will fail.
    action = `<span class="meta">start Ollama first</span>`;
  } else {
    action = dlBtn("llm-download", m.model, job);
  }
  const size = m.size_bytes ? fmtBytes(m.size_bytes) : "";
  const badges = m.running ? `<span class="badge">running</span>` : "";
  return modelRow({ title: m.label || m.model, code: m.model, badges, size, err: jobErr(job), action });
}
el("status-refresh").addEventListener("click", () => {
  loadSystemStatus();
  loadModels();
});

