"""Language presets for STT engine + language selection.

A single profile maps a language need to a concrete (engine, language) pair, so
callers don't have to wire engine and language hint separately:

  vi     -> qwen3_asr, forced Vietnamese                    — beat PhoWhisper on FLEURS vi (see benchmark)
  en     -> qwen3_asr, forced English                       — strong EN
  multi  -> qwen3_asr, auto-detect                          — 30-language + language ID
  en_vi  -> qwen3_asr, auto-detect                          — one model handles EN/VI code-switching

language is None where the engine should auto-detect. resolve_stt_profile returns
None for an unknown/empty profile (caller keeps its own defaults). See
[qwen3-asr size docs] — swap size via QWEN3_ASR_MODEL / set_active_qwen3_asr_model.
"""

# (engine, language | None)  — None means auto-detect.
STT_PROFILES: dict[str, tuple[str, str | None]] = {
    "vi": ("qwen3_asr", "vi"),
    "en": ("qwen3_asr", "en"),
    "multi": ("qwen3_asr", None),
    "en_vi": ("qwen3_asr", None),
}


def resolve_stt_profile(profile: str | None) -> tuple[str, str | None] | None:
    """Return (engine, language) for a profile name, or None if unknown/empty."""
    return STT_PROFILES.get((profile or "").strip().lower())
