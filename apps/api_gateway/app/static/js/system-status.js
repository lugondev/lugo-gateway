import { el, fmtBytes, setBadge } from "./helpers.js";

export async function loadSystemStatus() {
  const host = el("system-status");
  try {
    const body = await (await fetch("/v1/system/status")).json();
    const d = body.data;
    const tile = (label, value, ok) =>
      `<div class="stat ${ok === undefined ? "" : ok ? "ok" : "warn"}"><span>${label}</span><strong>${value}</strong></div>`;

    const sttOk = (d.stt_engines || []).some((e) => e.available);
    const ttsOk = (d.tts_engines || []).some((e) => e.available);

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
  } catch (error) {
    host.innerHTML = `<div class="stat warn"><span>status</span><strong>error</strong></div>`;
  }
}

