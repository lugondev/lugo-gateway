# Livehost (TikTok Co-host) UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a browser UI for the existing TikTok co-host backend (`/v1/livehost/*`), which today has zero frontend — a new sidebar section mirroring the Voice→Voice conversation pane, with its own independent session/audio state.

**Architecture:** New sidebar section `section-livehost` + a new, standalone JS module `livehost.js` that mirrors `conversation.js`'s WS lifecycle / mic-capture / gapless-audio-playback pattern but does not import from or share state with `conversation.js`/`chat.js` (those two are already coupled to each other for the Chat tab's sub-modes; livehost is a separate session type on a separate WS endpoint). TikTok connect/disconnect are two extra REST calls layered on top of an already-open livehost WS session, with a client-side poll of `GET /{session_id}/status` since the backend doesn't push connection-state changes over the WS.

**Tech Stack:** Vanilla ES modules (no build step), same as the rest of `static/js/`.

## Global Constraints

- Do not modify `apps/api_gateway/app/api/routes/livehost.py` or any other backend file — this is UI-only, consuming the existing `/v1/livehost/*` API exactly as it stands (spec: `docs/superpowers/specs/2026-07-07-livehost-ui-design.md`).
- `livehost.js` must not import from `conversation.js` or `chat.js`, and must not touch `conv`/`chat`'s DOM ids or module state — separate WS session, separate everything.
- No JS test framework exists in this repo — verify with `node --check` on every new/touched JS file, plus a curl-based smoke check of the new HTML markup, mirroring how the TTS Profile UI work (`docs/superpowers/plans/2026-07-06-tts-profile.md`, Task 7) was verified.
- Disconnecting TikTok must never close the WS session (mic/voice keeps working) — matches the original backend spec's core constraint (`docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md`).
- The TikTok connect/username controls must stay disabled until the WS's `session_started` event has actually arrived (not just `ws.onopen`) — the backend registers the session in `livehost_registry` synchronously before sending `session_started`, so gating on that event (rather than the earlier `onopen`) avoids a `connect` call racing ahead of registration.

---

### Task 1: Livehost sidebar section, styling, and JS module

**Files:**
- Modify: `apps/api_gateway/app/static/index.html` (add nav item; add new `section-livehost` before `section-models`, i.e. right after the closing `</div>` of `section-tts`... no — insert as its own top-level section, placement shown in Step 1 below)
- Modify: `apps/api_gateway/app/static/styles.css` (one small addition: `.bubble.social`)
- Modify: `apps/api_gateway/app/static/js/tts-profiles.js` (add `renderLivehostTtsProfileSelect`, call it from `loadTtsProfiles()`)
- Create: `apps/api_gateway/app/static/js/livehost.js`
- Modify: `apps/api_gateway/app/static/js/main.js` (import + call `loadLivehostEngines()`)

**Interfaces:**
- Consumes: `ttsProfileData` (exported from `tts-profiles.js`, already populated by the existing `loadTtsProfiles()`); `el`, `wsUrl`, `restoreAndBind` from `helpers.js`; `STREAM_SAMPLE_RATE`, `createMicCapture` from `audio-capture.js`; `getPreproc` from `base-context.js`.
- Produces: `export async function loadLivehostEngines()` and `export const lh` from `livehost.js` — no other module needs to import from `livehost.js` in this plan, but the export shape follows `conversation.js`'s `loadConversationEngines`/`conv` naming convention for consistency.

