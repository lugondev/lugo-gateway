// ============================================================ helpers
function pretty(value) {
  return JSON.stringify(value, null, 2);
}
function print(el, value, isError = false) {
  el.textContent = typeof value === "string" ? value : pretty(value);
  el.classList.toggle("error", isError);
}
function el(id) {
  return document.getElementById(id);
}
function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(n) / Math.log(1024));
  return `${(n / 1024 ** i).toFixed(1)} ${u[i]}`;
}
function wsUrl(path) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}${path}`;
}

// ---- persisted UI preferences (localStorage) ----
const PREFS_KEY = "stt-ui-prefs";
function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
  } catch {
    return {};
  }
}
function savePref(id, value) {
  const p = loadPrefs();
  p[id] = value;
  localStorage.setItem(PREFS_KEY, JSON.stringify(p));
}
function controlValue(e) {
  return e.type === "checkbox" ? e.checked : e.value;
}
// Restore a control's saved value (selects: only if the option exists), then
// persist future changes. Safe to call after options are populated.
function restoreAndBind(id) {
  const e = el(id);
  if (!e) return;
  const prefs = loadPrefs();
  if (id in prefs) {
    const v = prefs[id];
    if (e.tagName === "SELECT") {
      if ([...e.options].some((o) => o.value === v)) e.value = v;
    } else if (e.type === "checkbox") {
      e.checked = !!v;
    } else {
      e.value = v;
    }
  }
  if (!e.dataset.prefBound) {
    e.dataset.prefBound = "1";
    const evt = e.tagName === "SELECT" || e.type === "checkbox" ? "change" : "input";
    e.addEventListener(evt, () => savePref(id, controlValue(e)));
  }
}

// ============================================================ audio capture
const STREAM_SAMPLE_RATE = 16000;

// Average-decimate a float32 buffer from inputRate down to targetRate -> Int16.
function downsampleToPcm16(input, inputRate, targetRate) {
  const ratio = inputRate / targetRate;
  const outLength = Math.floor(input.length / ratio);
  const pcm = new Int16Array(outLength);
  let pos = 0;
  for (let i = 0; i < outLength; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(Math.floor((i + 1) * ratio), input.length);
    let sum = 0;
    let count = 0;
    for (let j = start; j < end; j++) {
      sum += input[j];
      count++;
    }
    const sample = count ? sum / count : input[start] || 0;
    const clamped = Math.max(-1, Math.min(1, sample));
    pcm[pos++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
  }
  return pcm;
}

function writeStr(view, offset, str) {
  for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
}

// Encode a list of Int16Array chunks into a mono PCM16 WAV blob.
function encodeWav(chunks, sampleRate) {
  const length = chunks.reduce((n, c) => n + c.length, 0);
  const buffer = new ArrayBuffer(44 + length * 2);
  const view = new DataView(buffer);
  writeStr(view, 0, "RIFF");
  view.setUint32(4, 36 + length * 2, true);
  writeStr(view, 8, "WAVE");
  writeStr(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(view, 36, "data");
  view.setUint32(40, length * 2, true);
  let offset = 44;
  for (const c of chunks) {
    for (let i = 0; i < c.length; i++) {
      view.setInt16(offset, c[i], true);
      offset += 2;
    }
  }
  return new Blob([view], { type: "audio/wav" });
}

// A reusable mic capture that yields PCM frames via onframe and can build a WAV.
function createMicCapture({ onframe } = {}) {
  return {
    ctx: null,
    source: null,
    processor: null,
    stream: null,
    chunks: [],
    async start() {
      this.chunks = [];
      // Browser-side AEC + noise suppression + AGC: stops TTS playback bleeding
      // back into the mic (enables clean barge-in) and reduces room noise.
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AudioCtx();
      this.source = this.ctx.createMediaStreamSource(this.stream);
      this.processor = this.ctx.createScriptProcessor(4096, 1, 1);
      this.processor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0);
        const pcm = downsampleToPcm16(input, this.ctx.sampleRate, STREAM_SAMPLE_RATE);
        this.chunks.push(pcm);
        if (onframe) onframe(pcm);
      };
      this.source.connect(this.processor);
      this.processor.connect(this.ctx.destination);
    },
    durationSeconds() {
      const samples = this.chunks.reduce((n, c) => n + c.length, 0);
      return samples / STREAM_SAMPLE_RATE;
    },
    stop() {
      if (this.processor) {
        this.processor.disconnect();
        this.processor.onaudioprocess = null;
        this.processor = null;
      }
      if (this.source) {
        this.source.disconnect();
        this.source = null;
      }
      if (this.ctx) {
        this.ctx.close();
        this.ctx = null;
      }
      if (this.stream) {
        this.stream.getTracks().forEach((t) => t.stop());
        this.stream = null;
      }
    },
    toWavBlob() {
      return encodeWav(this.chunks, STREAM_SAMPLE_RATE);
    },
  };
}

// ============================================================ system status
async function loadSystemStatus() {
  const host = el("system-status");
  try {
    const body = await (await fetch("/v1/system/status")).json();
    const d = body.data;
    const tiles = [];
    const tile = (label, value, ok) =>
      tiles.push(
        `<div class="stat ${ok === undefined ? "" : ok ? "ok" : "warn"}"><span>${label}</span><strong>${value}</strong></div>`
      );

    tile("Env", d.app.env);
    tile("TTS mode", d.tts.mock_enabled ? "mock" : "live", !d.tts.mock_enabled);
    tile("OmniVoice", d.tts.omnivoice_present ? "found" : "missing", d.tts.omnivoice_present);
    tile("Vosk model", d.vosk.active_model_present ? "ready" : "missing", d.vosk.active_model_present);
    tile("Whisper cache", d.whisper_local.cached ? `${d.whisper_local.active_model} ✓` : `${d.whisper_local.active_model} (on demand)`, d.whisper_local.cached);
    const remote = d.stt_engines.filter((e) => e.mode === "remote");
    const remoteOk = remote.filter((e) => e.configured).length;
    tile("Remote STT", `${remoteOk}/${remote.length} configured`, remoteOk > 0);
    tile("Artifacts", `${d.artifacts.count} · ${fmtBytes(d.artifacts.total_bytes)}`);
    host.innerHTML = tiles.join("");

    // Update header/footer status badges
    const sttOk = (d.stt_engines || []).some(e => e.available);
    const ttsOk = !d.tts.mock_enabled;
    setBadge("badge-stt", sttOk); setBadge("foot-stt", sttOk);
    setBadge("badge-tts", ttsOk); setBadge("foot-tts", ttsOk);

    // Initialize the shared preprocessing config from server defaults (once).
    if (!preprocessInit && d.stt_preprocess) {
      preprocessInit = true;
      if (el("pp-denoise")) el("pp-denoise").checked = d.stt_preprocess.noise_reduce;
      if (el("pp-vad")) el("pp-vad").checked = d.stt_preprocess.vad;

      const sel = el("pp-vad-backend");
      const avail = d.stt_preprocess.vad_backends_available || { energy: true };
      if (sel) {
        sel.innerHTML = "";
        ["energy", "silero", "pyannote"].forEach((b) => {
          const opt = document.createElement("option");
          opt.value = b;
          opt.textContent = avail[b] ? b : `${b} (not installed)`;
          opt.disabled = !avail[b];
          sel.appendChild(opt);
        });
        const want = d.stt_preprocess.vad_backend;
        sel.value = want && avail[want] ? want : "energy";
      }
      // Saved user prefs override server defaults.
      ["pp-denoise", "pp-vad", "pp-vad-backend"].forEach(restoreAndBind);
    }
  } catch (error) {
    host.innerHTML = `<div class="stat warn"><span>status</span><strong>error</strong></div>`;
  }
}
let preprocessInit = false;

// Shared STT preprocessing config (System tab) used by batch / streaming / conversation.
function getPreproc() {
  return {
    denoise: el("pp-denoise") ? el("pp-denoise").checked : false,
    vad: el("pp-vad") ? el("pp-vad").checked : false,
    backend: el("pp-vad-backend") ? el("pp-vad-backend").value || "energy" : "energy",
  };
}

// ============================================================ model manager
let modelPollTimer = null;

async function loadModels() {
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

    // ---- Qwen-Omni (audio-native STT, MLX) ----
    const qo = body.data.qwen_omni;
    if (qo && el("qwen-omni-models")) {
      if (el("qwen-omni-hint")) {
        el("qwen-omni-hint").textContent = qo.available
          ? `mlx-vlm ready — active: ${qo.active.split("/").pop()}`
          : "Needs mlx-vlm (pip install -e '.[mlx]', Apple Silicon). Audio → Vietnamese text, no Whisper.";
      }
      el("qwen-omni-models").innerHTML = qo.models.map((m) => renderQwenOmniRow(m, qo.available)).join("");
    }

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
function modelRow({ title, code, badges = "", size = "", err = "", action = "" }) {
  return `<div class="model-row"><div class="model-info"><strong>${title}</strong><code>${code}</code>${badges}<span class="model-size">${size}</span>${err}</div><div class="model-action">${action}</div></div>`;
}
const ACTIVE_BADGE = `<span class="badge mock">active</span>`;
const SPINNER_ACTION = `<div class="progress indeterminate"><div class="bar"></div></div><span class="pct">…</span>`;
const jobErr = (job) => (job && job.state === "error" ? `<span class="model-err">${job.error}</span>` : "");
const isErr = (job) => job && job.state === "error";
const useOrActive = (active, attr, key) =>
  active ? ACTIVE_BADGE : `<button class="mini" data-${attr}="${key}">Use</button>`;
const dlBtn = (attr, key, job) =>
  `<button class="mini" data-${attr}="${key}">${isErr(job) ? "Retry" : "Download"}</button>`;
// Shown when a model/mode is the *selected* one but its engine package isn't installed
// (so it is not actually running).
const SELECTED_OFF = `<span class="badge mock">selected · engine not installed</span>`;
// Managed TTS engines that are installable via the pip allowlist (/v1/models/install).
const TTS_PIP = { vieneu: "vieneu" };

// Per-section banner showing whether the engine PACKAGE is installed (the real
// "can it run" truth), with a one-click Install when available + enabled.
function engineBanner(engine, ttsEngines, installEnabled) {
  const e = ttsEngines && ttsEngines[engine];
  if (!e) return "";
  if (e.available) return `<p class="meta"><span class="badge ok">engine installed</span></p>`;
  const hint = e.install_hint || "";
  const pkg = TTS_PIP[engine];
  const btn = pkg && installEnabled ? ` <button class="mini" data-pip-install="${pkg}">Install</button>` : "";
  return `<p class="meta"><span class="badge danger">engine not installed</span> ${hint}${btn}</p>`;
}

function renderOmniRow(m, available) {
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

function renderVieneuRow(m, available) {
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

function renderWhisperRow(m) {
  let action;
  if (m.job && m.job.state === "downloading") action = SPINNER_ACTION;
  else if (m.cached) action = `${useOrActive(m.active, "w-select", m.size)}<button class="mini danger" data-w-delete="${m.size}">Delete</button>`;
  else action = dlBtn("w-download", m.size, m.job);
  return modelRow({ title: m.label, code: m.size, size: m.cached ? fmtBytes(m.size_bytes) : "", err: jobErr(m.job), action });
}

function renderQwenOmniRow(m, available) {
  let action;
  if (m.job && m.job.state === "downloading") action = SPINNER_ACTION;
  else if (m.cached) {
    const use = m.active
      ? (available ? ACTIVE_BADGE : SELECTED_OFF)
      : `<button class="mini" data-qo-select="${m.model}">Use</button>`;
    action = `${use}<button class="mini danger" data-qo-delete="${m.model}">Delete</button>`;
  } else action = dlBtn("qo-download", m.model, m.job);
  return modelRow({ title: m.label, code: m.model, size: m.cached ? fmtBytes(m.size_bytes) : "", err: jobErr(m.job), action });
}

function renderModelRow(name, label, size, installedNames, jobs, installed, activeName) {
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
const MODEL_ENGINES = {
  vosk: { keyName: "name", deleteVerb: "DELETE", refresh: () => loadSttEngines() },
  whisper: { keyName: "size", deleteVerb: "DELETE", refresh: () => loadSttEngines() },
  omnivoice: { keyName: "id", deleteVerb: "POST", refresh: () => loadTtsEngines() },
  vieneu: { keyName: "mode", deleteVerb: "POST", refresh: () => loadTtsEngines() },
  llm: { keyName: "model", deleteVerb: "POST", refresh: () => {} },
  "qwen-omni": { keyName: "model", deleteVerb: "POST", refresh: () => loadSttEngines() },
};

const MODEL_ATTRS = [
  ["data-download", "vosk", "download"], ["data-delete", "vosk", "delete"], ["data-v-select", "vosk", "select"],
  ["data-w-download", "whisper", "download"], ["data-w-delete", "whisper", "delete"], ["data-w-select", "whisper", "select"],
  ["data-o-download", "omnivoice", "download"], ["data-o-delete", "omnivoice", "delete"], ["data-o-select", "omnivoice", "select"],
  ["data-vn-download", "vieneu", "download"], ["data-vn-delete", "vieneu", "delete"], ["data-vn-select", "vieneu", "select"],
  ["data-llm-download", "llm", "download"], ["data-llm-delete", "llm", "delete"], ["data-llm-select", "llm", "select"],
  ["data-qo-download", "qwen-omni", "download"], ["data-qo-delete", "qwen-omni", "delete"], ["data-qo-select", "qwen-omni", "select"],
];

function bindModelButtons() {
  MODEL_ATTRS.forEach(([attr, engine, action]) => {
    document.querySelectorAll(`[${attr}]`).forEach((btn) => {
      btn.addEventListener("click", () => runModelAction(engine, action, btn.getAttribute(attr)));
    });
  });
}

async function runModelAction(engine, action, key) {
  const cfg = MODEL_ENGINES[engine];
  if (action === "delete" && !confirm(`Delete ${engine} "${key}"?`)) return;
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

function bindDownloadByName(btnId, inputId, engine) {
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

// ============================================================ model recommender
let recommendData = null;
let recommendActions = [];
const REC_CHIP_GROUPS = [
  ["apple_silicon", "Apple Silicon (MLX)"],
  ["cpu", "CPU"],
  ["nvidia_gpu", "NVIDIA GPU"],
];
const REC_CAT_LABELS = { stt: "STT", tts: "TTS", llm: "LLM", vad: "VAD" };

function recCapChips(c) {
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

function recStatusBadge(it) {
  if (it.status.startsWith("incompatible")) return `<span class="badge danger">${it.status}</span>`;
  if (it.status.startsWith("needs")) return `<span class="badge mock">${it.status}</span>`;
  if (it.status === "installed") return `<span class="badge ok">installed</span>`;
  return `<span class="badge ok">runnable</span>`;
}

function recRow(it) {
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

function renderRecommend() {
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

async function loadRecommend() {
  try {
    const body = await (await fetch("/v1/models/recommend")).json();
    recommendData = body.data;
    renderRecommend();
  } catch (error) {
    el("recommend-list").innerHTML = `<p class="meta error">${error}</p>`;
  }
}

async function recAct(action) {
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
function isLocalLlm(url) {
  return /\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0)/.test(url || "");
}

function renderLlmOnlineStatus(d) {
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
const LLM_PRESETS = {
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

async function loadLlmOnlineConfig() {
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

function renderLlmRow(m, jobs, available) {
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

// ============================================================ STT engines list
let engineDetails = {};

function updateEngineDetail(selectId, detailId) {
  const det = el(detailId);
  if (!det) return;
  const engine = el(selectId).value;
  const detail = engineDetails[engine];
  det.textContent = detail ? `model: ${detail}` : "";
}

async function loadSttEngines() {
  const pairs = [
    ["stt-engine", "stt-engine-detail"],
    ["stt-stream-engine", "stt-stream-engine-detail"],
    ["v2t-stt-engine", null],
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

// ============================================================ STT batch (file or recording)
let sttBatchRecorder = null;
let sttRecordedBlob = null;
let sttMode = "upload";

function initSttMode() {
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
  form.append("engine", el("stt-engine").value || "vosk");
  if (el("stt-language").value.trim()) form.append("language", el("stt-language").value.trim());
  const pp = getPreproc();
  form.append("denoise", pp.denoise ? "true" : "false");
  form.append("vad", pp.vad ? "true" : "false");
  form.append("vad_backend", pp.backend);

  print(sttResult, `Transcribing ${file ? "file" : "recording"}...`);
  try {
    const response = await fetch("/v1/stt/transcribe", { method: "POST", body: form });
    const body = await response.json();
    print(sttResult, body, !response.ok);
  } catch (error) {
    print(sttResult, { error: String(error) }, true);
  }
});

// ============================================================ STT streaming (WebSocket)
const sttStream = { ws: null, capture: null, transcript: "", logLines: [] };

function setStreamStatus(text, state) {
  const node = el("stt-stream-status");
  node.textContent = text;
  node.className = state;
}

// Single source of truth for the streaming buttons.
// idle: only Start enabled · starting: both disabled · recording: only Stop enabled
function setStreamUI(state) {
  el("stt-stream-record").disabled = state !== "idle";
  el("stt-stream-end").disabled = state !== "recording";
}

function toggleStreamingAvailability(enabled) {
  // No realtime-capable engine (e.g. no Vosk model) -> hide the whole streaming card.
  const card = el("stt-stream-card");
  if (card) card.classList.toggle("hidden", !enabled);
  if (enabled && !sttStream.ws) setStreamUI("idle");
}
function appendStreamLog(label, payload) {
  sttStream.logLines.push(`${label}: ${JSON.stringify(payload)}`);
  if (sttStream.logLines.length > 50) sttStream.logLines.shift();
  el("stt-stream-log").textContent = sttStream.logLines.join("\n");
}

async function startStreaming() {
  setStreamUI("starting");
  const engine = el("stt-stream-engine").value || "vosk";
  const language = el("stt-stream-language").value.trim();
  sttStream.transcript = "";
  sttStream.logLines = [];
  el("stt-stream-transcript").textContent = "";
  el("stt-stream-partial").textContent = "—";
  el("stt-stream-log").textContent = "";

  const pp = getPreproc();
  let params = `engine=${encodeURIComponent(engine)}&sample_rate=${STREAM_SAMPLE_RATE}`;
  if (language) params += `&language=${encodeURIComponent(language)}`;
  params += `&denoise=${pp.denoise}&vad=${pp.vad}&vad_backend=${encodeURIComponent(pp.backend)}`;

  let capture;
  try {
    capture = createMicCapture({
      onframe: (pcm) => {
        if (sttStream.ws && sttStream.ws.readyState === WebSocket.OPEN) sttStream.ws.send(pcm.buffer);
      },
    });
  } catch (error) {
    setStreamStatus("mic error", "status-error");
    setStreamUI("idle");
    return;
  }

  const ws = new WebSocket(wsUrl(`/v1/stt/stream?${params}`));
  sttStream.ws = ws;

  ws.onopen = async () => {
    try {
      await capture.start();
      sttStream.capture = capture;
      setStreamStatus("● recording", "status-rec");
      setStreamUI("recording");
    } catch (error) {
      setStreamStatus("mic denied", "status-error");
      ws.close();
    }
  };

  ws.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }
    appendStreamLog(data.event_type, data.payload ?? {});
    if (data.event_type === "partial") {
      el("stt-stream-partial").textContent = data.payload?.text || "…";
    } else if (data.event_type === "final") {
      const text = (data.payload?.text || "").trim();
      if (text) {
        sttStream.transcript = `${sttStream.transcript} ${text}`.trim();
        el("stt-stream-transcript").textContent = sttStream.transcript;
      }
      el("stt-stream-partial").textContent = "—";
    } else if (data.event_type === "error") {
      setStreamStatus("error", "status-error");
    } else if (data.event_type === "done") {
      stopStreaming();
    }
  };

  ws.onerror = () => setStreamStatus("ws error", "status-error");
  ws.onclose = () => {
    setStreamUI("idle");
    if (el("stt-stream-status").className !== "status-error") setStreamStatus("idle", "status-idle");
  };
}

function endStreaming() {
  setStreamUI("starting");
  setStreamStatus("finalizing…", "status-idle");
  if (sttStream.capture) {
    sttStream.capture.stop();
    sttStream.capture = null;
  }
  if (sttStream.ws && sttStream.ws.readyState === WebSocket.OPEN) {
    sttStream.ws.send(JSON.stringify({ type: "end" }));
  }
}
function stopStreaming() {
  if (sttStream.capture) {
    sttStream.capture.stop();
    sttStream.capture = null;
  }
  if (sttStream.ws) {
    if (sttStream.ws.readyState === WebSocket.OPEN) sttStream.ws.close();
    sttStream.ws = null;
  }
}

el("stt-stream-record").addEventListener("click", startStreaming);
el("stt-stream-end").addEventListener("click", endStreaming);

// ============================================================ TTS engines + voices
let ttsEngineDetails = {};

async function loadTtsEngines() {
  try {
    const body = await (await fetch("/v1/tts/engines")).json();
    const items = body.data.filter((e) => e.available);
    ttsEngineDetails = {};
    body.data.forEach((e) => (ttsEngineDetails[e.engine] = e.detail));
    const def = (body.data.find((e) => e.default) || items[0] || {}).engine;

    renderTtsEnginesStatus(body.data);

    [["tts-engine", "tts-engine-detail"], ["tts-stream-engine", "tts-stream-engine-detail"], ["t2v-tts-engine", "t2v-engine-detail"], ["pf-tts-engine", null]].forEach(
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

function renderTtsEnginesStatus(engines) {
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

function updateTtsEngine(selId, detId) {
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
  if (selId === "pf-tts-engine") {
    const isVieneu = engine === "vieneu";
    const wrap = el("pf-tts-voice-wrap");
    if (wrap) wrap.classList.toggle("hidden", !isVieneu);
    if (isVieneu) {
      fetch("/v1/tts/voices?engine=vieneu").then(r => r.json()).then(b => {
        const sel = el("pf-tts-voice");
        if (!sel) return;
        sel.innerHTML = '<option value="">(auto)</option>';
        b.data.forEach(v => { const o = document.createElement("option"); o.value = v.voice; o.textContent = v.label; sel.appendChild(o); });
      }).catch(() => {});
    }
  }
}

async function loadTtsVoices() {
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

// ============================================================ TTS batch
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
    const response = await fetch("/v1/tts/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: ttsText.value.trim(),
        engine: el("tts-engine").value || "omnivoice",
        voice: el("tts-voice").value || null,
      }),
    });
    const body = await response.json();
    print(ttsResult, body, !response.ok);
    if (response.ok && body.data?.audio_url) {
      ttsAudio.src = body.data.audio_url;
      ttsAudio.classList.remove("hidden");
      ttsMeta.textContent = `${body.data.duration_seconds ?? "?"}s @ ${body.data.sample_rate}Hz${body.data.mock ? " · mock audio" : ""}`;
    }
  } catch (error) {
    print(ttsResult, { error: String(error) }, true);
  }
});

// ============================================================ TTS stream (SSE) + progressive playback
const ttsStream = { source: null, queue: [], playing: false, lines: [] };

function stopEventSource() {
  if (ttsStream.source) {
    ttsStream.source.close();
    ttsStream.source = null;
  }
}
function appendStreamEvent(label, payload) {
  ttsStream.lines.push(`${label}: ${JSON.stringify(payload)}`);
  el("tts-stream-events").textContent = ttsStream.lines.join("\n");
}
function playNextChunk() {
  const audio = el("tts-stream-audio");
  if (ttsStream.playing) return;
  const next = ttsStream.queue.shift();
  if (!next) return;
  ttsStream.playing = true;
  audio.src = next;
  audio.classList.remove("hidden");
  audio.play().catch(() => {
    ttsStream.playing = false;
  });
}
el("tts-stream-audio").addEventListener("ended", () => {
  ttsStream.playing = false;
  playNextChunk();
});
el("tts-stream-stop").addEventListener("click", () => {
  stopEventSource();
  el("tts-job").textContent = "Stopped listening SSE";
});

el("tts-stream-start").addEventListener("click", async () => {
  const text = el("tts-stream-text").value.trim();
  if (!text) {
    print(el("tts-stream-events"), "Please provide stream text", true);
    return;
  }
  stopEventSource();
  ttsStream.queue = [];
  ttsStream.playing = false;
  ttsStream.lines = [];
  el("tts-stream-chunks").innerHTML = "";
  el("tts-stream-audio").classList.add("hidden");
  print(el("tts-stream-events"), "Creating stream job...");

  try {
    const response = await fetch("/v1/tts/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, engine: el("tts-stream-engine").value || "omnivoice" }),
    });
    const body = await response.json();
    if (!response.ok || !body.success) {
      print(el("tts-stream-events"), body, true);
      return;
    }
    const jobId = body.data.job_id;
    el("tts-job").textContent = `Job ID: ${jobId}`;

    const source = new EventSource(`/v1/events/jobs/${jobId}`);
    ttsStream.source = source;

    source.addEventListener("queued", (e) => appendStreamEvent("queued", JSON.parse(e.data)));
    source.addEventListener("audio_chunk", (e) => {
      const payload = JSON.parse(e.data).payload;
      appendStreamEvent("audio_chunk", payload);
      renderChunkItem(payload);
      if (payload.audio_url) {
        ttsStream.queue.push(payload.audio_url);
        playNextChunk();
      }
    });
    source.addEventListener("error", (e) => {
      try {
        appendStreamEvent("error", JSON.parse(e.data).payload);
      } catch {
        ttsStream.lines.push("error: SSE connection closed or failed");
        el("tts-stream-events").textContent = ttsStream.lines.join("\n");
      }
    });
    source.addEventListener("done", (e) => {
      appendStreamEvent("done", JSON.parse(e.data));
      stopEventSource();
    });
  } catch (error) {
    print(el("tts-stream-events"), { error: String(error) }, true);
  }
});

function renderChunkItem(payload) {
  const li = document.createElement("li");
  const text = document.createElement("span");
  text.className = "chunk-text";
  text.textContent = payload.text || "(chunk)";
  li.appendChild(text);
  if (payload.mock) {
    const badge = document.createElement("span");
    badge.className = "badge mock";
    badge.textContent = "mock";
    li.appendChild(badge);
  }
  if (payload.audio_url) {
    const play = document.createElement("button");
    play.className = "mini";
    play.textContent = "▶";
    play.addEventListener("click", () => {
      const audio = el("tts-stream-audio");
      audio.src = payload.audio_url;
      audio.classList.remove("hidden");
      audio.play().catch(() => {});
    });
    li.appendChild(play);
  }
  el("tts-stream-chunks").appendChild(li);
}

// ============================================================ conversation
const conv = { ws: null, capture: null, log: [], ctx: null, nextTime: 0, sources: [], chain: null, assistantBubble: null, opusMode: false, opusDec: null, opusTs: 0, outRate: 24000 };

function setConvStatus(text, state) {
  const node = el("conv-status");
  node.textContent = text;
  node.className = state;
}
function convLog(line) {
  conv.log.push(line);
  if (conv.log.length > 60) conv.log.shift();
  el("conv-log").textContent = conv.log.join("\n");
}
function addBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  el("chat-dialogue").appendChild(div);
  el("chat-dialogue").scrollTop = el("chat-dialogue").scrollHeight;
  return div;
}
function convAudioCtx() {
  if (!conv.ctx) conv.ctx = new (window.AudioContext || window.webkitAudioContext)();
  return conv.ctx;
}
// True while assistant audio is still scheduled/playing (+small tail).
function convIsSpeaking() {
  return !!conv.ctx && (conv.nextTime || 0) > conv.ctx.currentTime + 0.15;
}
function convStopAudio() {
  (conv.sources || []).forEach((s) => {
    try {
      s.stop();
    } catch {}
  });
  conv.sources = [];
  conv.nextTime = 0;
  conv.chain = Promise.resolve();
  convResetOpus(); // flush any in-flight Opus decode so stale audio can't play
}
// Gapless playback: decode each chunk and schedule it back-to-back on the audio
// timeline (no <audio> src-swap gaps, no queue underrun between sentences).
function convEnqueueAudio(url) {
  conv.chain = (conv.chain || Promise.resolve())
    .then(async () => {
      const ctx = convAudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      const data = await (await fetch(url)).arrayBuffer();
      const buf = await ctx.decodeAudioData(data);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      const start = Math.max(ctx.currentTime + 0.05, conv.nextTime || 0);
      src.start(start);
      conv.nextTime = start + buf.duration;
      conv.sources.push(src);
      src.onended = () => {
        conv.sources = conv.sources.filter((s) => s !== src);
      };
    })
    .catch((e) => convLog("audio error: " + e));
}

// ---- Opus downlink (WebCodecs): decode streamed Opus frames in-browser ----
// Same gapless scheduling as convEnqueueAudio, but the audio arrives as raw 60ms
// Opus packets (binary WS frames) decoded by AudioDecoder instead of fetched WAVs.
function convOpusSupported() {
  return typeof window.AudioDecoder === "function" && typeof window.EncodedAudioChunk === "function";
}
function convScheduleBuffer(buf) {
  const ctx = convAudioCtx();
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(ctx.destination);
  const start = Math.max(ctx.currentTime + 0.05, conv.nextTime || 0);
  src.start(start);
  conv.nextTime = start + buf.duration;
  conv.sources.push(src);
  src.onended = () => {
    conv.sources = conv.sources.filter((s) => s !== src);
  };
}
function convInitOpusDecoder() {
  if (conv.opusDec) {
    try {
      conv.opusDec.close();
    } catch {}
  }
  conv.opusTs = 0;
  const ctx = convAudioCtx();
  const dec = new AudioDecoder({
    output: (audioData) => {
      try {
        const frames = audioData.numberOfFrames;
        const buf = ctx.createBuffer(1, frames, audioData.sampleRate);
        const arr = new Float32Array(frames);
        audioData.copyTo(arr, { planeIndex: 0, format: "f32-planar" });
        buf.copyToChannel(arr, 0);
        convScheduleBuffer(buf);
      } catch (e) {
        convLog("opus output error: " + e);
      } finally {
        audioData.close();
      }
    },
    error: (e) => convLog("opus decoder error: " + e),
  });
  dec.configure({ codec: "opus", sampleRate: conv.outRate, numberOfChannels: 1 });
  conv.opusDec = dec;
}
function convFeedOpus(data) {
  if (!conv.opusDec || conv.opusDec.state === "closed") convInitOpusDecoder();
  try {
    conv.opusDec.decode(new EncodedAudioChunk({ type: "key", timestamp: conv.opusTs, data }));
    conv.opusTs += 60000; // 60ms frames (microseconds)
  } catch (e) {
    convLog("opus feed error: " + e);
  }
}
function convResetOpus() {
  if (conv.opusDec) {
    try {
      conv.opusDec.close();
    } catch {}
    conv.opusDec = null;
  }
  conv.opusTs = 0;
}

const convDetails = { stt: {}, tts: {}, llm: "" };

function updateConvEnginesInfo() {
  const info = el("conv-engines-info");
  if (!info) return;
  const sttEng = el("conv-stt-engine")?.value || "";
  const ttsEng = el("conv-tts-engine")?.value || "";
  // Strip the " · cached" status suffix so the model name reads clearly.
  const clean = (d) => (d || "").split(" · ")[0];
  const sttDet = clean(convDetails.stt[sttEng]);
  const ttsDet = clean(convDetails.tts[ttsEng]);
  const llmPart = convDetails.llm ? `LLM: ${convDetails.llm}` : "LLM: echo (no LLM configured)";
  const sttPart = `STT: ${sttEng}${sttDet ? ` → ${sttDet}` : ""}`;
  const ttsPart = `TTS: ${ttsEng}${ttsDet ? ` → ${ttsDet}` : ""}`;
  info.textContent = `${sttPart} · ${llmPart} · ${ttsPart}`;
}

async function loadConversationEngines() {
  try {
    const [stt, tts, models] = await Promise.all([
      (await fetch("/v1/stt/engines")).json(),
      (await fetch("/v1/tts/engines")).json(),
      (await fetch("/v1/models")).json().catch(() => null),
    ]);
    stt.data.forEach((e) => (convDetails.stt[e.engine] = e.detail));
    tts.data.forEach((e) => (convDetails.tts[e.engine] = e.detail));
    convDetails.llm = models?.data?.llm?.active || "";
    const fill = (id, items, label) => {
      const sel = el(id);
      if (!sel) return;
      sel.innerHTML = "";
      items.forEach((e) => {
        const opt = document.createElement("option");
        opt.value = e.engine;
        opt.textContent = label(e);
        sel.appendChild(opt);
      });
    };
    fill("conv-stt-engine", stt.data.filter((e) => e.available), (e) => `${e.engine}`);
    fill("conv-tts-engine", tts.data.filter((e) => e.available), (e) => `${e.engine}`);
    // Prefer the fast Apple-GPU engine when available, else whisper; VieNeu for TTS.
    const sttSel = el("conv-stt-engine");
    const sttPref = ["whisper_mlx", "whisper"].find((v) => [...(sttSel?.options || [])].some((o) => o.value === v));
    if (sttSel && sttPref) sttSel.value = sttPref;
    const ttsSel = el("conv-tts-engine");
    if (ttsSel && [...ttsSel.options].some((o) => o.value === "vieneu")) ttsSel.value = "vieneu";
    restoreAndBind("conv-stt-engine");
    restoreAndBind("conv-tts-engine");
    restoreAndBind("conv-language");
    restoreAndBind("conv-opus");
    el("conv-tts-engine").addEventListener("change", convVoiceToggle);
    el("conv-stt-engine").addEventListener("change", updateConvEnginesInfo);
    el("conv-tts-engine").addEventListener("change", updateConvEnginesInfo);
    convVoiceToggle();
    updateConvEnginesInfo();
  } catch (error) {
    convLog(`engines error: ${error}`);
  }
}

function convVoiceToggle() {
  const isVieneu = el("conv-tts-engine").value === "vieneu";
  el("conv-voice-wrap").classList.toggle("hidden", !isVieneu);
  if (isVieneu && !el("conv-voice").dataset.loaded) {
    fetch("/v1/tts/voices?engine=vieneu")
      .then((r) => r.json())
      .then((b) => {
        const sel = el("conv-voice");
        sel.innerHTML = '<option value="">(auto)</option>';
        b.data.forEach((v) => {
          const opt = document.createElement("option");
          opt.value = v.voice;
          opt.textContent = v.label;
          sel.appendChild(opt);
        });
        sel.dataset.loaded = "1";
        restoreAndBind("conv-voice");
      })
      .catch(() => {});
  }
}

function setConvUI(state) {
  el("conv-start").disabled = state !== "idle";
  el("conv-stop").disabled = state === "idle";
}

async function startConversation() {
  setConvUI("starting");
  convStopAudio();
  conv.assistantBubble = null;
  el("chat-dialogue").innerHTML = "";
  conv.log = [];
  el("conv-log").textContent = "";

  const sttEngine = el("conv-stt-engine").value;
  if (!sttEngine) {
    setConvStatus("Không có STT engine khả dụng", "status-error");
    setConvUI("idle");
    return;
  }

  // Warm up the STT engine so the first turn doesn't stall loading the model.
  // The /warm endpoint is a fast no-op for engines with no warm() method.
  setConvStatus("⏳ khởi động STT engine…", "status-idle");
  try {
    const warmRes = await fetch(`/v1/stt/warm?engine=${encodeURIComponent(sttEngine)}`, { method: "POST" });
    if (!warmRes.ok) {
      setConvStatus(`STT engine '${sttEngine}' chưa sẵn sàng`, "status-error");
      setConvUI("idle");
      return;
    }
  } catch {
    setConvStatus("Không thể kết nối STT engine", "status-error");
    setConvUI("idle");
    return;
  }

  let params = `stt_engine=${encodeURIComponent(el("conv-stt-engine").value)}`;
  params += `&tts_engine=${encodeURIComponent(el("conv-tts-engine").value)}&sample_rate=${STREAM_SAMPLE_RATE}`;
  const activeProfile = el("profile-select")?.value;
  if (activeProfile) params += `&profile=${encodeURIComponent(activeProfile)}`;
  if (currentSessionId) params += `&session_id=${encodeURIComponent(currentSessionId)}`;
  if (el("conv-voice").value) params += `&voice=${encodeURIComponent(el("conv-voice").value)}`;
  if (el("conv-language").value.trim()) params += `&language=${encodeURIComponent(el("conv-language").value.trim())}`;
  const cpp = getPreproc();
  params += `&denoise=${cpp.denoise}&vad=${cpp.vad}&vad_backend=${encodeURIComponent(cpp.backend)}`;

  // Opus downlink: stream reply audio as Opus frames decoded in-browser (WebCodecs).
  // Falls back to the default URL/WAV path if unchecked or unsupported.
  conv.opusMode = !!el("conv-opus")?.checked && convOpusSupported();
  if (el("conv-opus")?.checked && !conv.opusMode) {
    convLog("Opus downlink unsupported in this browser — using WAV/URL.");
  }
  if (conv.opusMode) {
    conv.outRate = 24000;
    params += `&output=audio,text&audio_out=opus&output_sample_rate=${conv.outRate}`;
  }
  convResetOpus();

  let capture;
  try {
    capture = createMicCapture({
      onframe: (pcm) => {
        if (!conv.ws || conv.ws.readyState !== WebSocket.OPEN) return;
        // Half-duplex: while the assistant is speaking, don't send mic audio —
        // otherwise its own voice (speaker echo) is mistaken for the user
        // barging in and the reply gets cut off after 1-2 words.
        if (convIsSpeaking()) return;
        conv.ws.send(pcm.buffer);
      },
    });
  } catch (error) {
    setConvStatus("mic error", "status-error");
    setConvUI("idle");
    return;
  }

  const ws = new WebSocket(wsUrl(`/v1/conversation/stream?${params}`));
  conv.ws = ws;
  if (conv.opusMode) ws.binaryType = "arraybuffer";

  ws.onopen = async () => {
    try {
      await capture.start();
      conv.capture = capture;
      setConvStatus("● listening", "status-rec");
      setConvUI("recording");
    } catch (error) {
      setConvStatus("mic denied", "status-error");
      ws.close();
    }
  };

  ws.onmessage = (event) => {
    // Binary frames are Opus packets (audio_out=opus) — decode + play in-browser.
    if (typeof event.data !== "string") {
      convFeedOpus(event.data);
      return;
    }
    let d;
    try {
      d = JSON.parse(event.data);
    } catch {
      return;
    }
    convLog(`${d.event}: ${d.text ? d.text.slice(0, 60) : JSON.stringify({ ...d, event: undefined })}`);
    switch (d.event) {
      case "session_started": {
        if (d.session_id) currentSessionId = d.session_id;
        if (d.output_sample_rate) conv.outRate = d.output_sample_rate;
        // Authoritative: show exactly which models this session is using.
        const info = el("conv-engines-info");
        if (info) {
          const sttPart = `STT: ${d.stt_engine}${d.stt_detail ? ` → ${d.stt_detail}` : ""}`;
          const llmPart = d.responder === "llm" ? `LLM: ${d.llm_model}` : "LLM: echo (no LLM configured)";
          const ttsPart = `TTS: ${d.tts_engine}${d.tts_detail ? ` → ${d.tts_detail}` : ""}`;
          info.textContent = `${sttPart} · ${llmPart} · ${ttsPart}`;
        }
        // Engines may still be cold-loading — tell the user to hold off speaking
        // instead of letting them talk into a pipeline that'll lose the start of
        // their utterance. Cleared by the "engines_ready" event below.
        if (d.stt_ready === false || d.tts_ready === false) {
          setConvStatus("⏳ engines warming up, please wait…", "status-idle");
        }
        break;
      }
      case "engines_ready":
        setConvStatus("● listening", "status-rec");
        break;
      case "speech_start":
        setConvStatus("● you're speaking", "status-rec");
        break;
      case "speech_end":
        setConvStatus("… thinking", "status-idle");
        break;
      case "user_transcript":
        if (d.text) addBubble("user", d.text);
        break;
      case "response_text":
        // Sentences stream in; build one assistant bubble per turn.
        if (d.chunk_index === 0 || !conv.assistantBubble) {
          conv.assistantBubble = addBubble("assistant", d.text);
        } else {
          conv.assistantBubble.textContent += " " + d.text;
          el("chat-dialogue").scrollTop = el("chat-dialogue").scrollHeight;
        }
        break;
      case "audio_start":
        // Opus stream chunk begins; ensure the decoder matches the server's rate.
        if (d.codec === "opus" && d.sample_rate) conv.outRate = d.sample_rate;
        break;
      case "audio_chunk":
        if (d.audio_url) convEnqueueAudio(d.audio_url); // URL/WAV mode (non-opus)
        break;
      case "turn_done":
        setConvStatus("● listening", "status-rec");
        break;
      case "aborted":
        convStopAudio();  // barge-in / interrupt: stop playback immediately
        break;
      case "error":
        setConvStatus(`error: ${d.message || ""}`, "status-error");
        break;
    }
  };

  ws.onerror = () => setConvStatus("ws error", "status-error");
  ws.onclose = () => {
    setConvUI("idle");
    if (el("conv-status").className !== "status-error") setConvStatus("idle", "status-idle");
  };
}

function stopConversation() {
  if (conv.capture) {
    conv.capture.stop();
    conv.capture = null;
  }
  if (conv.ws) {
    if (conv.ws.readyState === WebSocket.OPEN) conv.ws.send(JSON.stringify({ type: "end" }));
    try {
      conv.ws.close();
    } catch {}
    conv.ws = null;
  }
  setConvUI("idle");
}

el("conv-start").addEventListener("click", startConversation);
el("conv-stop").addEventListener("click", () => {
  convStopAudio();
  stopConversation();
});
el("conv-reset").addEventListener("click", () => {
  convStopAudio();
  conv.assistantBubble = null;
  el("chat-dialogue").innerHTML = "";
  if (conv.ws && conv.ws.readyState === WebSocket.OPEN) conv.ws.send(JSON.stringify({ type: "reset" }));
});

// ============================================================ LLM text chat
const chat = { history: [], busy: false };
let currentSessionId = null;

function chatBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  const dialogue = el("chat-dialogue");
  if (dialogue) {
    dialogue.appendChild(div);
    dialogue.scrollTop = dialogue.scrollHeight;
  }
  return div;
}

async function sendChat() {
  const input = el("chat-input");
  const text = input.value.trim();
  if (!text || chat.busy) return;
  chat.busy = true;
  el("chat-send").disabled = true;
  input.value = "";

  chat.history.push({ role: "user", content: text });
  chatBubble("user", text);
  const pending = chatBubble("assistant", "…");

  try {
    const profileVal = el("profile-select")?.value;
    const params = new URLSearchParams();
    if (profileVal) params.set("profile", profileVal);
    if (currentSessionId) params.set("session_id", currentSessionId);
    const qs = params.toString();
    const chatUrl = qs ? `/v1/conversation/chat?${qs}` : "/v1/conversation/chat";
    // Send only the new turn: the backend prefixes stored session context by
    // session_id, so resending chat.history here would double up persisted
    // messages and duplicate context sent to the LLM on every subsequent turn.
    const resp = await fetch(chatUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: [{ role: "user", content: text }] }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      pending.textContent = `error: ${body.error || JSON.stringify(body)}`;
      pending.classList.add("error");
    } else {
      pending.textContent = body.data.reply;
      chat.history.push({ role: "assistant", content: body.data.reply });
      if (body.data.session_id) currentSessionId = body.data.session_id;
      el("chat-hint").textContent = `Responder: ${body.data.responder}${body.data.responder === "llm" ? " · " + body.data.model : " (no LLM configured — set CONVERSATION_LLM_BASE_URL)"}`;
    }
  } catch (error) {
    pending.textContent = `error: ${error}`;
    pending.classList.add("error");
  } finally {
    chat.busy = false;
    el("chat-send").disabled = false;
    input.focus();
  }
}

el("chat-send").addEventListener("click", sendChat);
el("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
});
el("chat-reset").addEventListener("click", () => {
  chat.history = [];
  currentSessionId = null;
  const dialogue = el("chat-dialogue");
  if (dialogue) dialogue.innerHTML = "";
  convStopAudio();
  conv.assistantBubble = null;
});

// ============================================================ sidebar nav
function initSidebar() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const section = btn.getAttribute("data-section");
      document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".section").forEach((s) => {
        s.classList.toggle("active", s.id === `section-${section}`);
      });
      if (section === "models") loadRecommend();
      if (section === "mcp") loadMcpServers();
    });
  });

  const toggle = el("sidebar-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      el("sidebar").classList.toggle("collapsed");
    });
  }
}

// ============================================================ module-level state (must precede init)
const CHAT_MODES = {
  "text-text":   { title: "Text Chat",      hint: "Text chat with the configured LLM." },
  "voice-voice": { title: "Voice Chat",     hint: "Speak — VAD detects your pause, transcribes, LLM replies, TTS plays back." },
  "voice-text":  { title: "Voice → Text",   hint: "Live microphone transcription. No LLM, no TTS." },
  "text-voice":  { title: "Text → Voice",   hint: "Type text to synthesize and play back." },
};
let chatMode = "text-text";
let profileData = {};
let profileEditMode = null; // null | "new" | "<profile-name>"
let mcpServerData = {};     // loaded first so profile panel can use it
const v2t = { ws: null, capture: null };

// ============================================================ init
initSidebar();
initSttMode();
initChatModes();
setStreamUI("idle");
setConvUI("idle");
// Persist + restore free-text controls (selects/checkboxes are bound after they populate).
["stt-language", "stt-stream-language"].forEach(restoreAndBind);
loadSttEngines();
loadTtsEngines();
loadConversationEngines();
loadSystemStatus();
loadModels();
loadRecommend();
loadLlmOnlineConfig();
loadProfiles();
loadMcpServers();

// ============================================================ status badges
function setBadge(id, ok) {
  const e = el(id);
  if (!e) return;
  e.classList.toggle("ok", !!ok);
  e.classList.toggle("err", !ok);
}

// ============================================================ chat modes
function setChatMode(mode) {
  chatMode = mode;
  document.querySelectorAll("#chat-mode-seg .seg-btn").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-mode") === mode);
  });
  ["text-text", "voice-voice", "voice-text", "text-voice"].forEach((m) => {
    const pane = el(`mode-${m}`);
    if (pane) pane.classList.toggle("hidden", m !== mode);
  });
  const info = CHAT_MODES[mode] || {};
  if (el("chat-section-title")) el("chat-section-title").textContent = info.title || "Chat";
  if (el("chat-hint")) el("chat-hint").textContent = info.hint || "";
  const enginesInfo = el("conv-engines-info");
  if (enginesInfo) enginesInfo.classList.toggle("hidden", mode !== "voice-voice");
}

function initChatModes() {
  document.querySelectorAll("#chat-mode-seg .seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => setChatMode(btn.getAttribute("data-mode")));
  });
  setChatMode("text-text");
}

// ============================================================ profiles
async function loadProfiles() {
  try {
    const body = await (await fetch("/v1/profiles")).json();
    profileData = body.data || {};
    renderProfileSelect();
  } catch {
    /* ignore */
  }
}

function renderProfileSelect() {
  const sel = el("profile-select");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">(none — server defaults)</option>';
  Object.keys(profileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (profileData[prev]) sel.value = prev;
}

function openProfilePanel(mode, name) {
  profileEditMode = mode === "new" ? "new" : name;
  const panel = el("profile-panel");
  if (!panel) return;

  let selectedMcpServers = [];

  if (mode === "new") {
    el("pf-name").value = "";
    el("pf-name").disabled = false;
    el("pf-nickname").value = "";
    el("pf-system-prompt").value = "";
    el("pf-llm-url").value = "";
    el("pf-llm-model").value = "";
    el("pf-llm-key").value = "";
    if (el("pf-tts-engine")) el("pf-tts-engine").value = "";
    el("pf-delete-btn").classList.add("hidden");
    el("pf-mem-enabled").checked = true;
    el("pf-mem-mode").value = "all";
    el("pf-mem-list").innerHTML = "";
  } else {
    const p = profileData[name];
    if (!p) return;
    el("pf-name").value = name;
    el("pf-name").disabled = true;
    el("pf-nickname").value = p.nickname || "";
    el("pf-system-prompt").value = p.system_prompt || "";
    el("pf-llm-url").value = p.llm?.base_url || "";
    el("pf-llm-model").value = p.llm?.model || "";
    el("pf-llm-key").value = "";
    if (el("pf-tts-engine")) el("pf-tts-engine").value = p.tts?.engine || "";
    el("pf-delete-btn").classList.remove("hidden");
    selectedMcpServers = p.mcp_servers || [];
    el("pf-mem-enabled").checked = p.memory?.enabled ?? true;
    el("pf-mem-mode").value = p.memory?.mode || "all";
    loadMemories(name);
  }

  el("pf-status").textContent = "";
  panel.classList.remove("hidden");
  pfUpdateTtsVoice();
  renderProfileMcpList(selectedMcpServers);
}

function closeProfilePanel() {
  profileEditMode = null;
  const panel = el("profile-panel");
  if (panel) panel.classList.add("hidden");
}

function pfUpdateTtsVoice() {
  const eng = el("pf-tts-engine");
  const wrap = el("pf-tts-voice-wrap");
  if (!eng || !wrap) return;
  wrap.classList.toggle("hidden", eng.value !== "vieneu");
}

function renderProfileMcpList(selectedServers) {
  const container = el("pf-mcp-list");
  if (!container) return;
  const selectedUrls = new Set((selectedServers || []).map((s) => s.url));
  const servers = Object.values(mcpServerData);
  if (!servers.length) {
    container.innerHTML = '<p class="hint" style="margin:0">No global MCP servers. Add them in the MCP section first.</p>';
    return;
  }
  container.innerHTML = servers.map((s) => `
    <label class="pf-mcp-item">
      <input type="checkbox" data-mcp-srv="${s.name}" ${selectedUrls.has(s.url) ? "checked" : ""} />
      <span>${s.name}</span>
      <code>${s.url}</code>
    </label>
  `).join("");
}

async function saveProfile() {
  const name = el("pf-name").value.trim();
  if (!name) { print(el("pf-status"), "Enter a profile name", true); return; }

  const selectedMcpServers = [...document.querySelectorAll("#pf-mcp-list input[data-mcp-srv]:checked")]
    .map((cb) => cb.getAttribute("data-mcp-srv"))
    .map((n) => mcpServerData[n])
    .filter(Boolean)
    .map((s) => ({ name: s.name, url: s.url }));

  // Preserve memory fields this panel doesn't expose (top_k, extractor_model, embed_model)
  // by spreading the previously loaded profile's memory config, then overriding what we edit.
  const existingMemory = profileData[name]?.memory || {};

  const payload = {
    name,
    nickname: el("pf-nickname").value.trim(),
    llm: {
      base_url: el("pf-llm-url").value.trim(),
      api_key: el("pf-llm-key").value,
      model: el("pf-llm-model").value.trim(),
    },
    system_prompt: el("pf-system-prompt").value,
    tts: {
      engine: el("pf-tts-engine")?.value || "",
      voice: el("pf-tts-voice")?.value || "",
    },
    mcp_servers: selectedMcpServers,
    memory: {
      ...existingMemory,
      enabled: el("pf-mem-enabled").checked,
      mode: el("pf-mem-mode").value,
    },
  };

  print(el("pf-status"), "Saving…");
  try {
    const isNew = profileEditMode === "new";
    const url = isNew ? "/v1/profiles" : `/v1/profiles/${encodeURIComponent(name)}`;
    const resp = await fetch(url, {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(el("pf-status"), body.detail || body.error || JSON.stringify(body), true);
      return;
    }
    el("pf-status").textContent = "Saved ✓";
    el("pf-llm-key").value = "";
    await loadProfiles();
    el("profile-select").value = name;
    if (isNew) { profileEditMode = name; el("pf-name").disabled = true; el("pf-delete-btn").classList.remove("hidden"); }
  } catch (error) {
    print(el("pf-status"), String(error), true);
  }
}

async function deleteProfile() {
  const name = el("pf-name").value.trim();
  if (!name || !confirm(`Delete profile "${name}"?`)) return;
  try {
    const resp = await fetch(`/v1/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) { const b = await resp.json(); print(el("pf-status"), b.detail || "Delete failed", true); return; }
    await loadProfiles();
    el("profile-select").value = "";
    closeProfilePanel();
  } catch (error) {
    print(el("pf-status"), String(error), true);
  }
}

