import { el, escapeHtml } from "./helpers.js";
import { fetchAuthStatus } from "./session.js";

function _tile(label, value, ok) {
  const cls = ok === undefined ? "" : ok ? "ok" : "warn";
  return `<div class="stat ${cls}"><span>${label}</span><strong>${value}</strong></div>`;
}

async function _loadOverview() {
  const host = el("home-overview");
  if (!host) return;
  try {
    const body = await (await fetch("/v1/stats/home")).json();
    if (!body.success) throw new Error("failed to load stats");
    const d = body.data;
    host.innerHTML =
      _tile("Profiles", d.profiles.count) +
      _tile("Devices", `${d.devices.count} (${d.devices.active_recent} active recently)`) +
      _tile("Sessions", d.sessions.count);
  } catch (error) {
    host.innerHTML = _tile("Overview", "error", false);
  }
}

function _renderLimits(limits) {
  if (!limits || !limits.length) return "";
  const parts = limits.map((l) => {
    const spent = Number(l.spend_usd || 0);
    const limit = Number(l.limit_usd || 0);
    const over = limit > 0 && spent >= limit;
    const label = l.scope === "global" ? "Shared limit" : "Your limit";
    return `<li class="${over ? "danger" : ""}">${label} (${escapeHtml(String(l.period))}): $${spent.toFixed(4)} of $${limit.toFixed(2)}${over ? " - reached" : ""}</li>`;
  });
  return `<ul class="limit-list">${parts.join("")}</ul>`;
}

async function _loadUsageForUser() {
  const host = el("home-usage");
  if (!host) return;
  try {
    const body = await (await fetch("/v1/usage/me")).json();
    if (!body.success) throw new Error("failed to load usage");
    const rows = body.data || [];
    const requests = rows.reduce((s, r) => s + Number(r.count || 0), 0);
    const cost = rows.reduce((s, r) => s + Number(r.cost_usd || 0), 0);
    host.innerHTML =
      _tile("Requests (all time)", requests) +
      _tile("Cost (all time)", `$${cost.toFixed(4)}`) +
      _renderLimits(body.limits);
  } catch (error) {
    host.innerHTML = _tile("Usage", "error", false);
  }
}

export async function loadHome() {
  await Promise.all([_loadOverview(), _loadUsageForAdminOrUser()]);
}

async function _loadUsageForAdminOrUser() {
  let status;
  try {
    status = await fetchAuthStatus();
  } catch (error) {
    const host = el("home-usage");
    if (host) host.innerHTML = _tile("Usage", "error", false);
    return;
  }
  const isAdmin = status.authenticated && status.role === "admin";
  if (!isAdmin) {
    await _loadUsageForUser();
    return;
  }
  await Promise.all([_loadUsageForAdmin(), _loadSystemAndModels(), _loadRegistrySummary()]);
}

async function _loadUsageForAdmin() {
  const host = el("home-usage");
  if (!host) return;
  try {
    const [summaryBody, quotasBody] = await Promise.all([
      (await fetch("/v1/usage/summary?group_by=kind")).json(),
      (await fetch("/v1/quotas")).json(),
    ]);
    if (!summaryBody.success) throw new Error("failed to load usage");
    const rows = summaryBody.data || [];
    const requests = rows.reduce((s, r) => s + Number(r.count || 0), 0);
    const cost = rows.reduce((s, r) => s + Number(r.cost_usd || 0), 0);
    const quotas = quotasBody.success ? quotasBody.data || [] : [];
    host.innerHTML =
      _tile("Requests (all time)", requests) +
      _tile("Cost (all time)", `$${cost.toFixed(4)}`) +
      _renderQuotaLimits(quotas);
  } catch (error) {
    host.innerHTML = _tile("Usage", "error", false);
  }
}

function _renderQuotaLimits(quotas) {
  const enabled = (quotas || []).filter((q) => q.enabled);
  if (!enabled.length) return "";
  const parts = enabled.map((q) => {
    const spent = Number(q.spend_usd || 0);
    const limit = Number(q.limit_usd || 0);
    const over = limit > 0 && spent >= limit;
    const label = `${escapeHtml(q.scope)}${q.scope_id ? ` (${escapeHtml(q.scope_id)})` : ""}`;
    return `<li class="${over ? "danger" : ""}">${label} — ${escapeHtml(q.period)}: $${spent.toFixed(4)} of $${limit.toFixed(2)}${over ? " - reached" : ""}</li>`;
  });
  return `<ul class="limit-list">${parts.join("")}</ul>`;
}

async function _loadSystemAndModels() {
  const healthHost = el("home-health");
  const modelsHost = el("home-active-models");
  if (!healthHost || !modelsHost) return;
  try {
    const [statusBody, modelsBody] = await Promise.all([
      (await fetch("/v1/system/status")).json(),
      (await fetch("/v1/models")).json(),
    ]);
    const d = statusBody.data;
    const llm = modelsBody.data.llm;
    const sttOk = (d.stt_engines || []).some((e) => e.available);
    const ttsOk = (d.tts_engines || []).some((e) => e.available);

    healthHost.innerHTML =
      _tile("STT", sttOk ? "ready" : "not ready", sttOk) +
      _tile("TTS", ttsOk ? "ready" : "not ready", ttsOk) +
      _tile("LLM", llm.available ? "ready" : "not ready", llm.available);

    const whisperActive = d.whisper_local?.active_model || "(none)";
    const voskActive = d.vosk?.active_model_present ? "installed" : "not installed";
    modelsHost.innerHTML =
      _tile("Whisper (local STT)", escapeHtml(whisperActive)) +
      _tile("Vosk (local STT)", escapeHtml(voskActive)) +
      _tile("LLM active model", escapeHtml(llm.active || "(none)")) +
      _tile("LLM endpoint", llm.remote ? "Cloud API" : `Ollama (${llm.running ? "running" : "idle"})`);
  } catch (error) {
    healthHost.innerHTML = _tile("System health", "error", false);
    modelsHost.innerHTML = "";
  }
}

async function _loadRegistrySummary() {
  const host = el("home-registry");
  if (!host) return;
  try {
    const body = await (await fetch("/v1/model_registry")).json();
    if (!body.success) throw new Error("failed to load registry");
    const entries = body.data || [];
    const byKind = {};
    entries.forEach((e) => {
      byKind[e.kind] = (byKind[e.kind] || 0) + 1;
    });
    const kindTiles = Object.keys(byKind)
      .sort()
      .map((k) => _tile(k.toUpperCase(), byKind[k]))
      .join("");
    host.innerHTML = _tile("Total entries", entries.length) + kindTiles;
  } catch (error) {
    host.innerHTML = _tile("Model registry", "error", false);
  }
}

if (el("home-registry-link")) {
  el("home-registry-link").addEventListener("click", () => {
    document.querySelector('[data-section="model-registry"]')?.click();
  });
}
