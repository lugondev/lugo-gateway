from typing import Literal

from pydantic import BaseModel

# NOTE: ref_audio_path is deliberately NOT validated here (see
# 2026-07-28-critical-authz-fixes task-6-fixes-round-2 finding "NEW
# (Important)"). This model is deserialized from STORAGE
# (SqliteBackedStore._ensure(), config_store.py) as well as constructed from
# request bodies. A field_validator here rejects a row at LOAD time, not just
# save time -- one legacy/host-relative-mismatched row would then raise
# uncaught out of `_ensure()`'s single dict comprehension (no per-row guard)
# and permanently break `list()`/`get()` for every OTHER profile too, since
# `_ensure()` leaves `self._cache = None` on failure and retries (and fails)
# on every subsequent call. Confirmed against the live DB: three rows store
# the absolute path `/Users/lugon/.../artifacts/refs/*.wav`, which only
# satisfies containment when the process's CWD is exactly that repo root --
# any deployment-root change (container path, restored DB on a different
# host, this worktree) flips those rows from valid to store-bricking.
#
# The actual security boundary is the READ, not the save: TTSRequest.ref_audio_path
# (schemas/tts.py) is what's fed into Path(...).read_bytes() by the six TTS
# providers, and it validates every request-time construction regardless of
# where the value came from. Save-time rejection (a nicer 422 UX so a bad
# value never reaches storage in the first place) is enforced instead in the
# routes -- see api/routes/tts_profiles.py's create_tts_profile/
# update_tts_profile -- where it can 422 a bad NEW value without being
# reachable by model_validate_json() on an EXISTING stored row.


class TtsProfile(BaseModel):
    name: str
    owner_id: str | None = None
    engine: str = ""
    model_id: str = ""         # registry row id within the engine (see TTSRequest.model_id)
    voice_mode: Literal["preset", "clone"] = "preset"
    voice: str = ""            # preset mode: voice id from GET /v1/tts/voices?engine=
    ref_audio_path: str = ""   # clone mode
    ref_text: str = ""         # clone mode: transcript of the reference audio
    instruct: str = ""         # style/emotion instruction (engine-dependent, e.g. omnivoice)
    speed: float | None = None
    language: str | None = None