// ============================================================ profile memory
async function loadMemories(name) {
  const list = el("pf-mem-list");
  if (!list) return;
  list.innerHTML = "";
  if (!name) return;
  try {
    const body = await (await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories`)).json();
    for (const m of body.data || []) list.appendChild(memRow(name, m));
    if (!(body.data || []).length) list.innerHTML = '<p class="hint">No memories yet.</p>';
  } catch (e) {
    list.innerHTML = '<p class="hint">Failed to load memories.</p>';
  }
}

function memRow(name, m) {
  const row = document.createElement("div");
  row.className = "pf-mem-item";
  const text = document.createElement("span");
  text.className = "mem-text";
  text.textContent = m.content;
  const edit = document.createElement("button");
  edit.className = "ghost mini";
  edit.type = "button";
  edit.textContent = "✎";
  edit.addEventListener("click", async () => {
    const next = prompt("Edit memory:", m.content);
    if (next === null || !next.trim()) return;
    try {
      const resp = await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories/${m.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: next.trim() }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        print(el("pf-status"), "Memory update failed: " + (body.detail || body.error || resp.statusText), true);
        return;
      }
      loadMemories(name);
    } catch (error) {
      print(el("pf-status"), "Memory update failed: " + error, true);
    }
  });
  const del = document.createElement("button");
  del.className = "ghost mini";
  del.type = "button";
  del.textContent = "✕";
  del.addEventListener("click", async () => {
    try {
      const resp = await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories/${m.id}`, { method: "DELETE" });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        print(el("pf-status"), "Memory delete failed: " + (body.detail || body.error || resp.statusText), true);
        return;
      }
      loadMemories(name);
    } catch (error) {
      print(el("pf-status"), "Memory delete failed: " + error, true);
    }
  });
  row.append(text, edit, del);
  return row;
}

