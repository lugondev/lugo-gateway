"""Fast-path STT engine routing.

FunASR ships fast non-autoregressive models (SenseVoice/Paraformer) that beat
autoregressive Whisper on short utterances. The same idea applies here: route a
short command to a low-latency engine and keep the accurate engine for longer,
harder utterances. This module holds only the (pure, testable) decision; the
caller resolves and falls back on provider availability.
"""


def select_stt_engine(
    speech_ms: float,
    default_engine: str,
    fast_engine: str = "",
    fast_max_ms: int = 0,
    available: set[str] | None = None,
) -> str:
    """Pick the STT engine for an utterance of ``speech_ms`` milliseconds.

    Returns ``fast_engine`` when it is configured, differs from the default, the
    utterance is at/under ``fast_max_ms``, and the engine is available; otherwise
    ``default_engine``. ``fast_max_ms<=0`` or an empty ``fast_engine`` disables
    the fast path.
    """
    if (
        fast_engine
        and fast_engine != default_engine
        and fast_max_ms > 0
        and speech_ms <= fast_max_ms
        and (available is None or fast_engine in available)
    ):
        return fast_engine
    return default_engine
