from app.core.errors import EngineNotFoundError
from app.core.settings import settings
from app.services.system_config import system_config_store
from app.services.tts.base import TTSProvider
from app.services.tts.providers.edge_tts_provider import EdgeTTSProvider
from app.services.tts.providers.extra_engines import EXTRA_TTS_PROVIDERS
from app.services.tts.providers.omnivoice_provider import OmniVoiceProvider
from app.services.tts.providers.http_tts_provider import HttpTtsProvider
from app.services.tts.providers.qwen3_tts_provider import QWEN3_TTS_PROVIDERS
from app.services.tts.providers.vieneu_provider import VieNeuProvider


class TTSService:
    def __init__(self) -> None:
        self.providers: dict[str, TTSProvider] = {
            "omnivoice": OmniVoiceProvider(),
            "vieneu": VieNeuProvider(),
            "edge_tts": EdgeTTSProvider(),
            "http_tts": HttpTtsProvider(),
        }
        for provider in EXTRA_TTS_PROVIDERS:
            self.providers[provider.name] = provider
        for provider in QWEN3_TTS_PROVIDERS:
            self.providers[provider.name] = provider

    def get_provider(self, engine: str) -> TTSProvider:
        provider = self.providers.get(engine)
        if provider is None:
            raise EngineNotFoundError(f"Unsupported TTS engine: {engine}")
        return provider

    def list_engines(self) -> list[dict]:
        result: list[dict] = []
        default_engine = system_config_store.get().engines.default_tts_engine
        for name, provider in self.providers.items():
            available = provider.available()
            try:
                detail = provider.detail()
            except Exception as exc:  # noqa: BLE001 -- one bad engine must not 500 the whole list
                detail = f"detail() failed: {exc}"
            # "remote" = calls out to an OpenAI-compatible HTTP service (http_tts);
            # everything else runs in-process ("local"). Mirrors _SERVICE_TTS_ENGINES
            # in routes/model_registry.py and the `mode` field STT's list_engines emits.
            mode = "remote" if name == "http_tts" else "local"
            result.append(
                {
                    "engine": name,
                    "available": available,
                    "detail": detail,
                    "install_hint": provider.install_hint(),
                    "install_package": provider.install_package,
                    "install_enabled": settings.allow_runtime_install,
                    "default": name == default_engine,
                    "mode": mode,
                }
            )
        return result


tts_service = TTSService()
