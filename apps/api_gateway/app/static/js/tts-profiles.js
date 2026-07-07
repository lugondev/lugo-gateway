import { el, print, restoreAndBind } from "./helpers.js";
import { renderProfileTtsSelect } from "./profiles.js";

export let ttsProfileData = {};
export let ttsProfileEditName = null; // null = "new" (no profile currently loaded into the form)

export async function loadTtsProfiles() {
  try {
    const body = await (await fetch("/v1/tts/profiles")).json();
    ttsProfileData = body.data || {};
    renderTtsProfileList();
    renderProfileTtsSelect();
    renderConvTtsProfileSelect();
    renderLivehostTtsProfileSelect();
  } catch {
    /* ignore */
  }
}

export function renderConvTtsProfileSelect() {
  const sel = el("conv-tts-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">(server default)</option>';
  Object.keys(ttsProfileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (ttsProfileData[prev]) sel.value = prev;
  restoreAndBind("conv-tts-profile");
}

export function renderLivehostTtsProfileSelect() {
  const sel = el("lh-tts-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">(server default)</option>';
  Object.keys(ttsProfileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (ttsProfileData[prev]) sel.value = prev;
  restoreAndBind("lh-tts-profile");
}

export function renderTtsProfileList() {
  const host = el("tts-profile-list");
  if (!host) return;
  const names = Object.keys(ttsProfileData).sort();
  if (!names.length) {
    host.innerHTML = '<p class="hint">No TTS profiles yet. Create one below.</p>';
    return;
  }
  host.innerHTML = names.map((name) => {
    const p = ttsProfileData[name];
    const voiceSummary = p.voice_mode === "clone" ? "cloned voice" : (p.voice || "auto voice");
    return `
      <div class="model-row">
        <div class="model-info">
          <strong>${name}</strong>
          <code>${p.engine || "(no engine)"}</code>
          <span class="hint">${voiceSummary}</span>
        </div>
        <div class="model-action">
          <button class="mini" data-tp-edit="${name}">Edit</button>
          <button class="mini danger" data-tp-delete="${name}">Delete</button>
        </div>
      </div>
    `;
  }).join("");

  document.querySelectorAll("[data-tp-edit]").forEach((btn) =>
    btn.addEventListener("click", () => openTtsProfileForm(btn.getAttribute("data-tp-edit")))
  );
  document.querySelectorAll("[data-tp-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteTtsProfile(btn.getAttribute("data-tp-delete")))
  );
}

export function toggleTtsVoiceMode() {
  const isClone = el("tp-mode-clone")?.checked;
  const presetWrap = el("tp-preset-wrap");
  const cloneWrap = el("tp-clone-wrap");
  if (presetWrap) presetWrap.classList.toggle("hidden", !!isClone);
  if (cloneWrap) cloneWrap.classList.toggle("hidden", !isClone);
}

export async function loadTtsProfileVoiceOptions(engine) {
  const sel = el("tp-voice");
  if (!sel) return;
  sel.innerHTML = '<option value="">(auto)</option>';
  if (!engine) return;
  try {
    const body = await (await fetch(`/v1/tts/voices?engine=${encodeURIComponent(engine)}`)).json();
    (body.data || []).forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.voice;
      opt.textContent = v.label;
      sel.appendChild(opt);
    });
  } catch {
    /* voices optional */
  }
}

export function openTtsProfileForm(name) {
  ttsProfileEditName = name || null;
  el("tp-form-title").textContent = name ? `Edit "${name}"` : "New TTS Profile";
  const p = name ? ttsProfileData[name] : null;

  el("tp-name").value = name || "";
  el("tp-name").disabled = !!name;
  el("tp-engine").value = p?.engine || "";
  const isClone = p?.voice_mode === "clone";
  el("tp-mode-preset").checked = !isClone;
  el("tp-mode-clone").checked = isClone;
  toggleTtsVoiceMode();
  loadTtsProfileVoiceOptions(p?.engine || "").then(() => {
    if (p?.voice) el("tp-voice").value = p.voice;
  });
  el("tp-ref-audio").value = p?.ref_audio_path || "";
  el("tp-ref-text").value = p?.ref_text || "";
  el("tp-instruct").value = p?.instruct || "";
  el("tp-speed").value = p?.speed ?? "";
  el("tp-language").value = p?.language || "";
  el("tp-delete-btn").classList.toggle("hidden", !name);
  el("tp-status").textContent = "";
}

export function resetTtsProfileForm() {
  openTtsProfileForm(null);
}

export async function saveTtsProfile() {
  const name = el("tp-name").value.trim();
  if (!name) { print(el("tp-status"), "Enter a profile name", true); return; }

  const speedRaw = el("tp-speed").value.trim();
  const payload = {
    name,
    engine: el("tp-engine").value || "",
    voice_mode: el("tp-mode-clone").checked ? "clone" : "preset",
    voice: el("tp-voice").value || "",
    ref_audio_path: el("tp-ref-audio").value.trim(),
    ref_text: el("tp-ref-text").value.trim(),
    instruct: el("tp-instruct").value.trim(),
    speed: speedRaw ? parseFloat(speedRaw) : null,
    language: el("tp-language").value.trim() || null,
  };

  print(el("tp-status"), "Saving…");
  try {
    const isNew = !ttsProfileEditName;
    const url = isNew ? "/v1/tts/profiles" : `/v1/tts/profiles/${encodeURIComponent(ttsProfileEditName)}`;
    const resp = await fetch(url, {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok) {
      print(el("tp-status"), body.detail || body.error || JSON.stringify(body), true);
      return;
    }
    el("tp-status").textContent = "Saved ✓";
    await loadTtsProfiles();
    openTtsProfileForm(name);
  } catch (error) {
    print(el("tp-status"), String(error), true);
  }
}

export async function deleteTtsProfile(name) {
  if (!confirm(`Delete TTS profile "${name}"?`)) return;
  try {
    const resp = await fetch(`/v1/tts/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!resp.ok) { const b = await resp.json(); print(el("tp-status"), b.detail || "Delete failed", true); return; }
    await loadTtsProfiles();
    if (ttsProfileEditName === name) resetTtsProfileForm();
  } catch (error) {
    print(el("tp-status"), String(error), true);
  }
}

if (el("tp-engine")) {
  el("tp-engine").addEventListener("change", (e) => loadTtsProfileVoiceOptions(e.target.value));
}
if (el("tp-mode-preset")) el("tp-mode-preset").addEventListener("change", toggleTtsVoiceMode);
if (el("tp-mode-clone")) el("tp-mode-clone").addEventListener("change", toggleTtsVoiceMode);
if (el("tp-save-btn")) el("tp-save-btn").addEventListener("click", saveTtsProfile);
if (el("tp-cancel-btn")) el("tp-cancel-btn").addEventListener("click", resetTtsProfileForm);
if (el("tp-delete-btn")) el("tp-delete-btn").addEventListener("click", () => deleteTtsProfile(ttsProfileEditName));
