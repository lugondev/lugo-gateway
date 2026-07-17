"""Env -> validated ServiceConfig for the standalone model service.

Everything is validated at startup rather than on first request: a container
with a typo'd SERVICE_ENGINE should fail its healthcheck immediately, not 30
minutes later when the first audio arrives.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

VALID_KINDS = ("stt", "tts")


class ConfigError(RuntimeError):
    """Env is missing or invalid; the process must not start."""


@dataclass(frozen=True)
class ServiceConfig:
    kind: str
    engine: str
    api_token: str
    port: int = 8100


def load_config(env: Mapping[str, str] | None = None) -> ServiceConfig:
    env = os.environ if env is None else env

    kind = env.get("SERVICE_KIND", "").strip().lower()
    if kind not in VALID_KINDS:
        raise ConfigError(f"SERVICE_KIND must be one of {VALID_KINDS!r}, got {kind!r}")

    engine = env.get("SERVICE_ENGINE", "").strip()
    if not engine:
        raise ConfigError("SERVICE_ENGINE is required (e.g. whisper_local, vieneu)")

    api_token = env.get("SERVICE_API_TOKEN", "").strip()
    if not api_token:
        raise ConfigError("SERVICE_API_TOKEN is required; the service refuses to run open")

    try:
        port = int(env.get("SERVICE_PORT", "8100"))
    except ValueError as exc:
        raise ConfigError(f"SERVICE_PORT must be an integer: {exc}") from exc

    return ServiceConfig(kind=kind, engine=engine, api_token=api_token, port=port)
