from app.core.errors import EngineNotFoundError
from app.core.settings import settings
from app.services.tts.base import TTSProvider
from app.services.tts.providers.extra_engines import EXTRA_TTS_PROVIDERS
from app.services.tts.providers.omnivoice_provider import OmniVoiceProvider
from app.services.tts.providers.vieneu_provider import VieNeuProvider


class TTSService:
    def __init__(self) -> None:
        self.providers: dict[str, TTSProvider] = {
            "omnivoice": OmniVoiceProvider(),
            "vieneu": VieNeuProvider(),
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
        for name, provider in self.providers.items():
            result.append(
                {
                    "engine": name,
                    "available": provider.available(),
                    "detail": provider.detail(),
                    "install_hint": provider.install_hint(),
                    "default": name == settings.default_tts_engine,
                }
            )
        return result


tts_service = TTSService()
