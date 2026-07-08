import pytest

from app.core.settings import settings


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Keep tests independent of the developer's .env / running services:
    the built-in echo responder, and never spawn the OmniVoice sidecar or
    call a real LLM.
    """
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "omnivoice_use_server", False)
    # Don't load real STT/TTS models when TestClient(app) runs the app lifespan.
    monkeypatch.setattr(settings, "warmup_on_startup", False)

    # warmup._ready_ids tracks readiness by id(provider), which is safe in
    # production (providers are long-lived process-wide singletons) but not
    # across a test run: many tests register short-lived stub provider
    # instances, and CPython can reuse a freed object's id() for a later one,
    # making an unrelated test's provider spuriously read back as "ready".
    # Reset it per test so no test starts with another test's stale ids.
    from app.services import warmup as warmup_module

    monkeypatch.setattr(warmup_module, "_ready_ids", set())


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """Point the DB engine at a per-test tmp file so no test ever touches
    the real data/app.db, even tests that only indirectly hit routes
    backed by the DB (chat/session/memory endpoints, conversation WS).

    Also repoint the legacy config JSON paths (profiles/tts_profiles/
    mcp_servers/system_config) at nonexistent tmp files. Without this, the
    config-store singletons (app.services.profiles.store.profile_store etc.)
    fall back to settings.*_path's real default -- e.g. "profiles.json",
    resolved against the repo root -- and any test that reaches an unpatched
    store can trigger a legacy import against the real file. The stores
    re-read these settings attributes lazily (at `_ensure()` time, see
    app/services/db/config_store.py), so patching them here, before any
    store method runs, is enough to redirect even the module-level
    singletons that were already constructed at import time.
    """
    from app.services.db import engine as db_engine
    from app.services.db import sync_engine as cfg_engine
    from app.core.settings import settings

    monkeypatch.setattr(settings, "profiles_path", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(settings, "tts_profiles_path", str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr(settings, "mcp_servers_path", str(tmp_path / "mcp_servers.json"))
    monkeypatch.setattr(settings, "system_config_path", str(tmp_path / "system_config.json"))
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    cfg_engine.configure(f"sqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()
    cfg_engine.configure()
