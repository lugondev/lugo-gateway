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


class UsernameTakenError(AppError):
    """Raised on signup/create-user when the username already exists."""

    status_code = 409


class PairingCodeInvalidError(AppError):
    """Raised when pair/claim is given an unknown or expired code."""

    status_code = 400


class DeviceSerialConflictError(AppError):
    """Raised when pair/claim's serial already has a non-revoked device."""

    status_code = 409


class ModelNotAllowedError(AppError):
    """Raised when a chosen (kind, engine, model_id) matches a registry entry
    that is disabled, or is stage=testing and the user lacks can_use_testing."""

    status_code = 403