This task has no pytest coverage (pure front-end, no backend changes). Verify with `node --check` on every touched/created `.js` file, plus a curl-based smoke check (start the dev server, fetch `/static/index.html`, confirm the new DOM ids are present and the old ones aren't duplicated), then a manual browser click-through is recommended before calling this done (same caveat as the TTS Profile UI work).

- [ ] **Step 1: Add the sidebar nav item**

In `apps/api_gateway/app/static/index.html`, find the nav list (it currently has `chat`, `stt`, `tts`, `models`, `mcp`, `system` in that order). Insert a new nav item right after `chat`'s `<li>` and before `stt`'s `<li>`:

```html
            <li>
              <button class="nav-item" data-section="livehost">
                <span class="nav-icon">◎</span>
                <span class="nav-label">Livehost</span>
              </button>
            </li>
```

So the surrounding markup reads:

```html
            <li>
              <button class="nav-item active" data-section="chat">
                <span class="nav-icon">◈</span>
                <span class="nav-label">Chat</span>
              </button>
            </li>
            <li>
              <button class="nav-item" data-section="livehost">
                <span class="nav-icon">◎</span>
                <span class="nav-label">Livehost</span>
              </button>
            </li>
            <li>
              <button class="nav-item" data-section="stt">
```

No JS changes needed for section-switching itself — `sidebar-nav.js`'s `activateSection()` already toggles `.section` visibility generically by matching `data-section` to `section-${section}`; it only special-cases `models` and `mcp` for an extra data-refresh call, which this new section doesn't need (its data loads once at boot like TTS profiles do).

- [ ] **Step 2: Add the `section-livehost` markup**

In `apps/api_gateway/app/static/index.html`, insert a new top-level section. Place it right after the closing `</div>` of `section-chat` (the div that starts at `<div class="section active" id="section-chat">`) and before the STT section's opening comment/div. The exact content:

```html
          <!-- ============================== LIVEHOST ============================== -->
          <div class="section" id="section-livehost">
            <section class="card">
              <div class="card-head">
                <h2>TikTok Co-host</h2>
              </div>
              <p class="hint">AI co-host for TikTok Live: answers viewer comments/gifts by voice and handles your own spoken input, same as Voice&#8594;Voice chat.</p>

              <div class="row">
                <label>
                  STT engine
                  <select id="lh-stt-engine"></select>
                </label>
                <label>
                  TTS Profile
                  <select id="lh-tts-profile">
                    <option value="">(server default)</option>
                  </select>
                </label>
              </div>
              <div class="row">
                <label>
                  Language (STT hint)
                  <input id="lh-language" type="text" value="vi" placeholder="vi, en, ja&#8230; (vi recommended)" />
                </label>
                <label class="check" title="Receive reply audio as streamed Opus frames decoded in-browser instead of fetching WAV files.">
                  <input type="checkbox" id="lh-opus" /> Opus downlink (low bandwidth)
                </label>
              </div>
              <div class="actions">
                <button id="lh-session-start">Start Session</button>
                <button id="lh-session-stop" class="ghost" disabled>Stop Session</button>
                <span id="lh-status" class="status-idle">idle</span>
              </div>

              <div class="row" style="margin-top:1rem">
                <label>
                  TikTok username
                  <input id="lh-tiktok-username" type="text" placeholder="unique_id (e.g. therock)" disabled />
                </label>
                <div class="actions end">
                  <button id="lh-tiktok-connect" disabled>Connect</button>
                  <button id="lh-tiktok-disconnect" class="ghost" disabled>Disconnect</button>
                </div>
              </div>
              <p class="hint">TikTok: <span id="lh-tiktok-status" class="status-idle">idle</span></p>
              <p id="lh-tiktok-error" class="meta error hidden"></p>

              <div id="lh-dialogue" class="dialogue"></div>
              <audio id="lh-audio" class="player hidden" controls></audio>
              <pre id="lh-log" class="output small"></pre>
            </section>
          </div>
```

- [ ] **Step 3: Add the `.bubble.social` style**

In `apps/api_gateway/app/static/styles.css`, right after the existing `.bubble.assistant` rule (around line 762-768), add:

```css
.bubble.social {
  align-self: center;
  max-width: 95%;
  background: transparent;
  border: 1px dashed var(--line);
  color: var(--muted);
  font-size: 12px;
  padding: 6px 10px;
}
```

- [ ] **Step 4: Add `renderLivehostTtsProfileSelect` to `tts-profiles.js`**

In `apps/api_gateway/app/static/js/tts-profiles.js`, change:

```js
export async function loadTtsProfiles() {
  try {
    const body = await (await fetch("/v1/tts/profiles")).json();
    ttsProfileData = body.data || {};
    renderTtsProfileList();
    renderProfileTtsSelect();
    renderConvTtsProfileSelect();
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
```

to:

```js
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
```

(This mirrors `renderConvTtsProfileSelect` exactly, just targeting `lh-tts-profile` instead of `conv-tts-profile` — both selects are populated from the same `ttsProfileData`.)

- [ ] **Step 5: Create `livehost.js`**

Create `apps/api_gateway/app/static/js/livehost.js`:

```js
import { el, wsUrl, restoreAndBind } from "./helpers.js";
import { STREAM_SAMPLE_RATE, createMicCapture } from "./audio-capture.js";
import { getPreproc } from "./base-context.js";

export const lh = {
  ws: null, capture: null, log: [], ctx: null, nextTime: 0, sources: [], chain: null,
  opusMode: false, opusDec: null, opusTs: 0, outRate: 24000,
  sessionId: null, statusPollTimer: null, assistantBubble: null, pendingReplyIsSocial: false,
};

const lhDetails = { stt: {} };

function setLhStatus(text, state) {
  const node = el("lh-status");
  node.textContent = text;
  node.className = state;
}
function lhLog(line) {
  lh.log.push(line);
  if (lh.log.length > 60) lh.log.shift();
  el("lh-log").textContent = lh.log.join("\n");
}
function lhAddBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  el("lh-dialogue").appendChild(div);
  el("lh-dialogue").scrollTop = el("lh-dialogue").scrollHeight;
  return div;
}
function lhAddFeedRow(text) {
  const div = document.createElement("div");
  div.className = "bubble social";
  div.textContent = text;
  el("lh-dialogue").appendChild(div);
  el("lh-dialogue").scrollTop = el("lh-dialogue").scrollHeight;
}
function lhAudioCtx() {
  if (!lh.ctx) lh.ctx = new (window.AudioContext || window.webkitAudioContext)();
  return lh.ctx;
}
function lhIsSpeaking() {
  return !!lh.ctx && (lh.nextTime || 0) > lh.ctx.currentTime + 0.15;
}
function lhStopAudio() {
  (lh.sources || []).forEach((s) => {
    try {
      s.stop();
    } catch {}
  });
  lh.sources = [];
  lh.nextTime = 0;
  lh.chain = Promise.resolve();
  lhResetOpus();
}
function lhEnqueueAudio(url) {
  lh.chain = (lh.chain || Promise.resolve())
    .then(async () => {
      const ctx = lhAudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      const data = await (await fetch(url)).arrayBuffer();
      const buf = await ctx.decodeAudioData(data);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      const start = Math.max(ctx.currentTime + 0.05, lh.nextTime || 0);
      src.start(start);
      lh.nextTime = start + buf.duration;
      lh.sources.push(src);
      src.onended = () => {
        lh.sources = lh.sources.filter((s) => s !== src);
      };
    })
    .catch((e) => lhLog("audio error: " + e));
}
function lhOpusSupported() {
  return typeof window.AudioDecoder === "function" && typeof window.EncodedAudioChunk === "function";
}
function lhScheduleBuffer(buf) {
  const ctx = lhAudioCtx();
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(ctx.destination);
  const start = Math.max(ctx.currentTime + 0.05, lh.nextTime || 0);
  src.start(start);
  lh.nextTime = start + buf.duration;
  lh.sources.push(src);
  src.onended = () => {
    lh.sources = lh.sources.filter((s) => s !== src);
  };
}
function lhInitOpusDecoder() {
  if (lh.opusDec) {
    try {
      lh.opusDec.close();
    } catch {}
  }
  lh.opusTs = 0;
  const ctx = lhAudioCtx();
  const dec = new AudioDecoder({
    output: (audioData) => {
      try {
        const frames = audioData.numberOfFrames;
        const buf = ctx.createBuffer(1, frames, audioData.sampleRate);
        const arr = new Float32Array(frames);
        audioData.copyTo(arr, { planeIndex: 0, format: "f32-planar" });
        buf.copyToChannel(arr, 0);
        lhScheduleBuffer(buf);
      } catch (e) {
        lhLog("opus output error: " + e);
      } finally {
        audioData.close();
      }
    },
    error: (e) => lhLog("opus decoder error: " + e),
  });
  dec.configure({ codec: "opus", sampleRate: lh.outRate, numberOfChannels: 1 });
  lh.opusDec = dec;
}
function lhFeedOpus(data) {
  if (!lh.opusDec || lh.opusDec.state === "closed") lhInitOpusDecoder();
  try {
    lh.opusDec.decode(new EncodedAudioChunk({ type: "key", timestamp: lh.opusTs, data }));
    lh.opusTs += 60000;
  } catch (e) {
    lhLog("opus feed error: " + e);
  }
}
function lhResetOpus() {
  if (lh.opusDec) {
    try {
      lh.opusDec.close();
    } catch {}
    lh.opusDec = null;
  }
  lh.opusTs = 0;
}

function setLhSessionUI(state) {
  el("lh-session-start").disabled = state !== "idle";
  el("lh-session-stop").disabled = state === "idle";
}
function setLhTiktokControlsEnabled(enabled) {
  el("lh-tiktok-username").disabled = !enabled;
  el("lh-tiktok-connect").disabled = !enabled;
}

function tiktokStatusLabel(state) {
  return (
    { idle: "idle", connecting: "connecting…", live: "live", reconnecting: "reconnecting…", offline_waiting: "offline, waiting…", error: "error" }[state] ||
    state
  );
}
function tiktokStatusClass(state) {
  if (state === "live") return "status-rec";
  if (state === "error") return "status-error";
  return "status-idle";
}
function setLhTiktokBadge(state) {
  const node = el("lh-tiktok-status");
  node.textContent = tiktokStatusLabel(state);
  node.className = tiktokStatusClass(state);
}

function stopLhStatusPoll() {
  if (lh.statusPollTimer) {
    clearInterval(lh.statusPollTimer);
    lh.statusPollTimer = null;
  }
}
function startLhStatusPoll() {
  stopLhStatusPoll();
  lh.statusPollTimer = setInterval(async () => {
    if (!lh.sessionId) return;
    try {
      const resp = await fetch(`/v1/livehost/${encodeURIComponent(lh.sessionId)}/status`);
      if (!resp.ok) {
        stopLhStatusPoll();
        setLhTiktokBadge("idle");
        return;
      }
      const body = await resp.json();
      setLhTiktokBadge(body.data.state);
    } catch {
      /* transient poll failure — try again next tick */
    }
  }, 2000);
}

export async function loadLivehostEngines() {
  try {
    const stt = await (await fetch("/v1/stt/engines")).json();
    stt.data.forEach((e) => (lhDetails.stt[e.engine] = e.detail));
    const sel = el("lh-stt-engine");
    if (sel) {
      sel.innerHTML = "";
      stt.data
        .filter((e) => e.available)
        .forEach((e) => {
          const opt = document.createElement("option");
          opt.value = e.engine;
          opt.textContent = e.engine;
          sel.appendChild(opt);
        });
      const pref = ["whisper_mlx", "whisper"].find((v) => [...sel.options].some((o) => o.value === v));
      if (pref) sel.value = pref;
      restoreAndBind("lh-stt-engine");
    }
    restoreAndBind("lh-language");
    restoreAndBind("lh-opus");
  } catch (error) {
    lhLog(`engines error: ${error}`);
  }
}

export async function startLhSession() {
  setLhSessionUI("starting");
  lhStopAudio();
  el("lh-dialogue").innerHTML = "";
  lh.log = [];
  el("lh-log").textContent = "";
  setLhTiktokBadge("idle");
  el("lh-tiktok-error").classList.add("hidden");

  const sttEngine = el("lh-stt-engine").value;
  if (!sttEngine) {
    setLhStatus("Không có STT engine khả dụng", "status-error");
    setLhSessionUI("idle");
    return;
  }

  setLhStatus("⏳ khởi động STT engine…", "status-idle");
  try {
    const warmRes = await fetch(`/v1/stt/warm?engine=${encodeURIComponent(sttEngine)}`, { method: "POST" });
    if (!warmRes.ok) {
      setLhStatus(`STT engine '${sttEngine}' chưa sẵn sàng`, "status-error");
      setLhSessionUI("idle");
      return;
    }
  } catch {
    setLhStatus("Không thể kết nối STT engine", "status-error");
    setLhSessionUI("idle");
    return;
  }

  lh.sessionId = crypto.randomUUID();
  let params = `stt_engine=${encodeURIComponent(sttEngine)}&session_id=${encodeURIComponent(lh.sessionId)}`;
  params += `&sample_rate=${STREAM_SAMPLE_RATE}`;
  const ttsProfile = el("lh-tts-profile")?.value;
  if (ttsProfile) params += `&tts_profile=${encodeURIComponent(ttsProfile)}`;
  if (el("lh-language").value.trim()) params += `&language=${encodeURIComponent(el("lh-language").value.trim())}`;
  const cpp = getPreproc();
  params += `&denoise=${cpp.denoise}&vad=${cpp.vad}&vad_backend=${encodeURIComponent(cpp.backend)}`;

  lh.opusMode = !!el("lh-opus")?.checked && lhOpusSupported();
  if (el("lh-opus")?.checked && !lh.opusMode) {
    lhLog("Opus downlink unsupported in this browser — using WAV/URL.");
  }
  if (lh.opusMode) {
    lh.outRate = 24000;
    params += `&output=audio,text&audio_out=opus&output_sample_rate=${lh.outRate}`;
  }
  lhResetOpus();

  let capture;
  try {
    capture = createMicCapture({
      onframe: (pcm) => {
        if (!lh.ws || lh.ws.readyState !== WebSocket.OPEN) return;
        if (lhIsSpeaking()) return;
        lh.ws.send(pcm.buffer);
      },
    });
  } catch (error) {
    setLhStatus("mic error", "status-error");
    setLhSessionUI("idle");
    return;
  }

  const ws = new WebSocket(wsUrl(`/v1/livehost/stream?${params}`));
  lh.ws = ws;
  if (lh.opusMode) ws.binaryType = "arraybuffer";

  ws.onopen = async () => {
    try {
      await capture.start();
      lh.capture = capture;
      setLhStatus("● listening", "status-rec");
      setLhSessionUI("recording");
    } catch (error) {
      setLhStatus("mic denied", "status-error");
      ws.close();
    }
  };

  ws.onmessage = (event) => {
    if (typeof event.data !== "string") {
      lhFeedOpus(event.data);
      return;
    }
    let d;
    try {
      d = JSON.parse(event.data);
    } catch {
      return;
    }
    lhLog(`${d.event}: ${d.text ? d.text.slice(0, 60) : JSON.stringify({ ...d, event: undefined })}`);
    switch (d.event) {
      case "session_started":
        if (d.output_sample_rate) lh.outRate = d.output_sample_rate;
        setLhTiktokControlsEnabled(true);
        if (d.stt_ready === false || d.tts_ready === false) {
          setLhStatus("⏳ engines warming up, please wait…", "status-idle");
        }
        break;
      case "engines_ready":
        setLhStatus("● listening", "status-rec");
        break;
      case "speech_start":
        setLhStatus("● you're speaking", "status-rec");
        break;
      case "speech_end":
      case "processing":
        setLhStatus("… thinking", "status-idle");
        break;
      case "user_transcript":
        if (d.text) lhAddBubble("user", d.text);
        break;
      case "social_event": {
        const label =
          d.kind === "gift"
            ? `🎁 ${d.user_name} gifted ${d.gift_name || ""}${d.gift_value ? ` (${d.gift_value})` : ""}`
            : d.kind === "follow"
              ? `${d.user_name} followed`
              : d.kind === "like"
                ? `${d.user_name} liked`
                : `${d.user_name}: ${d.text || ""}`;
        lhAddFeedRow(label);
        break;
      }
      case "social_reply":
        lh.pendingReplyIsSocial = true;
        break;
      case "response_text": {
        if (d.chunk_index === 0 || !lh.assistantBubble) {
          const prefix = lh.pendingReplyIsSocial ? "↳ replying to chat: " : "";
          lh.assistantBubble = lhAddBubble("assistant", prefix + d.text);
          lh.pendingReplyIsSocial = false;
        } else {
          lh.assistantBubble.textContent += " " + d.text;
          el("lh-dialogue").scrollTop = el("lh-dialogue").scrollHeight;
        }
        break;
      }
      case "audio_start":
        if (d.codec === "opus" && d.sample_rate) lh.outRate = d.sample_rate;
        break;
      case "audio_chunk":
        if (d.audio_url) lhEnqueueAudio(d.audio_url);
        break;
      case "turn_done":
        lh.assistantBubble = null;
        setLhStatus("● listening", "status-rec");
        break;
      case "aborted":
        lhStopAudio();
        break;
      case "error":
        setLhStatus(`error: ${d.message || ""}`, "status-error");
        break;
    }
  };

  ws.onerror = () => setLhStatus("ws error", "status-error");
  ws.onclose = () => {
    setLhSessionUI("idle");
    setLhTiktokControlsEnabled(false);
    el("lh-tiktok-disconnect").disabled = true;
    stopLhStatusPoll();
    setLhTiktokBadge("idle");
    lh.sessionId = null;
    if (el("lh-status").className !== "status-error") setLhStatus("idle", "status-idle");
  };
}

export function stopLhSession() {
  if (lh.capture) {
    lh.capture.stop();
    lh.capture = null;
  }
  if (lh.ws) {
    if (lh.ws.readyState === WebSocket.OPEN) lh.ws.send(JSON.stringify({ type: "end" }));
    try {
      lh.ws.close();
    } catch {}
    lh.ws = null;
  }
  stopLhStatusPoll();
  setLhSessionUI("idle");
}

export async function connectTiktok() {
  const username = el("lh-tiktok-username").value.trim();
  const errEl = el("lh-tiktok-error");
  errEl.classList.add("hidden");
  if (!username) {
    errEl.textContent = "Enter a TikTok username";
    errEl.classList.remove("hidden");
    return;
  }
  if (!lh.sessionId) {
    errEl.textContent = "Start the session first";
    errEl.classList.remove("hidden");
    return;
  }
  try {
    const resp = await fetch(`/v1/livehost/${encodeURIComponent(lh.sessionId)}/connect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ unique_id: username }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      errEl.textContent = body.detail || "Connect failed";
      errEl.classList.remove("hidden");
      return;
    }
    setLhTiktokBadge(body.data.state);
    el("lh-tiktok-disconnect").disabled = false;
    startLhStatusPoll();
  } catch (error) {
    errEl.textContent = String(error);
    errEl.classList.remove("hidden");
  }
}

