from abc import ABC, abstractmethod

from app.schemas.tts import TTSRequest, TTSResult


class TTSProvider(ABC):
    name: str

    @abstractmethod
    async def synthesize(self, payload: TTSRequest) -> TTSResult:
        raise NotImplementedError

    def available(self) -> bool:
        """Whether the engine can run real synthesis (deps/binaries present)."""
        return True

    def detail(self) -> str:
        """Short model/version label for display."""
        return self.name

    def install_hint(self) -> str:
        """How to enable this engine when it isn't available; empty if built-in."""
        return ""
