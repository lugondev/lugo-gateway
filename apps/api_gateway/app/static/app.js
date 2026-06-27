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

    // ---- OmniVoice (TTS) ----
    const omni = body.data.omnivoice;
    el("omnivoice-models").innerHTML = omni.models.map(renderOmniRow).join("");

    // ---- VieNeu (TTS) ----
    const vieneu = body.data.vieneu;
    el("vieneu-models").innerHTML = vieneu.modes.map(renderVieneuRow).join("");

    // ---- Conversation LLM (Ollama) ----
    const llm = body.data.llm;
    if (el("llm-hint")) {
      el("llm-hint").textContent = llm.available
        ? `Ollama at ${llm.base_url} — active: ${llm.active} ${llm.running ? "(running)" : "(idle)"}`
        : "Ollama not reachable. Install & run Ollama, then set CONVERSATION_LLM_BASE_URL=http://localhost:11434/v1.";
    }
    const llmBtn = el("llm-start");
    if (llmBtn) {
      llmBtn.dataset.mode = llm.available ? "stop" : "start";
      llmBtn.textContent = llm.available ? "Stop service" : "Start service";
    }
    const llmNames = new Set(llm.suggestions.map((s) => s.model));
    const llmRows = llm.suggestions.map((s) => renderLlmRow(s, llm.jobs));
    llm.installed
      .filter((m) => !llmNames.has(m.model))
      .forEach((m) => llmRows.push(renderLlmRow({ ...m, installed: true }, llm.jobs)));
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

function renderOmniRow(m) {
  let action;
  if (m.job && m.job.state === "downloading") action = SPINNER_ACTION;
  else if (m.cached) action = `${useOrActive(m.active, "o-select", m.id)}<button class="mini danger" data-o-delete="${m.id}">Delete</button>`;
  else action = dlBtn("o-download", m.id, m.job);
  return modelRow({ title: m.label, code: m.id, size: m.cached ? fmtBytes(m.size_bytes) : "", err: jobErr(m.job), action });
}

function renderVieneuRow(m) {
  let action;
  if (m.job && m.job.state === "downloading") action = SPINNER_ACTION;
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
};

