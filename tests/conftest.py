import pytest


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Keep tests independent of the developer's .env / running services:
    the built-in echo responder, and never spawn the OmniVoice sidecar or
    call a real LLM.
    """
    # Don't load real STT/TTS models when TestClient(app) runs the app lifespan.
    # warmup_on_startup, conversation_llm_base_url, and omnivoice_use_server now
    # live on system_config_store (Task 2 / Task 3 / Task 7), not Settings.
    # Patch the *instance method* (not `.set()`/the DB row) so this never writes
    # through to the shared config_system DB row -- system_config_store is a
    # true singleton shared by every test in the run (and by any test that
    # builds its own SystemConfigStore pointed at the same test DB, since the
    # row is keyed by a fixed id, not by path), so a `.set()` write here would
    # leak into and corrupt unrelated tests' expectations.
    from app.services.system_config import system_config_store

    _real_get = system_config_store.get

    def _get_with_warmup_off():
        cfg = _real_get()
        return cfg.model_copy(update={
            "engines": cfg.engines.model_copy(update={"warmup_on_startup": False}),
            "conversation_llm": cfg.conversation_llm.model_copy(update={"conversation_llm_base_url": ""}),
            "omnivoice": cfg.omnivoice.model_copy(update={"omnivoice_use_server": False}),
        })

    monkeypatch.setattr(system_config_store, "get", _get_with_warmup_off)

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