export async function disconnectTiktok() {
  if (!lh.sessionId) return;
  try {
    const resp = await fetch(`/v1/livehost/${encodeURIComponent(lh.sessionId)}/disconnect`, { method: "POST" });
    if (resp.ok) {
      const body = await resp.json();
      setLhTiktokBadge(body.data.state);
    }
  } catch {
    /* best-effort */
  } finally {
    stopLhStatusPoll();
    el("lh-tiktok-disconnect").disabled = true;
  }
}

if (el("lh-session-start")) el("lh-session-start").addEventListener("click", startLhSession);
if (el("lh-session-stop"))
  el("lh-session-stop").addEventListener("click", () => {
    lhStopAudio();
    stopLhSession();
  });
if (el("lh-tiktok-connect")) el("lh-tiktok-connect").addEventListener("click", connectTiktok);
if (el("lh-tiktok-disconnect")) el("lh-tiktok-disconnect").addEventListener("click", disconnectTiktok);
```

- [ ] **Step 6: Wire `livehost.js` into the app bootstrap**

In `apps/api_gateway/app/static/js/main.js`, add the import next to `loadConversationEngines`:

```js
import { setConvUI, loadConversationEngines } from "./conversation.js";
import { loadLivehostEngines } from "./livehost.js";
```

and call it next to the other eager loads:

```js
loadConversationEngines();
loadLivehostEngines();
```

- [ ] **Step 7: Syntax-check every touched/created JS file**

Run:
```bash
node --check apps/api_gateway/app/static/js/tts-profiles.js
node --check apps/api_gateway/app/static/js/livehost.js
node --check apps/api_gateway/app/static/js/main.js
```
Expected: no output, exit code 0 for each.

- [ ] **Step 8: Run the full backend test suite (sanity check — this diff touches no Python)**

Run: `pytest tests/ -q`
Expected: all pass (438 passed, 2 skipped, per the current baseline — this diff shouldn't change that number since it touches no backend code).

- [ ] **Step 9: Smoke-test the served markup and REST endpoints**

Start the dev server (`make dev` or an ad-hoc `uvicorn app.main:app` from the repo, with `PYTHONPATH=apps/api_gateway`), then:

```bash
curl -s http://127.0.0.1:8000/static/index.html | grep -o \
  'id="lh-session-start"\|id="lh-tts-profile"\|id="lh-tiktok-username"\|id="lh-dialogue"\|data-section="livehost"'
