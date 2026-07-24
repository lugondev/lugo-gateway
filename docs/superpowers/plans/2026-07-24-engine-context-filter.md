# Engine dropdown: context-aware filtering (local vs remote by provider)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the Model Registry add-form ENGINE dropdown meaningful: when a Provider (remote) is selected, offer only the **remote/service** adapters (default the generic `http_stt`/`http_tts`) and drop the misleading local-package "(unavailable)" tag; when no provider is selected, offer only **local** runtimes. Hide the field entirely when there's ≤1 relevant choice (e.g. remote TTS = just `http_tts`). llm keeps its existing auto-derive+hide.

**Why:** `engine` is the dispatch/adapter key. For a provider-linked (remote OpenAI-compat) entry, local engines (vosk, whisper, mlx, qwen3_asr…) are irrelevant and the local-package availability check is meaningless — showing them all (as now) is confusing.

**Architecture:** STT `/v1/stt/engines` already returns a `mode` field ("local"|"remote") per engine; TTS `/v1/tts/engines` does NOT — Task 1 adds it (remote = `http_tts`, matching `_SERVICE_TTS_ENGINES`). Task 2 filters the dropdown in `model-registry.js::_loadEngineOptions` using `mode` + whether a provider is selected. `_effectiveEngine()` already reads `sel.value` for non-llm regardless of visibility, so no change there.

## Global Constraints
- T1 backend: tests from repo ROOT `.venv/bin/python -m pytest`; asyncio auto; `_tmp_db` autouse (never a param). T2 static-UI: `node --check` + grep, NO pytest.
- `mode` values: "local" | "remote". TTS remote engine set = `{http_tts}` (mirror `_SERVICE_TTS_ENGINES` in model_registry route). Keep STT `/engines` unchanged (already has mode).
- Preserve everything else in the add-form (provider→config.provider_id, per-kind creds, llm engine hidden+derived, model combobox).
- Git `lugondev <lugondev@gmail.com>`. Concurrent session active — re-check `git branch --show-current` before git steps. No push (main auto-deploys prod).

---

### Task 1: Add `mode` to TTS `list_engines`

**Files:** Modify `apps/api_gateway/app/services/tts/service.py`; Test `tests/unit/test_tts_engines_mode.py`.

- [ ] **Step 1: Failing test**
```python
# tests/unit/test_tts_engines_mode.py
from app.services.tts.service import tts_service


def test_tts_list_engines_has_mode_and_http_tts_is_remote():
    engines = tts_service.list_engines()
    assert engines, "expected at least one tts engine registered"
    by_name = {e["engine"]: e for e in engines}
    # every engine carries a mode in {local, remote}
    assert all(e.get("mode") in ("local", "remote") for e in engines)
    # http_tts is the one remote (OpenAI-compatible HTTP) tts engine
    if "http_tts" in by_name:
        assert by_name["http_tts"]["mode"] == "remote"
    # a representative local engine is "local" (edge_tts always registers)
    local_names = [n for n, e in by_name.items() if e["mode"] == "local"]
    assert local_names, "expected at least one local tts engine"
    assert "http_tts" not in local_names
```

- [ ] **Step 2: Run — FAIL** (`.venv/bin/python -m pytest tests/unit/test_tts_engines_mode.py -v`) — no `mode` key.

- [ ] **Step 3: Implement** — in `tts/service.py::list_engines`, add a `mode` to each appended dict. Remote = `http_tts` (the only OpenAI-compatible HTTP tts engine; matches `_SERVICE_TTS_ENGINES = {"http_tts"}` in the model_registry route). Add inside the loop, before/at the `result.append({...})`:
```python
            # "remote" = calls out to an OpenAI-compatible HTTP service (http_tts);
            # everything else runs in-process ("local"). Mirrors _SERVICE_TTS_ENGINES
            # in routes/model_registry.py and the `mode` field STT's list_engines emits.
            mode = "remote" if name == "http_tts" else "local"
```
and add `"mode": mode,` to the appended dict.

- [ ] **Step 4: Run — PASS.** Regression: `.venv/bin/python -m pytest tests/unit/test_tts_service.py -q` (if it exists; else skip) + confirm nothing asserts the old dict shape strictly.

