class AppError(Exception):
    """Base class for domain errors raised by services."""

    status_code: int = 400


class EngineNotFoundError(AppError):
    """Raised when an unknown STT/TTS engine is requested."""

    status_code = 400


class ProviderError(AppError):
    """Raised when a provider fails to process a request (config/runtime)."""

    status_code = 502