```
Expected: all five ids/attributes present in the output.

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/static/js/livehost.js
```
Expected: `200`.

- [ ] **Step 10: Manual verification in a browser**

In the browser, at `/static/index.html`:
1. Click the new "Livehost" nav item — confirm the TikTok Co-host card renders, with STT engine and TTS Profile dropdowns populated (same data as the Chat section's equivalents).
2. Click "Start Session" — grant mic permission — confirm status goes to "● listening" and the TikTok username/Connect controls become enabled (they should NOT be enabled before this point).
3. Type a TikTok username of a room that is not currently live (any string) and click "Connect" — confirm the TikTok status badge shows "connecting…" then eventually "error" or "offline, waiting…" (a real connection attempt goes out over the network; a nonexistent/offline room is the safe, deterministic case to test without needing an actual live TikTok stream).
4. Click "Disconnect" — confirm the badge returns toward idle and the session status/mic are unaffected (still listening).
5. Speak into the mic — confirm a "user" bubble and an "assistant" reply bubble appear in the dialogue, and (if TTS is configured) audio plays.
6. Click "Stop Session" — confirm everything resets to idle and the TikTok controls become disabled again.

- [ ] **Step 11: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/styles.css apps/api_gateway/app/static/js/tts-profiles.js apps/api_gateway/app/static/js/livehost.js apps/api_gateway/app/static/js/main.js
git commit -m "feat(ui): add Livehost (TikTok co-host) sidebar section"
```

---

## Self-Review Notes

- **Spec coverage:** design spec's "TTS/STT & TikTok connect bar" → Step 2 markup + Step 5 engine/profile population. "Status" (session + TikTok badge, 2s poll) → Step 5's `setLhStatus`/`setLhTiktokBadge`/`startLhStatusPoll`. "Event log" (bubbles + feed rows + social_reply tagging) → Step 5's `ws.onmessage` switch. "Data Flow" 1-5 → Step 5's `startLhSession`/`connectTiktok`/`disconnectTiktok`/`stopLhSession`. "Error Handling" (WS errors, failed connect, dead status poll) → covered in the same functions. "Out of Scope" items (gift icons, event history, multi-room) are not implemented, correctly.
- **No placeholders:** every step has complete, runnable code.
- **Type/name consistency:** `lh` state object, `setLhStatus`/`setLhTiktokBadge`/`setLhSessionUI`/`setLhTiktokControlsEnabled`, `startLhSession`/`stopLhSession`/`connectTiktok`/`disconnectTiktok`, and the five DOM ids (`lh-session-start`, `lh-session-stop`, `lh-status`, `lh-tiktok-username`, `lh-tiktok-connect`, `lh-tiktok-disconnect`, `lh-tiktok-status`, `lh-tiktok-error`, `lh-dialogue`, `lh-log`, `lh-stt-engine`, `lh-tts-profile`, `lh-language`, `lh-opus`) are used identically between the HTML (Step 2) and JS (Step 5) — checked one-for-one.
