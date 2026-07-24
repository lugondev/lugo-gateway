# Engine: infer from provider + hide (STT symmetric with TTS/LLM)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop making the admin pick a remote STT "engine". When a Provider is selected, INFER the wire-format adapter from the provider's base_url and HIDE the Engine field entirely — so a provider-linked stt/tts entry is just **Provider + Model** (symmetric with TTS/LLM). The Engine `<select>` is shown ONLY for a LOCAL (no-provider) stt/tts model, where it genuinely picks the local runtime. Legacy duplicates (whisper_service/eventlab — same OpenAI-compat multipart protocol as http_stt) fall out of the form automatically.

**Why:** Confirmed from code — remote STT engines are only 2 real protocols: `/audio/transcriptions` multipart (http_stt; whisper_service/eventlab are legacy duplicates of it) vs `/audio/transcriptions` JSON-base64 (qwen3_asr_or/whisper_or, OpenRouter-only). The wire format is derivable from the provider's host, so the user never needs to choose it. Note the intended model: **remote = via a Provider; no-provider = local.**

**Architecture:** Static-UI only — one file, `apps/api_gateway/app/static/js/model-registry.js`. Rework `_loadEngineOptions` (show local select only when no provider) and `_effectiveEngine` (infer remote engine from the cached provider's base_url), and cache providers (incl. base_url) in `_loadProviderOptions`. No index.html/CSS/backend change (the engine `<select>` element stays, used for the local case; the `mode` field from the prior task is still used to filter local engines).

## Global Constraints
- Static-UI only. Verify `node --check` + grep; NO pytest.
- Engine field VISIBLE only for: kind ∈ {stt,tts} AND no provider selected (local runtime choice). HIDDEN for: kind=llm (any), OR any provider selected.
- `_effectiveEngine()` must return the correct engine in the hidden cases:
  - llm → the selected provider's name (option text `name — label` → name part) or "custom" (unchanged).
  - provider + tts → `http_tts`.
  - provider + stt → infer from the provider's base_url host: contains `openrouter.ai` → `qwen3_asr_or` (JSON-base64 format); else → `http_stt` (multipart OpenAI-compat).
  - no provider → the local `<select>`'s value.
- `/v1/providers` returns `base_url` unmasked (only api_key is masked) — cache it.
- Git `lugondev <lugondev@gmail.com>`. Concurrent session — re-check branch before git. No push (main auto-deploys prod).

---

### Task 1: Infer engine from provider + hide the field

**Files:** Modify `apps/api_gateway/app/static/js/model-registry.js`.

- [ ] **Step 1: Read** the current `_loadProviderOptions`, `_loadEngineOptions`, `_effectiveEngine`, and the listener wiring (kind-change / provider-change both call `_loadEngineOptions`; `createModelRegistryEntry` calls `_effectiveEngine`).

- [ ] **Step 2: Cache providers (with base_url).** Add a module-level `let _providersCache = [];` near the top (by `registryData`). In `_loadProviderOptions`, after fetching, set `_providersCache = providers;` (the array already has `{id, name, label, base_url, enabled}`). Keep the existing option-building loop.

- [ ] **Step 3: Replace `_loadEngineOptions`** — show the local-runtime select only when no provider + stt/tts; hide otherwise:
```javascript
async function _loadEngineOptions() {
  const kind = el("registry-add-kind")?.value;
  const sel = el("registry-add-engine");
  const wrap = el("registry-add-engine-wrap");
  if (!sel) return;
  const hasProvider = !!(el("registry-add-provider")?.value || "").trim();
  // Engine is a real choice only for a LOCAL (no-provider) stt/tts model — the
  // runtime. For a provider (remote) the wire-format adapter is inferred from
  // the provider host in _effectiveEngine(); for llm it's cosmetic. Hide those.
  if (kind === "llm" || hasProvider) { if (wrap) wrap.classList.add("hidden"); return; }
  if (wrap) wrap.classList.remove("hidden");
  const prev = sel.value;
  sel.innerHTML = "";
  let engines = [];
  try { engines = (await (await fetch(`/v1/${kind}/engines`)).json()).data || []; } catch { /* leave empty */ }
  for (const e of engines.filter((x) => x.mode === "local")) {
    const opt = document.createElement("option");
    opt.value = e.engine;
    opt.textContent = e.available ? e.engine : `${e.engine} (unavailable)`;
    sel.appendChild(opt);
  }
  if (engines.some((e) => e.engine === prev && e.mode === "local")) sel.value = prev;
}
```

- [ ] **Step 4: Replace `_effectiveEngine`** — infer the remote engine from the provider's base_url:
```javascript
// The engine actually submitted. For a LOCAL (no-provider) stt/tts model it's
// the runtime the admin picked. For a provider it's inferred from the provider's
// base_url wire format; for llm it's the (cosmetic) provider name.
function _effectiveEngine() {
  const kind = el("registry-add-kind")?.value;
  const providerId = (el("registry-add-provider")?.value || "").trim();
  if (kind === "llm") {
    const name = el("registry-add-provider")?.selectedOptions?.[0]?.textContent || "";
    return (name.split(" — ")[0] || "custom").trim() || "custom";
  }
  if (providerId) {
    if (kind === "tts") return "http_tts";
    const prov = _providersCache.find((p) => p.id === providerId);
    const base = (prov?.base_url || "").toLowerCase();
    // OpenRouter STT uses /audio/transcriptions with a JSON base64 body
    // (qwen3_asr_or); every other OpenAI-compatible host uses multipart upload
    // (http_stt). model_id (from the form) selects the actual model either way.
    return base.includes("openrouter.ai") ? "qwen3_asr_or" : "http_stt";
  }
  return (el("registry-add-engine")?.value || "").trim(); // local runtime choice
}
```

- [ ] **Step 5: Verify** — `node --check apps/api_gateway/app/static/js/model-registry.js` (OK). Grep: `_providersCache` set in `_loadProviderOptions` + used in `_effectiveEngine`; `_loadEngineOptions` hides on `hasProvider`; `_effectiveEngine` infers `http_stt`/`qwen3_asr_or`/`http_tts`. Manual reasoning walk-through (state in report):
  - Provider=OpenRouter + stt → engine hidden, `_effectiveEngine`→`qwen3_asr_or`.
  - Provider=OpenAI/QwenCloud + stt → engine hidden, →`http_stt`.
  - Provider=any + tts → hidden, →`http_tts`.
  - No provider + stt → local select shown (vosk/whisper/mlx/qwen3_asr…), →selected value.
  - kind=llm → hidden, →provider name.

- [ ] **Step 6: Commit** — model-registry.js → `feat(admin-ui): infer STT/TTS engine from provider + hide field (Provider+Model only)`.

---

### Task 2: Verify (controller)
- [ ] `node --check apps/api_gateway/app/static/js/model-registry.js`; grep the inference logic present; `.venv/bin/python -c "import app.main"` (unaffected).

## Deferred / note
- Remote stt/tts WITHOUT a provider is no longer creatable via the form (by design: remote ⇒ Provider). Backend still supports such entries; they're just not offered in the add-form. whisper_service/eventlab (legacy multipart duplicates of http_stt) thus drop out of the form. If a genuine need arises, revisit.
- qwen3_asr_or vs whisper_or: both are the OpenRouter JSON-base64 format (same OpenRouterSttProvider); the concrete model comes from model_id, so inferring `qwen3_asr_or` for any OpenRouter STT is correct regardless of model family.

## Self-Review
- **Coverage:** engine no longer a confusing free/again-select for remote — inferred from provider host + hidden; STT now symmetric with TTS/LLM (Provider + Model). Local models still choose a runtime. Directly answers "why does only STT have engine / http_stt≈whisper_service≈openai_compat."
- **Placeholders:** complete code for both reworked functions + the cache.
- **Consistency:** `_providersCache` (with base_url) set in `_loadProviderOptions`, read in `_effectiveEngine`; hide condition (`llm || hasProvider`) in `_loadEngineOptions` matches the "show only for local stt/tts" rule; `_effectiveEngine` covers all four cases; createModelRegistryEntry still calls `_effectiveEngine()` (unchanged) + still sends config.provider_id.