if (el("pf-mem-add")) {
  el("pf-mem-add").addEventListener("click", async () => {
    const name = el("pf-name").value.trim();
    const content = el("pf-mem-new").value.trim();
    if (!name || !content) return;
    try {
      const resp = await fetch(`/v1/profiles/${encodeURIComponent(name)}/memories`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        print(el("pf-status"), "Memory add failed: " + (body.detail || body.error || resp.statusText), true);
        return;
      }
      el("pf-mem-new").value = "";
      loadMemories(name);
    } catch (error) {
      print(el("pf-status"), "Memory add failed: " + error, true);
    }
  });
}

// Profile bar event listeners
if (el("profile-edit-btn")) {
  el("profile-edit-btn").addEventListener("click", () => {
    const name = el("profile-select").value;
    if (!name) { alert("Select a profile first, or click + New to create one."); return; }
    openProfilePanel("edit", name);
  });
}
if (el("profile-new-btn")) el("profile-new-btn").addEventListener("click", () => openProfilePanel("new"));
if (el("profile-close-btn")) el("profile-close-btn").addEventListener("click", closeProfilePanel);
if (el("pf-cancel-btn")) el("pf-cancel-btn").addEventListener("click", closeProfilePanel);
if (el("pf-save-btn")) el("pf-save-btn").addEventListener("click", saveProfile);
if (el("pf-delete-btn")) el("pf-delete-btn").addEventListener("click", deleteProfile);
if (el("pf-tts-engine")) el("pf-tts-engine").addEventListener("change", pfUpdateTtsVoice);
if (el("profile-select")) {
  el("profile-select").addEventListener("change", () => {
    currentSessionId = null;
    const dialogue = el("chat-dialogue");
    if (dialogue) dialogue.innerHTML = "";
  });
}

