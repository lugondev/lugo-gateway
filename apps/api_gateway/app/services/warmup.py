import asyncio
import logging

from app.services.tts.base import TTSProvider

logger = logging.getLogger(__name__)

# Providers whose warm() has completed (or attempted and finished, even if it
# failed — either way there's nothing left to wait for). Keyed by id() since
# providers are long-lived module-level singletons for the life of the process.
_ready_ids: set[int] = set()

# TTSProvider.warm() defaults to a no-op for engines with nothing to preload
# (e.g. remote APIs) — those must never be reported as "cold".
_NOOP_TTS_WARM = TTSProvider.warm


def _needs_warming(provider: object) -> bool:
    warm = getattr(provider, "warm", None)
    if not callable(warm):
        return False
    return getattr(warm, "__func__", warm) is not _NOOP_TTS_WARM


def is_ready(provider: object) -> bool:
    """True once a provider is safe to use without paying a cold-load delay.

    A provider with no real warm() (missing entirely, or just the inherited
    TTSProvider no-op) has no model to load, so it's always ready. One with a
    real warm() is ready only after warm_providers() has run it at least once —
    lets callers (e.g. the conversation WS) tell a connecting client whether an
    engine is still loading, instead of the client finding out by way of the
    first turn taking a long time / losing the start of its audio.
    """
    if not _needs_warming(provider):
        return True
    return id(provider) in _ready_ids


async def warm_providers(*providers: object) -> None:
    """Best-effort warm each provider's model, off the event loop.

    A provider with no real warm() is skipped; a failure on one provider
    doesn't stop the others from warming. Readiness is recorded whether or not
    the warm attempt succeeded, since either way the attempt is done.
    """
    for provider in providers:
        if not _needs_warming(provider):
            continue
        try:
            await asyncio.to_thread(provider.warm)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s warm failed: %s", type(provider).__name__, exc)
        finally:
            _ready_ids.add(id(provider))
