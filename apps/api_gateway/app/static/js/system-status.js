import { el, fmtBytes, setBadge, restoreAndBind } from "./helpers.js";

export async function loadSystemStatus() {
  const host = el("system-status");
  try {
    const body = await (await fetch("/v1/system/status")).json();
    const d = body.data;
    const tile = (label, value, ok) =>
      `<div class="stat ${ok === undefined ? "" : ok ? "ok" : "warn"}"><span>${label}</span><strong>${value}</strong></div>`;

    const sttOk = (d.stt_engines || []).some((e) => e.available);
    const ttsOk = Boolean(d.tts.omnivoice_present);

    const groups = [
      {
        title: "Environment",
        tiles: [
          tile("Env", d.app.env),
          tile("Artifacts", `${d.artifacts.count} · ${fmtBytes(d.artifacts.total_bytes)}`),
        ],
      },
      {
        title: "TTS",
        tiles: [tile("Status", ttsOk ? "ready" : "not ready", ttsOk)],
      },
      {
        title: "STT",
        tiles: [tile("Status", sttOk ? "ready" : "not ready", sttOk)],
      },
    ];
    host.innerHTML = groups
      .map((g) => `<div class="status-group"><h3 class="sub">${g.title}</h3><div class="status-grid">${g.tiles.join("")}</div></div>`)
      .join("");

    // Update header/footer status badges
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
export let preprocessInit = false;