// ============================================================ sessions panel
async function openSessionsPanel() {
  const panel = el("session-panel");
  const list = el("session-list");
  if (!panel || !list) return;
  panel.classList.remove("hidden");
  list.innerHTML = '<p class="hint">Loading&#8230;</p>';
  const profile = el("profile-select")?.value || "";
  const url = profile ? `/v1/sessions?profile=${encodeURIComponent(profile)}` : "/v1/sessions";
  try {
    const body = await (await fetch(url)).json();
    list.innerHTML = "";
    for (const s of body.data || []) {
      const row = document.createElement("div");
      row.className = "session-row";
      const t = document.createElement("span");
      t.className = "sess-time";
      t.textContent = (s.created_at || "").slice(0, 16).replace("T", " ");
      const p = document.createElement("span");
      p.className = "sess-preview";
      p.textContent = s.preview || "(empty)";
      row.append(t, p);
      row.addEventListener("click", () => loadSession(s.id));
      list.appendChild(row);
    }
    if (!(body.data || []).length) list.innerHTML = '<p class="hint">No sessions yet.</p>';
  } catch (e) {
    list.innerHTML = '<p class="hint">Failed to load sessions.</p>';
  }
}

async function loadSession(id) {
  let body;
  try {
    const resp = await fetch(`/v1/sessions/${id}`);
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}));
      const list = el("session-list");
      if (list) list.insertAdjacentHTML("afterbegin", `<p class="hint error">Failed to load session: ${errBody.detail || errBody.error || resp.statusText}</p>`);
      return;
    }
    body = await resp.json();
  } catch (error) {
    const list = el("session-list");
    if (list) list.insertAdjacentHTML("afterbegin", `<p class="hint error">Failed to load session: ${error}</p>`);
    return;
  }
  // Only mutate chat state/DOM after the fetch has succeeded.
  currentSessionId = id;
  const dlg = el("chat-dialogue");
  dlg.innerHTML = "";
  chat.history = [];
  for (const m of body.data.messages || []) {
    if (m.role !== "user" && m.role !== "assistant") continue;
    addBubble(m.role, m.content);
    chat.history.push({ role: m.role, content: m.content });
  }
  el("session-panel").classList.add("hidden");
}

