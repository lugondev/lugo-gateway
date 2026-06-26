import os
from pathlib import Path

from app.core.deps import module_available
from app.core.errors import EngineNotFoundError
from app.core.settings import settings
from app.services.stt.base import STTProvider
from app.services.stt.providers.remote_whisper_provider import RemoteWhisperProvider
from app.services.stt.providers.vosk_provider import VoskProvider
from app.services.stt.providers.whisper_provider import WhisperProvider


class STTService:
    def __init__(self) -> None:
        whisper_local = WhisperProvider()
        self.providers: dict[str, STTProvider] = {
            "vosk": VoskProvider(),
            "whisper": whisper_local,
            "whisper_local": whisper_local,
            "whisper_service": RemoteWhisperProvider(
                name="whisper_service",
                base_url=settings.whisper_service_base_url,
                api_key=settings.whisper_service_api_key,
                model=settings.whisper_service_model,
                timeout_seconds=settings.remote_stt_timeout_seconds,
            ),
            "eventlab": RemoteWhisperProvider(
                name="eventlab",
                base_url=settings.eventlab_base_url,
                api_key=settings.eventlab_api_key,
                model=settings.eventlab_model,
                timeout_seconds=settings.remote_stt_timeout_seconds,
            ),
        }

    def get_provider(self, engine: str) -> STTProvider:
        provider = self.providers.get(engine)
        if provider is None:
            raise EngineNotFoundError(f"Unsupported STT engine: {engine}")
        return provider

    def list_engines(self) -> list[dict]:
        # Lazy import to avoid any module load-order coupling.
        from app.services.stt.providers.vosk_provider import get_active_vosk_path
        from app.services.whisper_models import whisper_manager

        active_vosk_path = get_active_vosk_path()
        vosk_present = module_available("vosk") and os.path.isdir(active_vosk_path)
        vosk_detail = Path(active_vosk_path).name if vosk_present else None

        fw_available = module_available("faster_whisper")
        active_whisper = whisper_manager.snapshot()["active"]
        whisper_cached = whisper_manager._cached(active_whisper)
        whisper_detail = active_whisper + (" · cached" if whisper_cached else " · downloads on first use")

        remote = {
            "whisper_service": (settings.whisper_service_base_url, settings.whisper_service_model),
            "eventlab": (settings.eventlab_base_url, settings.eventlab_model),
        }

        result: list[dict] = []
        seen_providers: set[int] = set()
        for engine, provider in self.providers.items():
            # Skip alias keys that point to an already-listed provider (e.g. whisper_local).
            if id(provider) in seen_providers:
                continue
            seen_providers.add(id(provider))

            # Realtime = the provider implements native incremental streaming
            # (overrides open_stream); buffering engines return a final only on stop.
            realtime = type(provider).open_stream is not STTProvider.open_stream

            if engine == "vosk":
                entry = {"mode": "local", "available": vosk_present, "detail": vosk_detail}
            elif engine in ("whisper", "whisper_local"):
                entry = {"mode": "local", "available": fw_available, "detail": whisper_detail}
            else:
                base_url, model = remote[engine]
                configured = bool(base_url)
                entry = {"mode": "remote", "available": configured, "detail": model if configured else None}

            result.append(
                {"engine": engine, "configured": entry["available"], "realtime": realtime, **entry}
            )
        return result


stt_service = STTService()
