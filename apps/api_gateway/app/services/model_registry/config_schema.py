"""Describe a (kind, engine)'s config fields for the admin UI's Config form.

Single source of truth: the field lists come from the exact same
STT_ENGINE_CONFIG_DEFAULTS / OmnivoiceConfig the resolvers read, so the form can
never drift from what the backend actually honors. Types are inferred from each
default's Python type -- bool is checked before int because bool is an int
subclass. Engines with no known schema (llm, plain tts) return [] and the UI
falls back to raw-JSON editing.
"""

from __future__ import annotations

from app.services.model_registry.resolve import STT_ENGINE_CONFIG_DEFAULTS
from app.services.system_config import OmnivoiceConfig

# Remote STT/TTS providers read exactly one config key.
_REMOTE_ENGINES = {"openai_stt", "openai_tts", "whisper_service", "eventlab"}


def _type_of(value) -> str:
    if isinstance(value, bool):  # before int -- bool is a subclass of int
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def _fields_from_defaults(defaults: dict) -> list[dict]:
    return [{"key": k, "type": _type_of(v), "default": v} for k, v in defaults.items()]


def config_schema_for(kind: str, engine: str) -> list[dict]:
    if engine in STT_ENGINE_CONFIG_DEFAULTS:
        return _fields_from_defaults(STT_ENGINE_CONFIG_DEFAULTS[engine])
    if engine in _REMOTE_ENGINES:
        return [{"key": "timeout_seconds", "type": "float", "default": 60.0}]
    if engine == "omnivoice":
        return _fields_from_defaults(OmnivoiceConfig().model_dump())
    return []