if (el("session-list-btn")) el("session-list-btn").addEventListener("click", openSessionsPanel);
if (el("session-close-btn")) el("session-close-btn").addEventListener("click", () => el("session-panel").classList.add("hidden"));
if (el("session-new-btn")) {
  el("session-new-btn").addEventListener("click", () => {
    currentSessionId = null;
    chat.history = [];
    const dialogue = el("chat-dialogue");
    if (dialogue) dialogue.innerHTML = "";
  });
}

// ============================================================ MCP servers
async function loadMcpServers() {
  try {
    const body = await (await fetch("/v1/mcp/servers")).json();
    mcpServerData = body.data || {};
    renderMcpList();
  } catch {
    /* ignore */
  }
}

function renderMcpList() {
  const host = el("mcp-server-list");
  if (!host) return;
  const servers = Object.values(mcpServerData);
  if (!servers.length) {
    host.innerHTML = '<p class="hint">No servers configured yet. Add one below.</p>';
    return;
  }
  host.innerHTML = servers.map((s) => `
    <div class="model-row">
      <div class="model-info">
        <strong>${s.name}</strong>
        <code>${s.url}</code>
        <span class="mcp-row-status" id="mcp-test-${s.name}"></span>
      </div>
      <div class="model-action">
        <button class="mini" data-mcp-test="${s.name}">Test</button>
        <button class="mini danger" data-mcp-delete="${s.name}">Delete</button>
      </div>
    </div>
  `).join("");

  document.querySelectorAll("[data-mcp-test]").forEach((btn) =>
    btn.addEventListener("click", () => testMcpServer(btn.getAttribute("data-mcp-test")))
  );
  document.querySelectorAll("[data-mcp-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteMcpServer(btn.getAttribute("data-mcp-delete")))
  );
}

