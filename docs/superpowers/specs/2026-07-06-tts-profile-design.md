# TTS Profile — Design Spec
**Date:** 2026-07-06

## Overview

Introduce a standalone, named **TTS Profile** entity that bundles everything needed to synthesize speech with one voice: engine, a preset voice *or* a cloned voice (reference audio + transcript), and optional style/speed/language overrides. LLM Profiles (`services/profiles/models.py`) currently inline `engine`+`voice` in a nested `TtsConfig` — this changes to a single reference (`profile_name`) pointing at a TTS Profile, so a conversation only has to pick a name instead of re-entering engine/voice (and now instruct/speed/language/clone params) every time.

`profiles.json` is currently empty in this repo (checked both root and `apps/api_gateway/`), so no migration path is needed — `TtsConfig`'s schema can change directly.

---

## 1. Data Model & Storage

### `TtsProfile` — `services/tts/profile_models.py`

```python
from typing import Literal
from pydantic import BaseModel

class TtsProfile(BaseModel):
    name: str
    engine: str = ""
    voice_mode: Literal["preset", "clone"] = "preset"
    voice: str = ""            # preset mode: voice id from GET /v1/tts/voices?engine=
    ref_audio_path: str = ""   # clone mode
    ref_text: str = ""         # clone mode: transcript of the reference audio
    instruct: str = ""         # style/emotion instruction (e.g. omnivoice)
    speed: float | None = None
    language: str | None = None
```

All fields except `name` are optional; an empty `engine` means "fall back to server default" at resolution time (same fallback behavior `TtsConfig.engine` has today).

### `tts_profiles.json` (`settings.tts_profiles_path`, default `"tts_profiles.json"`)

```json
{
  "profiles": {
    "cohost-girl": {
      "name": "cohost-girl",
      "engine": "vieneu",
      "voice_mode": "preset",
      "voice": "vi-female-1",
      "ref_audio_path": "",
      "ref_text": "",
      "instruct": "",
      "speed": 1.0,
      "language": null
    },
    "cloned-host": {
      "name": "cloned-host",
      "engine": "omnivoice",
      "voice_mode": "clone",
      "voice": "",
      "ref_audio_path": "artifacts/refs/host.wav",
      "ref_text": "Xin chào các bạn, hôm nay...",
      "instruct": "",
      "speed": 1.0,
      "language": "vi"
    }
  }
}
```

Same atomic-write-on-change convention as `profiles.json`/`mcp_servers.json`: temp file + rename, created empty on first access.

### `services/tts/profile_store.py` — `TtsProfileStore`

Byte-for-byte the same shape as `ProfileStore` (`services/profiles/store.py`):
- `list() -> dict[str, TtsProfile]`
- `get(name) -> TtsProfile | None`
- `upsert(profile: TtsProfile) -> None`
- `delete(name) -> None`
- `threading.Lock`-guarded read/write.

Module-level singleton: `tts_profile_store = TtsProfileStore(settings.tts_profiles_path)`.

### `TtsConfig` change (`services/profiles/models.py`)

```python
class TtsConfig(BaseModel):
    profile_name: str = ""   # name of a TtsProfile; "" = server defaults
```

(Drops the old inline `engine`/`voice` fields — replaced entirely by the reference.)

---

## 2. API — `/v1/tts/profiles` (new router `api/routes/tts_profiles.py`)

Mirrors `api/routes/profiles.py` exactly (list/get/create/update/delete), registered in `main.py` alongside the existing `tts_router`:

- `GET /v1/tts/profiles` → `{success, data: {name: TtsProfile, ...}}`
- `POST /v1/tts/profiles` → create (body = `TtsProfile`, minus `name` duplication issues handled the same way `ProfileRequest` does)
- `GET /v1/tts/profiles/{name}` → 404 if missing
- `PUT /v1/tts/profiles/{name}` → upsert, name forced from path
- `DELETE /v1/tts/profiles/{name}`

No secrets to mask here (unlike LLM `api_key`), so no `_mask()` equivalent needed.

---

## 3. Resolution in `conversation.py` / `livehost.py`

Both currently do:

```python
if profile and profile.tts.engine:
    tts_engine = profile.tts.engine
    voice = profile.tts.voice or q.get("voice") or None
else:
    tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
    voice = q.get("voice") or None
```

New precedence, resolving a full `TtsProfile` instead of loose engine/voice:

1. Query param `tts_profile=<name>` (explicit ad-hoc pick, independent of which LLM profile is active)
2. `profile.tts.profile_name` (the LLM profile's linked TTS profile)
3. No profile resolved → legacy fallback: `q.get("tts_engine")`/`q.get("voice")` → `settings.conversation_tts_engine`/`settings.default_tts_engine`, with `ref_audio_path`/`ref_text`/`instruct`/`speed`/`language` left `None`.

```python
tts_profile_name = q.get("tts_profile") or (profile.tts.profile_name if profile else "") or None
tts_profile = tts_profile_store.get(tts_profile_name) if tts_profile_name else None

if tts_profile and tts_profile.engine:
    tts_engine = tts_profile.engine
    voice = tts_profile.voice or q.get("voice") or None
    ref_audio_path = tts_profile.ref_audio_path or None
    ref_text = tts_profile.ref_text or None
    instruct = tts_profile.instruct or None
    speed = tts_profile.speed
    tts_language = tts_profile.language
else:
    tts_engine = q.get("tts_engine") or settings.conversation_tts_engine or settings.default_tts_engine
    voice = q.get("voice") or None
    ref_audio_path = ref_text = instruct = None
    speed = tts_language = None
```

Every per-sentence `TTSRequest` construction (currently `TTSRequest(text=sentence, engine=tts_engine, voice=voice)` in both files) is updated to pass all resolved fields:

```python
TTSRequest(
    text=sentence, engine=tts_engine, voice=voice,
    ref_audio_path=ref_audio_path, ref_text=ref_text,
    instruct=instruct, speed=speed, language=tts_language,
)
```

The session-metadata echo (`meta={"stt_engine": ..., "tts_engine": tts_engine}` / the JSON blob with `"tts_engine": tts_engine`) is unchanged — engine name is still the identifying field clients read.

---

## 4. UI

### LLM Profile panel (`static/index.html` + `static/js/profiles.js`)

Replace the inline `pf-tts-engine` select + `pf-tts-voice-wrap` (voice select, only shown for vieneu) with a single `pf-tts-profile` select, populated from `GET /v1/tts/profiles` (same load-on-open pattern as `mcpServerData`/`profileData`). `pfUpdateTtsVoice()` (the show/hide-on-engine-change logic in `profiles.js`) is deleted along with its `tts-engines.js` wiring for `pf-tts-engine`, since engine is no longer picked here.

`saveProfile()`'s payload changes from:
```js
tts: { engine: el("pf-tts-engine")?.value || "", voice: el("pf-tts-voice")?.value || "" },
```
to:
```js
tts: { profile_name: el("pf-tts-profile")?.value || "" },
```

### New TTS Profile management panel (`static/js/tts-profiles.js` + markup in `index.html`)

Mirrors the LLM profile panel's list/edit/save/delete structure:
- List of profile names with new/edit/delete actions.
- Engine select (reuses existing `tts-engines.js` engine-list fetch).
- Voice-mode toggle: **Preset** (voice dropdown via `GET /v1/tts/voices?engine=`, same as today's vieneu-only case, now generalized to whatever engine is picked) vs **Clone** (text inputs for `ref_audio_path` and a textarea for `ref_text`).
- Plain inputs for `instruct` (text), `speed` (number), `language` (text).
- Save posts to `POST/PUT /v1/tts/profiles`.

This is new UI surface, not a refactor of a large existing file, so no extra decomposition beyond one panel + one JS module (consistent with `profiles.js` / `mcp-servers.js` sizing).

---

## 5. Tests

New (mirroring existing profile test files 1:1):
- `tests/unit/test_tts_profile_models.py` — defaults, field validation (mirror `test_profiles_models.py`)
- `tests/unit/test_tts_profile_store.py` — CRUD + atomic write (mirror `test_profiles_store.py`)
- `tests/unit/test_tts_profile_routes.py` — CRUD endpoints, 404 on missing (mirror `test_profiles_routes.py`)

Updated:
- `tests/unit/test_profiles_models.py` / `test_profiles_store.py` / `test_profiles_routes.py` — `TtsConfig` payloads change from `{engine, voice}` to `{profile_name}`.
- `tests/unit/test_conversation_profile.py` — resolution-precedence cases: query-param `tts_profile` override, profile-linked `tts_profile`, and the no-profile legacy fallback (including that clone/instruct/speed/language fields are `None` in the fallback path).

No changes needed to `test_tts_engines.py`, `test_tts_streaming.py`, `test_tts_models.py`, `test_tts_stream.py` — the TTS provider/service layer and its request schema (`TTSRequest`) are unchanged; only how callers *populate* a `TTSRequest` changes.

---

## 6. Out of Scope

- Uploading/recording reference audio through the UI (users provide a server-side `ref_audio_path` directly, same as OmniVoice's existing pinned-voice flow does today).
- Migrating any existing `profiles.json` data — none exists in this repo.
- Per-engine validation of which fields are meaningful (e.g. `instruct` is omnivoice-only, `voice` presets are vieneu-only) — the resolution layer passes through whatever is set; unsupported fields are simply ignored by providers that don't use them (already true of `TTSRequest` today).
