"""Wire the recommender to live state: detect capabilities, gather installed ids and
config flags from the existing managers, then rank the catalog per category.

Defensive throughout — a manager that errors degrades to "nothing installed" for its
category rather than failing the whole endpoint.
"""

from app.core.settings import settings
from app.services.recommend.capabilities import Capabilities, detect_capabilities
from app.services.recommend.catalog import CANDIDATES
from app.services.recommend.recommender import rank
from app.services.system_config import system_config_store


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _collect_state() -> tuple[set, set]:
    """Ids the managers report as installed and as active (matches catalog ids)."""
    installed: set = set()
    active: set = set()

    def whisper() -> None:
        from app.services.whisper_models import whisper_manager

        for m in whisper_manager.snapshot().get("models", []):
            if m.get("cached"):
                installed.add(m.get("size"))
            if m.get("active"):
                active.add(m.get("size"))

    def vosk() -> None:
        from app.services.models import model_manager

        for m in model_manager.snapshot().get("installed", []):
            installed.add(m.get("name"))
            if m.get("active"):
                active.add(m.get("name"))

    def tts() -> None:
        from app.services.tts_models import tts_model_manager

        snap = tts_model_manager.snapshot()
        omni = snap.get("omnivoice", {})
        for m in omni.get("models", []):
            if m.get("cached"):
                installed.add(m.get("id"))
            if m.get("active"):
                active.add(m.get("id"))
        active.add(omni.get("active"))
        vieneu = snap.get("vieneu", {})
        for m in vieneu.get("modes", vieneu.get("models", [])):
            mid = m.get("mode") or m.get("id")
            if m.get("cached") or m.get("installed"):
                installed.add(mid)
            if m.get("active"):
                active.add(mid)
        active.add(vieneu.get("active"))

    def llm() -> None:
        from app.services.llm_models import llm_manager

        snap = llm_manager.snapshot()
        for m in snap.get("installed", []):
            installed.add(m.get("model"))
            if m.get("active"):
                active.add(m.get("model"))
        active.add(snap.get("active"))

    for fn in (whisper, vosk, tts, llm):
        _safe(fn, None)

    # Built-in VAD is always available.
    installed.add("energy")
    installed.discard(None)
    active.discard(None)
    return installed, active


def _augment_config_flags(caps: Capabilities) -> None:
    """Remote/online entries are 'available' when their endpoint is configured."""
    remote_stt = system_config_store.get().remote_stt
    caps.modules["whisper_service"] = bool(remote_stt.whisper_service_base_url)
    caps.modules["eventlab"] = bool(remote_stt.eventlab_base_url)
    caps.modules["online_llm"] = bool(system_config_store.get().conversation_llm.conversation_llm_base_url)
    caps.modules["openrouter"] = bool(system_config_store.get().openrouter_api_key)


def recommend_all() -> dict:
    caps = detect_capabilities()
    _augment_config_flags(caps)
    installed, active = _collect_state()

    categories: dict = {"stt": [], "tts": [], "llm": [], "vad": []}
    for cat in categories:
        members = [c for c in CANDIDATES if c.category == cat]
        categories[cat] = rank(members, caps, installed, active)

    # Mark items whose only blocker is an installable pip package (needs:<pkg> where
    # <pkg> is in the allowlist) so the UI can offer a one-click Install when enabled.
    from app.services.install_manager import ALLOWLIST

    enabled = settings.allow_runtime_install
    for items in categories.values():
        for it in items:
            pkg = it["status"].split(":", 1)[1] if it["status"].startswith("needs:") else None
            installable_pkg = pkg if pkg in ALLOWLIST else None
            it["install_package"] = installable_pkg
            it["installable"] = bool(enabled and installable_pkg)

    return {
        "capabilities": caps.as_dict(),
        "categories": categories,
        "install_enabled": settings.allow_runtime_install,
    }