async function addMcpServer() {
  const name = el("mcp-add-name").value.trim();
  const url = el("mcp-add-url").value.trim();
  const status = el("mcp-status");
  if (!name || !url) { print(status, "Enter both name and URL", true); return; }
  try {
    const resp = await fetch("/v1/mcp/servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, url }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    status.textContent = `Added "${name}" ✓`;
    el("mcp-add-name").value = "";
    el("mcp-add-url").value = "";
    await loadMcpServers();
    // Refresh profile panel MCP list if open
    if (el("profile-panel") && !el("profile-panel").classList.contains("hidden")) {
      const pName = el("pf-name").value;
      renderProfileMcpList(pName && profileData[pName] ? profileData[pName].mcp_servers : []);
    }
  } catch (error) {
    print(status, String(error), true);
  }
}

async function testMcpServer(name) {
  const statusEl = el(`mcp-test-${name}`);
  if (statusEl) { statusEl.textContent = "testing…"; statusEl.className = "mcp-row-status"; }
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}/tools`);
    const body = await resp.json();
    if (!resp.ok) {
      if (statusEl) { statusEl.textContent = `✗ ${body.detail || "error"}`; statusEl.className = "mcp-row-status err"; }
      return;
    }
    const tools = body.data.tools || [];
    if (statusEl) { statusEl.textContent = `✓ ${tools.length} tool${tools.length !== 1 ? "s" : ""}`; statusEl.className = "mcp-row-status ok"; }
  } catch (error) {
    if (statusEl) { statusEl.textContent = `✗ ${error}`; statusEl.className = "mcp-row-status err"; }
  }
}

async function deleteMcpServer(name) {
  if (!confirm(`Delete MCP server "${name}"?`)) return;
  try {
    const resp = await fetch(`/v1/mcp/servers/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) { const b = await resp.json(); print(el("mcp-status"), b.detail || "Delete failed", true); return; }
    await loadMcpServers();
  } catch (error) {
    print(el("mcp-status"), String(error), true);
  }
}

