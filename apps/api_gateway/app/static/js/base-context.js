import { el, print } from "./helpers.js";

export async function loadBaseContext() {
  try {
    const body = await (await fetch("/v1/system/config")).json();
    el("sys-base-context").value = body.data.base_context || "";
  } catch (error) {
    print(el("sys-base-context-status"), String(error), true);
  }
}

export async function saveBaseContext() {
  const status = el("sys-base-context-status");
  try {
    const resp = await fetch("/v1/system/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_context: el("sys-base-context").value }),
    });
    const body = await resp.json();
    if (!resp.ok) { print(status, body.detail || JSON.stringify(body), true); return; }
    status.classList.remove("error");
    status.textContent = "Saved ✓";
  } catch (error) {
    print(status, String(error), true);
  }
}
if (el("sys-base-context-save")) {
  el("sys-base-context-save").addEventListener("click", saveBaseContext);
  loadBaseContext();
}

// Shared STT preprocessing config (System tab) used by batch / streaming / conversation.
export function getPreproc() {
  return {
    denoise: el("pp-denoise") ? el("pp-denoise").checked : false,
    vad: el("pp-vad") ? el("pp-vad").checked : false,
    backend: el("pp-vad-backend") ? el("pp-vad-backend").value || "energy" : "energy",
  };
}

