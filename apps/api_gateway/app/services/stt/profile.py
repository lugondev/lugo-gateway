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


def resolve_stt(
    profile: object | None,
    q_engine: str | None = None,
    q_language: str | None = None,
    q_model: str | None = None,
) -> tuple[str, str | None, str]:
    """Resolve (engine, language|None, model) for a conversation.

    Single source of truth shared by the conversation WS stream and the /stt/warm
    endpoint so a device that only sends a profile id warms and streams against the
    same STT model. Priority, highest first:

      1. explicit query param (stt_engine / language / stt_model) — debugging / manual override
      2. the chatllm profile's SttConfig (engine/language/model, or a language preset)
      3. the server-wide default (settings.stt_profile preset, then
         conversation_stt_engine / conversation_language); model has no server-wide
         default — "" means "whatever's currently active for the resolved engine".

    `profile` is a services.profiles Profile (or None); accessed duck-typed to avoid
    a circular import. A language preset (vi|en|multi|en_vi) sets engine+language
    together; language None means auto-detect and is authoritative when a preset
    resolves (it is not overridden by conversation_language). model is independent
    of the preset system — a preset never implies a model variant.
    """
    from app.core.settings import settings

    stt_cfg = getattr(profile, "stt", None)
    preset_name = (getattr(stt_cfg, "profile", "") or "") or settings.stt_profile
    preset = resolve_stt_profile(preset_name)
    preset_engine, preset_lang = preset if preset else (None, None)

    engine = (
        q_engine
        or (getattr(stt_cfg, "engine", "") or None)
        or preset_engine
        or settings.conversation_stt_engine
        or settings.default_stt_engine
    )
    if q_language:
        language: str | None = q_language
    elif getattr(stt_cfg, "language", ""):
        language = stt_cfg.language
    elif preset:
        language = preset_lang  # may be None (auto-detect) — authoritative
    else:
        language = settings.conversation_language or None
    model = q_model or (getattr(stt_cfg, "model", "") or "")
    return engine, language, model