if (el("mcp-add-btn")) el("mcp-add-btn").addEventListener("click", addMcpServer);
if (el("mcp-refresh")) el("mcp-refresh").addEventListener("click", loadMcpServers);

// ============================================================ voice→text (in chat section)
function setV2tUI(state) {
  const start = el("v2t-start");
  const stop = el("v2t-stop");
  if (start) start.disabled = state !== "idle";
  if (stop) stop.disabled = state !== "recording";
}

function setV2tStatus(text, cls) {
  const e = el("v2t-status");
  if (e) { e.textContent = text; e.className = cls; }
}

async function startV2t() {
  const engine = el("v2t-stt-engine")?.value || "vosk";
  const language = el("v2t-language")?.value.trim() || "";
  if (el("v2t-partial")) el("v2t-partial").textContent = "—";
  if (el("v2t-log")) el("v2t-log").textContent = "";

  const pp = getPreproc();
  let params = `engine=${encodeURIComponent(engine)}&sample_rate=${STREAM_SAMPLE_RATE}`;
  if (language) params += `&language=${encodeURIComponent(language)}`;
  params += `&denoise=${pp.denoise}&vad=${pp.vad}&vad_backend=${encodeURIComponent(pp.backend)}`;

  let capture;
  try {
    capture = createMicCapture({
      onframe: (pcm) => {
        if (v2t.ws && v2t.ws.readyState === WebSocket.OPEN) v2t.ws.send(pcm.buffer);
      },
    });
  } catch {
    setV2tStatus("mic error", "status-error");
    return;
  }

  const ws = new WebSocket(wsUrl(`/v1/stt/stream?${params}`));
  v2t.ws = ws;

  ws.onopen = async () => {
    try {
      await capture.start();
      v2t.capture = capture;
      setV2tStatus("● recording", "status-rec");
      setV2tUI("recording");
    } catch {
      setV2tStatus("mic denied", "status-error");
      ws.close();
    }
  };

  ws.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (el("v2t-log")) {
      const lines = el("v2t-log").textContent.split("\n");
      lines.push(`${data.event_type}: ${JSON.stringify(data.payload ?? {})}`);
      if (lines.length > 30) lines.shift();
      el("v2t-log").textContent = lines.join("\n");
    }
    if (data.event_type === "partial") {
      if (el("v2t-partial")) el("v2t-partial").textContent = data.payload?.text || "…";
    } else if (data.event_type === "final") {
      const text = (data.payload?.text || "").trim();
      if (text) chatBubble("user", text);
      if (el("v2t-partial")) el("v2t-partial").textContent = "—";
    } else if (data.event_type === "done") {
      stopV2t();
    }
  };

  ws.onerror = () => setV2tStatus("ws error", "status-error");
  ws.onclose = () => {
    setV2tUI("idle");
    if (el("v2t-status")?.className !== "status-error") setV2tStatus("idle", "status-idle");
  };
}