const MODEL_ATTRS = [
  ["data-download", "vosk", "download"], ["data-delete", "vosk", "delete"], ["data-v-select", "vosk", "select"],
  ["data-w-download", "whisper", "download"], ["data-w-delete", "whisper", "delete"], ["data-w-select", "whisper", "select"],
  ["data-o-download", "omnivoice", "download"], ["data-o-delete", "omnivoice", "delete"], ["data-o-select", "omnivoice", "select"],
  ["data-vn-download", "vieneu", "download"], ["data-vn-delete", "vieneu", "delete"], ["data-vn-select", "vieneu", "select"],
  ["data-llm-download", "llm", "download"], ["data-llm-delete", "llm", "delete"], ["data-llm-select", "llm", "select"],
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

function renderLlmRow(m, jobs) {
  const job = jobs[m.model];
  let action;
  if (job && job.state === "downloading") {
    const pct = Math.round((job.progress || 0) * 100);
    action = `<div class="progress"><div class="bar" style="width:${pct}%"></div></div><span class="pct">${pct}%</span>`;
  } else if (m.installed) {
    action = `${useOrActive(m.active, "llm-select", m.model)}<button class="mini danger" data-llm-delete="${m.model}">Delete</button>`;
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

    [["tts-engine", "tts-engine-detail"], ["tts-stream-engine", "tts-stream-engine-detail"]].forEach(
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
const conv = { ws: null, capture: null, log: [], ctx: null, nextTime: 0, sources: [], chain: null, assistantBubble: null };

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
  el("conv-dialogue").appendChild(div);
  el("conv-dialogue").scrollTop = el("conv-dialogue").scrollHeight;
  return div;
}
function convAudioCtx() {
  if (!conv.ctx) conv.ctx = new (window.AudioContext || window.webkitAudioContext)();
  return conv.ctx;
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
}
// Gapless playback: decode each chunk and schedule it back-to-back on the audio
// timeline (no <audio> src-swap gaps, no queue underrun between sentences).
function convSchedule(buf) {
  const ctx = conv.ctx;
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
// Audio arrives as raw WAV bytes over the WS (no per-chunk HTTP fetch).
function convEnqueueAudioBytes(arrayBuffer) {
  conv.chain = (conv.chain || Promise.resolve())
    .then(async () => {
      const ctx = convAudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      convSchedule(await ctx.decodeAudioData(arrayBuffer));
    })
    .catch((e) => convLog("audio error: " + e));
}

async function loadConversationEngines() {
  try {
    const [stt, tts] = await Promise.all([
      (await fetch("/v1/stt/engines")).json(),
      (await fetch("/v1/tts/engines")).json(),
    ]);
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
    // Prefer whisper for STT and VieNeu for TTS (OmniVoice CLI is too slow live).
    const sttSel = el("conv-stt-engine");
    if (sttSel && [...sttSel.options].some((o) => o.value === "whisper")) sttSel.value = "whisper";
    const ttsSel = el("conv-tts-engine");
    if (ttsSel && [...ttsSel.options].some((o) => o.value === "vieneu")) ttsSel.value = "vieneu";
    restoreAndBind("conv-stt-engine");
    restoreAndBind("conv-tts-engine");
    restoreAndBind("conv-language");
    el("conv-tts-engine").addEventListener("change", convVoiceToggle);
    convVoiceToggle();
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
  el("conv-dialogue").innerHTML = "";
  conv.log = [];
  el("conv-log").textContent = "";

  let params = `stt_engine=${encodeURIComponent(el("conv-stt-engine").value)}`;
  params += `&tts_engine=${encodeURIComponent(el("conv-tts-engine").value)}&sample_rate=${STREAM_SAMPLE_RATE}`;
  if (el("conv-voice").value) params += `&voice=${encodeURIComponent(el("conv-voice").value)}`;
  if (el("conv-language").value.trim()) params += `&language=${encodeURIComponent(el("conv-language").value.trim())}`;
  const cpp = getPreproc();
  params += `&denoise=${cpp.denoise}&vad=${cpp.vad}&vad_backend=${encodeURIComponent(cpp.backend)}`;

  let capture;
  try {
    capture = createMicCapture({
      onframe: (pcm) => {
        if (conv.ws && conv.ws.readyState === WebSocket.OPEN) conv.ws.send(pcm.buffer);
      },
    });
  } catch (error) {
    setConvStatus("mic error", "status-error");
    setConvUI("idle");
    return;
  }

  const ws = new WebSocket(wsUrl(`/v1/conversation/stream?${params}`));
  ws.binaryType = "arraybuffer";
  conv.ws = ws;

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
    if (typeof event.data !== "string") {
      convEnqueueAudioBytes(event.data); // raw WAV frame -> play gaplessly
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
          el("conv-dialogue").scrollTop = el("conv-dialogue").scrollHeight;
        }
        break;
      case "audio_chunk":
        // Audio for this chunk arrives as the next binary WS frame.
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
  el("conv-dialogue").innerHTML = "";
  if (conv.ws && conv.ws.readyState === WebSocket.OPEN) conv.ws.send(JSON.stringify({ type: "reset" }));
});

// ============================================================ LLM text chat
const chat = { history: [], busy: false };

function chatBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  el("chat-dialogue").appendChild(div);
  el("chat-dialogue").scrollTop = el("chat-dialogue").scrollHeight;
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
    const resp = await fetch("/v1/conversation/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chat.history }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      pending.textContent = `error: ${body.error || JSON.stringify(body)}`;
      pending.classList.add("error");
    } else {
      pending.textContent = body.data.reply;
      chat.history.push({ role: "assistant", content: body.data.reply });
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
  el("chat-dialogue").innerHTML = "";
});

// ============================================================ tabs
function initTabs() {
  const buttons = document.querySelectorAll(".tab-btn");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-tab");
      buttons.forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === `tab-${target}`);
      });
    });
  });
}

// ============================================================ init
initTabs();
initSttMode();
setStreamUI("idle");
setConvUI("idle");
// Persist + restore free-text controls (selects/checkboxes are bound after they populate).
["stt-language", "stt-stream-language"].forEach(restoreAndBind);
loadSttEngines();
loadTtsEngines();
loadConversationEngines();
loadSystemStatus();
loadModels();