- [ ] **Step 5: Commit** — tts/service.py + test → `feat(tts): add mode (local/remote) to list_engines`.

---

### Task 2: Context-filter the Engine dropdown in the add-form

**Files:** Modify `apps/api_gateway/app/static/js/model-registry.js` (rework `_loadEngineOptions` only).

- [ ] **Step 1: Read** the current `_loadEngineOptions` + `_effectiveEngine` + the bottom listener wiring (kind-change / provider-change both already call `_loadEngineOptions`). Confirm `_effectiveEngine` returns `sel.value` for non-llm (works hidden or shown) — do NOT change it.

- [ ] **Step 2: Replace `_loadEngineOptions`** with the context-aware version:
```javascript
async function _loadEngineOptions() {
  const kind = el("registry-add-kind")?.value;
  const sel = el("registry-add-engine");
  const wrap = el("registry-add-engine-wrap");
  if (!sel) return;
  if (kind === "llm") { if (wrap) wrap.classList.add("hidden"); return; } // engine derived from provider
  const hasProvider = !!(el("registry-add-provider")?.value || "").trim();
  const prev = sel.value;
  sel.innerHTML = "";
  let engines = [];
  try {
    const body = await (await fetch(`/v1/${kind}/engines`)).json();
    engines = body.data || [];
  } catch { /* leave empty; submit's required-guard will surface it */ }
  // With a provider (remote OpenAI-compat), only remote adapters use its creds;
  // without a provider, only local runtimes make sense. `mode` comes from the backend.
  const relevant = engines.filter((e) => (hasProvider ? e.mode === "remote" : e.mode === "local"));
  for (const e of relevant) {
    const opt = document.createElement("option");
    opt.value = e.engine;
    // The "(unavailable)" tag is a LOCAL package check — meaningless for a remote engine.
    opt.textContent = (!hasProvider && !e.available) ? `${e.engine} (unavailable)` : e.engine;
    sel.appendChild(opt);
  }
  // Default: with a provider, prefer the generic OpenAI-compatible adapter.
  const preferred = kind === "stt" ? "http_stt" : "http_tts";
  if (hasProvider && relevant.some((e) => e.engine === preferred)) {
    sel.value = preferred;
  } else if (relevant.some((e) => e.engine === prev)) {
    sel.value = prev; // keep the prior selection when still valid
  }
  // Nothing meaningful to choose (0 or 1 option) -> hide; the single value is
  // already selected and _effectiveEngine() reads sel.value regardless.
  if (wrap) wrap.classList.toggle("hidden", relevant.length <= 1);
}
```

- [ ] **Step 3: Verify** — `node --check apps/api_gateway/app/static/js/model-registry.js` (OK); grep confirms `_loadEngineOptions` now references `e.mode` and the `preferred`/`relevant` logic; confirm `_effectiveEngine` unchanged. (Manual reasoning: provider+stt → 5 remote engines, default http_stt; provider+tts → only http_tts → hidden; no-provider+stt → local runtimes shown with availability tag.)

- [ ] **Step 4: Commit** — model-registry.js → `feat(admin-ui): context-filter engine dropdown (remote w/ provider, local without)`.

---

### Task 3: Verify (controller)
- [ ] `.venv/bin/python -m pytest tests/unit/test_tts_engines_mode.py -q` (pass) + `node --check apps/api_gateway/app/static/js/model-registry.js` + `.venv/bin/python -c "import app.main"`.

## Self-Review
- **Coverage:** engine dropdown now context-correct — remote adapters (default http_stt/http_tts) when a provider is chosen, local runtimes otherwise; misleading local "(unavailable)" tag dropped for remote; field hidden when ≤1 choice (remote TTS). Directly addresses "engine list is meaningless / all registry" for the provider flow.
- **Placeholder scan:** complete code both tasks.
- **Consistency:** TTS `mode` field mirrors STT's + `_SERVICE_TTS_ENGINES`; UI filters on `e.mode`; `preferred` = http_stt/http_tts; `_effectiveEngine` untouched (reads sel.value hidden-or-shown, so a hidden single-engine still submits correctly).