function stopV2t() {
  if (v2t.capture) { v2t.capture.stop(); v2t.capture = null; }
  if (v2t.ws) {
    if (v2t.ws.readyState === WebSocket.OPEN) v2t.ws.send(JSON.stringify({ type: "end" }));
    v2t.ws = null;
  }
}

if (el("v2t-start")) el("v2t-start").addEventListener("click", startV2t);
if (el("v2t-stop")) el("v2t-stop").addEventListener("click", stopV2t);
setV2tUI("idle");

// ============================================================ text→voice (in chat section)
if (el("t2v-submit")) {
  el("t2v-submit").addEventListener("click", async () => {
    const text = el("t2v-text")?.value.trim();
    const audio = el("t2v-audio");
    const meta = el("t2v-meta");
    if (!text) return;
    if (meta) meta.textContent = "Synthesizing…";
    if (audio) audio.classList.add("hidden");
    try {
      const resp = await fetch("/v1/tts/synthesize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          engine: el("t2v-tts-engine")?.value || undefined,
          voice: el("t2v-tts-voice")?.value || null,
        }),
      });
      const body = await resp.json();
      if (!resp.ok) { if (meta) meta.textContent = body.error || "TTS error"; return; }
      if (body.data?.audio_url && audio) {
        audio.src = body.data.audio_url;
        audio.classList.remove("hidden");
        audio.play().catch(() => {});
        if (meta) meta.textContent = `${body.data.duration_seconds ?? "?"}s @ ${body.data.sample_rate}Hz`;
      }
    } catch (error) {
      if (meta) meta.textContent = String(error);
    }
  });
}
