"""Wire the recommender to live state: detect capabilities, gather installed ids and
config flags from the existing managers, then rank the catalog per category.

Defensive throughout — a manager that errors degrades to "nothing installed" for its
category rather than failing the whole endpoint.
"""

from app.core.settings import settings
from app.services.recommend.capabilities import Capabilities, detect_capabilities
from app.services.recommend.catalog import CANDIDATES
from app.services.recommend.recommender import rank


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _installed_ids() -> set:
    """Ids the managers report as cached/installed (matches catalog ids)."""
    ids: set = set()

    def whisper() -> None:
        from app.services.whisper_models import whisper_manager

        for m in whisper_manager.snapshot().get("models", []):
            if m.get("cached"):
                ids.add(m.get("size"))

    def vosk() -> None:
        from app.services.models import model_manager

        for m in model_manager.snapshot().get("installed", []):
            ids.add(m.get("name"))

    def tts() -> None:
        from app.services.tts_models import tts_model_manager

        snap = tts_model_manager.snapshot()
        for m in snap.get("omnivoice", {}).get("models", []):
            if m.get("cached"):
                ids.add(m.get("id"))
        vieneu = snap.get("vieneu", {})
        for m in vieneu.get("modes", vieneu.get("models", [])):
            if m.get("cached") or m.get("installed"):
                ids.add(m.get("mode") or m.get("id"))

    def llm() -> None:
        from app.services.llm_models import llm_manager

        snap = llm_manager.snapshot()
        for m in snap.get("installed", []):
            ids.add(m.get("model"))

    def qwen() -> None:
        from app.services.qwen_omni_models import qwen_omni_manager

        for m in qwen_omni_manager.snapshot().get("models", []):
            if m.get("cached"):
                ids.add(m.get("model"))

    for fn in (whisper, vosk, tts, llm, qwen):
        _safe(fn, None)

    # Built-in VAD is always available.
    ids.add("energy")
    ids.discard(None)
    return ids


def _augment_config_flags(caps: Capabilities) -> None:
    """Remote/online entries are 'available' when their endpoint is configured."""
    caps.modules["whisper_service"] = bool(settings.whisper_service_base_url)
    caps.modules["eventlab"] = bool(settings.eventlab_base_url)
    caps.modules["online_llm"] = bool(settings.conversation_llm_base_url)


def recommend_all() -> dict:
    caps = detect_capabilities()
    _augment_config_flags(caps)
    installed = _installed_ids()

    categories: dict = {"stt": [], "tts": [], "llm": [], "vad": []}
    for cat in categories:
        members = [c for c in CANDIDATES if c.category == cat]
        categories[cat] = rank(members, caps, installed)

    return {"capabilities": caps.as_dict(), "categories": categories}
