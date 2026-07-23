"""STT engine/language/model resolution for a conversation.

No preset layer, no per-request engine override: a profile names the engine,
language, and model variant directly, falling back to the server-wide
defaults. See docs/superpowers/specs/2026-07-23-system-settings-restructure-design.md
for why the query-param engine override was removed.
"""

from __future__ import annotations


def resolve_stt(
    profile: object | None,
    q_language: str | None = None,
    q_model: str | None = None,
) -> tuple[str, str | None, str]:
    """Resolve (engine, language|None, model) for a conversation.

    Single source of truth shared by the conversation WS stream and the /stt/warm
    endpoint so a device that only sends a profile id warms and streams against the
    same STT model. Priority, highest first:

      1. the chatllm profile's SttConfig (engine/language/model)
      2. the server-wide default (default_stt_engine / conversation_language);
         model has no server-wide default — "" means "whatever's currently active
         for the resolved engine".

    `profile` is a services.profiles Profile (or None); accessed duck-typed to avoid
    a circular import. language None means auto-detect.
    """
    from app.services.system_config import system_config_store

    stt_cfg = getattr(profile, "stt", None)
    conv_cfg = system_config_store.get().conversation
    engine = (
        (getattr(stt_cfg, "engine", "") or None)
        or system_config_store.get().engines.default_stt_engine
    )
    if q_language:
        language: str | None = q_language
    elif getattr(stt_cfg, "language", ""):
        language = stt_cfg.language
    else:
        language = conv_cfg.conversation_language or None
    model = q_model or (getattr(stt_cfg, "model", "") or "")
    return engine, language, model
