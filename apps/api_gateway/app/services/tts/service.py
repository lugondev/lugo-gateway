from app.core.errors import EngineNotFoundError
from app.services.system_config import system_config_store
from app.services.tts.base import TTSProvider
from app.services.tts.providers.edge_tts_provider import EdgeTTSProvider
from app.services.tts.providers.extra_engines import EXTRA_TTS_PROVIDERS
from app.services.tts.providers.omnivoice_provider import OmniVoiceProvider
from app.services.tts.providers.vieneu_provider import VieNeuProvider


class TTSService:
    def __init__(self) -> None:
        self.providers: dict[str, TTSProvider] = {
            "omnivoice": OmniVoiceProvider(),
            "vieneu": VieNeuProvider(),
            "edge_tts": EdgeTTSProvider(),
        }
        for provider in EXTRA_TTS_PROVIDERS:
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
            result.append(
                {
                    "engine": name,
                    "available": provider.available(),
                    "detail": provider.detail(),
                    "install_hint": provider.install_hint(),
                    "default": name == default_engine,
                }
            )
        return result


tts_service = TTSService()
