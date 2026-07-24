# Profile response: add resolved STT/LLM/TTS labels

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Use SONNET (not haiku) for code edits, and verify with the Read tool — this session's shell can false-pass `node --check`.

**Goal:** Every `/v1/profiles` response includes resolved friendly labels (`stt_label`, `llm_label`, `tts_label`) for what a session would actually use — the profile's pinned model, or the server default with a `(default)` marker — so a user-facing client can show the label without decoding raw engine/model ids. Raw `stt/llm/tts` STAY (the web + lugo-web-client profile editors read `.engine/.model` to pre-fill their dropdowns; removing them breaks editing).

**Architecture:** Backend only, `apps/api_gateway/app/api/routes/profiles.py`. Add an async `_with_labels(profile)` that wraps `_mask(profile)` and adds the three labels, and use it in every route that currently returns `_mask(...)` (list/get/create/update/clone). Label resolution mirrors the conversation summary + `/v1/model_registry/defaults`.

## Global Constraints
- Backend only. Tests from repo ROOT `.venv/bin/python -m pytest`; asyncio auto; `_tmp_db` autouse (never a param); sync TestClient.
- ADDITIVE: keep the existing `_mask` output (raw stt/llm/tts, masked api_key); just add `stt_label`/`llm_label`/`tts_label`.
- Label rule: profile pins engine → `model_registry_store.find(kind,engine,model).label` (else `engine/model` or `engine`); profile doesn't pin (empty engine) → the SERVER DEFAULT label + `" (default)"` (stt: `default_stt_engine`+`resolve_default_stt_model`; llm: `find_default("llm")`; tts: `default_tts_engine`), or `"server default"` if none.
- tts label source = `profile.tts.profile_name` (already a friendly name) or the default tts engine.
- Git `lugondev <lugondev@gmail.com>`. Concurrent session — re-check branch before git. No push (main auto-deploys prod).

---

### Task 1: `_with_labels` + apply to all profile routes

**Files:** Modify `apps/api_gateway/app/api/routes/profiles.py`; Test `tests/unit/test_profile_labels.py`.

- [ ] **Step 1: Failing test**
```python
# tests/unit/test_profile_labels.py
from fastapi.testclient import TestClient
from app.core.settings import settings
from app.main import app
from app.services.model_registry.store import model_registry_store


def _client(): return TestClient(app)

def _login(client, name="u"):
    client.post("/api/auth/signup", json={"username": name, "password": "pw"})
    client.post("/api/auth/login", json={"username": name, "password": "pw"})


def test_profile_response_has_resolved_labels(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    import asyncio
    from app.services.db.engine import init_db
    asyncio.run(init_db())
    # a registry entry whose label we expect to see
    asyncio.run(model_registry_store.create("stt", "qwen3_asr_or", "qwen3-asr-flash", "Qwen3 ASR Flash"))
    client = _client()
    _login(client)
    # profile pinning that stt engine/model
    r = client.post("/v1/profiles", json={"name": "p1", "stt": {"engine": "qwen3_asr_or", "model": "qwen3-asr-flash"}})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["stt_label"] == "Qwen3 ASR Flash"       # resolved registry label, not raw engine/model
    assert "llm_label" in d and "tts_label" in d
    # raw still present (editors need it)
    assert d["stt"]["engine"] == "qwen3_asr_or" and d["stt"]["model"] == "qwen3-asr-flash"


def test_unpinned_fields_show_server_default_label(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    client = _client(); _login(client, "u2")
    r = client.post("/v1/profiles", json={"name": "p2", "stt": {"language": "vi"}})  # no stt engine/model
    d = r.json()["data"]
    # unpinned -> a "(default)" label or the literal "server default", never blank
    assert d["stt_label"] and d["tts_label"] and d["llm_label"]
    assert d["tts_label"]  # profile_name empty -> default engine label or "server default"
```

- [ ] **Step 2: Run — FAIL** (no `stt_label`).

- [ ] **Step 3: Implement** in `profiles.py`. Add `from app.services.model_registry.store import model_registry_store` at the top (if not already imported). Add after `_mask`:
```python
async def _label_for(kind: str, engine: str, model_id: str) -> str:
    if not engine:
        return ""
    entry = await model_registry_store.find(kind, engine, model_id or "")
    if entry and entry.get("label"):
        return entry["label"]
    return f"{engine}/{model_id}" if model_id else engine


async def _with_labels(profile: Profile) -> dict:
    """_mask() + resolved friendly labels (stt_label/llm_label/tts_label) for what
    a session would actually use — the profile's pin, or the server default marked
    "(default)". Additive: raw stt/llm/tts stay (the profile editors read them)."""
    from app.services.stt.model_catalog import resolve_default_stt_model
    from app.services.system_config import system_config_store

    data = _mask(profile)
    eng = system_config_store.get().engines

    stt_label = await _label_for("stt", profile.stt.engine, profile.stt.model)
    if not stt_label:
        base = await _label_for("stt", eng.default_stt_engine, resolve_default_stt_model(eng.default_stt_engine) or "")
        stt_label = f"{base} (default)" if base else "server default"

    llm_label = await _label_for("llm", profile.llm.engine, profile.llm.model)
    if not llm_label:
        d = await model_registry_store.find_default("llm")
        if d:
            base = d.get("label") or (f'{d["engine"]}/{d["model_id"]}' if d.get("model_id") else d["engine"])
            llm_label = f"{base} (default)"
        else:
            llm_label = "server default"

    tts_label = profile.tts.profile_name or (
        f"{eng.default_tts_engine} (default)" if eng.default_tts_engine else "server default"
    )

    data["stt_label"], data["llm_label"], data["tts_label"] = stt_label, llm_label, tts_label
    return data
```

- [ ] **Step 4: Apply to every route returning `_mask(...)`** — replace with `await _with_labels(...)`:
  - `get_profile`, `create_profile`, `update_profile`, `clone_profile`: `return {"success": True, "data": await _with_labels(profile)}`.
  - `list_profiles`: the dict comprehension can't `await`; rewrite as a loop:
    ```python
    data = {}
    for k, v in visible.items():
        data[k] = await _with_labels(v)
    return {"success": True, "data": data}
    ```

- [ ] **Step 5: Run — PASS**; regression `.venv/bin/python -m pytest tests/unit/test_stt_profile.py tests/unit -q -k "profile"` (existing profile tests must stay green — they assert the raw shape, which is preserved).

- [ ] **Step 6: Verify no static/lugo-web-client editor breakage** — reasoning only: raw `stt/llm/tts` are untouched (additive), so `profiles.js`/`conversation.js`/lugo-web-client that read `.stt.engine` etc. are unaffected. State this in the report.

- [ ] **Step 7: Commit** — profiles.py + test → `feat(profiles): add resolved stt/llm/tts labels to profile responses (raw kept)`.

---

### Task 2: Verify (controller)
- [ ] `.venv/bin/python -m pytest tests/unit/test_profile_labels.py -q` + profile regression + `.venv/bin/python -c "import app.main"`.

## Self-Review
- **Coverage:** every /v1/profiles response now carries resolved stt/llm/tts labels (pinned model's label, or server-default label with "(default)"), so a user-facing client shows friendly names without decoding engine/model — while raw stays for the editors (verified 3 consumers depend on it: static profiles.js pre-fill, conversation.js label calc, lugo-web-client Profile type).
- **Placeholders:** complete code.
- **Consistency:** label logic mirrors `/v1/model_registry/defaults`; applied in all 5 routes; additive (raw preserved).
