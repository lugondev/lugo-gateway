# STT model selection per profile

## Problem

STT **engine** and **language** are already profile-aware (`SttConfig.engine`/`.language`/`.profile` in `apps/api_gateway/app/services/profiles/models.py`, resolved by `app/services/stt/profile.py:resolve_stt()`). STT **model variant** (e.g. PhoWhisper-medium vs tiny, Qwen3-ASR 0.6B vs 1.7B) is not: it's a single process-global runtime toggle (`WhisperManager.select()` / `set_active_qwen3_asr_model()`), set once for the whole server regardless of which profile a client is using.

Clients should only ever pick a profile; the profile should carry the full STT config, including which model variant to use — mirroring how TTS already works (`TtsConfig.profile_name` → a `TtsProfile`).

## Scope

Model-variant selection is added for the two engines that already have a variant concept:

- **whisper family** — `whisper` / `whisper_local` / `whisper_gemma` (all three read the same underlying `whisper_manager` active model), variants from `WHISPER_SIZES` (`app/services/whisper_models.py`).
- **`qwen3_asr`** — variants from `QWEN3_ASR_MODELS` (`app/services/stt/providers/qwen3_asr_provider.py`).

`whisper_mlx`, `vosk`, `whisper_service`, `eventlab` each pin one fixed model (settings-configured) and are out of scope — attempting to set `stt.model` for a profile resolving to one of these is a validation error.

Out of scope: concurrent multi-model loading. The active model per engine remains a single process-global slot (as today); this design adds profile-driven *selection* and *eager swap+warm* of that slot, not concurrent hosting of multiple models at once.

## Data model

Add one field to `SttConfig` (`app/services/profiles/models.py`):

```python
class SttConfig(BaseModel):
    profile: str = ""
    engine: str = ""
    language: str = ""
    model: str = ""   # NEW — model variant id for engines with a registry; "" = inherit whatever's currently active
```

`""` is fully backward compatible: every existing profile keeps today's behavior (whatever model happens to be active).

## Model registry

A small common shape so whisper-family and qwen3_asr expose the same interface:

```python
class SttModelRegistry(Protocol):
    def list_models(self) -> list[dict]     # [{id, label, cached, active}]
    def validate(self, model_id: str) -> None    # raises AppError if unknown id
    def select(self, model_id: str) -> None      # switches the process-global active model
```

- `whisper_manager` (`app/services/whisper_models.py`) already implements this shape (`snapshot()` → adapt to `list_models()`, plus existing `validate()`/`select()`). No changes needed beyond a thin adapter.
- New `Qwen3AsrModelRegistry` wrapping `QWEN3_ASR_MODELS` + `set_active_qwen3_asr_model()`. `cached` is computed the same way whisper does — presence of the model's snapshot dir under the shared HF hub cache (`app/core/hf_cache.py:hub_dir()`), for `available` consistency between engines.
- Module-level lookup:

```python
STT_MODEL_REGISTRIES: dict[str, SttModelRegistry] = {
    "whisper": whisper_manager,
    "whisper_local": whisper_manager,
    "whisper_gemma": whisper_manager,
    "qwen3_asr": qwen3_asr_registry,
}
```

Engines absent from this dict have no variant support.

## New endpoint: `GET /v1/stt/models?engine=`

Mirrors the existing `GET /v1/tts/voices?engine=` pattern. Response:

```json
{
  "success": true,
  "data": {
    "engine": "qwen3_asr",
    "supports_variants": true,
    "models": [
      {"id": "0.6b", "label": "Qwen3-ASR 0.6B (fast)", "valid": true, "available": true, "active": false},
      {"id": "1.7b", "label": "Qwen3-ASR 1.7B (accurate, multilingual)", "valid": true, "available": false, "active": true}
    ]
  }
}
```

For engines with no registry: `{"engine": "vosk", "supports_variants": false, "models": []}`.

This is what a profile-editing UI calls to populate the model dropdown for the currently-selected engine, distinguishing "valid" (known to the engine) from "available" (already cached on *this* server) — a model can be selected even if not yet cached; selecting it will trigger a download on first warm, same as today's admin whisper-download flow.

## Resolution: `resolve_stt()`

`app/services/stt/profile.py:resolve_stt()` gains a third return value:

```python
def resolve_stt(
    profile: object | None,
    q_engine: str | None = None,
    q_language: str | None = None,
    q_model: str | None = None,
) -> tuple[str, str | None, str]:   # (engine, language, model)
```

`model` resolution priority: explicit query override (debugging) → `profile.stt.model` → `""` (no override — inherit whatever's active). No server-wide default-model setting is introduced; `""` already means "whatever `get_active_whisper_model()`/`get_active_qwen3_asr_model()` currently returns," consistent with existing behavior.

All three callers of `resolve_stt()` (conversation WS stream, `/stt/warm`, boot warmup) are updated to thread the third value through.

## Eager swap + warm (the "like TTS" behavior)

This is the mechanism that makes model selection actually take effect without a cold-load surprise, matching how TTS already behaves for its profile.

**Session start** (`app/services/conversation/session.py`, existing `_warm_and_notify()` around line 295): after resolving `(engine, language, model)` for the session's profile, if `model` is non-empty and `engine` has a registry entry, call `registry.select(model)` **before** `warm_providers(self.tts_provider, self.stt_provider)`. This both switches the process-global active model to what the profile wants and warms it, before the client's first turn — no manual client-side model management needed.

**Boot warmup** (`app/services/warmup.py:engines_for_boot_warmup()`): extended to also resolve each profile's model and warm it at server start, same enumeration loop that already exists for engines. Known limitation (not a new regression — this constraint already exists today for engine-level boot warmup): since the active model is a single process-global slot per engine, if multiple profiles want different models on the same engine, boot warmup can only pre-warm one variant per engine (first profile enumerated wins). The session-start swap-on-use step above is the authoritative correctness mechanism per session; boot warmup is just a best-effort head start to avoid the *common* cold-load case.

## Validation

On profile create/update (`POST`/`PUT /v1/profiles`): if `stt.model` is set, resolve the effective engine (explicit `stt.engine`, else the `stt.profile` preset via `resolve_stt_profile()`), require it to have a registry entry (400 `AppError` if not — "engine X has no selectable model variants"), and call `registry.validate(model)` (400 if the id isn't in that engine's known list). "Available"/cached status is informational only from `/v1/stt/models` and does not block saving.

## Testing

- `resolve_stt()` unit tests: model resolution priority (query > profile > default), unchanged engine/language behavior for existing profiles (`model=""`).
- `Qwen3AsrModelRegistry`: validate/select/list_models, cached-detection reuses the same hub-cache pattern as whisper (parametrize or share a helper with whisper's cache-dir test if one exists).
- Profile CRUD: saving a profile with a valid model succeeds; invalid model id rejected (400); model set on an engine without a registry rejected (400); `model=""` always allowed regardless of engine.
- `GET /v1/stt/models?engine=`: known engine returns valid+available flags; unknown/no-registry engine returns `supports_variants: false, models: []`.
- Session start: with a profile specifying a non-default model, assert `registry.select()` is called with that model before `warm_providers()` runs (mock/spy on the registry).
- Boot warmup: profile with a model set is included in the warm set; two profiles with conflicting models on the same engine don't crash boot (last/first wins per enumeration order — documented, not asserted as "correct" since there is no single correct answer).
