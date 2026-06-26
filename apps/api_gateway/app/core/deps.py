"""Optional-dependency probing shared across services."""

import importlib.util


def module_available(module: str) -> bool:
    """True if an importable module exists, without importing it.

    Tolerates a missing parent package (find_spec raises ModuleNotFoundError
    for dotted names like ``pyannote.audio`` when ``pyannote`` is absent).
    """
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False
