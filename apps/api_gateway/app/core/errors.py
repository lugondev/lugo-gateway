class AppError(Exception):
    """Base class for domain errors raised by services."""

    status_code: int = 400


class EngineNotFoundError(AppError):
    """Raised when an unknown STT/TTS engine is requested."""

    status_code = 400


class RuntimeInstallDisabledError(AppError):
    """Raised when the runtime pip-install endpoint is hit while disabled."""

    status_code = 403


class ProviderError(AppError):
    """Raised when a provider fails to process a request (config/runtime)."""

    status_code = 502


class LLMUnavailableError(AppError):
    """Raised when a configured conversation LLM is unreachable/offline."""

    status_code = 503


class AuthError(AppError):
    """Raised when login credentials are invalid or a session is required."""

    status_code = 401
