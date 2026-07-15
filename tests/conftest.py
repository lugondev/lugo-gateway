import pytest


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Keep tests independent of the developer's .env / running services:
    the built-in echo responder, and never spawn the OmniVoice sidecar or
    call a real LLM.
    """
    # Don't load real STT/TTS models when TestClient(app) runs the app lifespan.
    # warmup_on_startup lives on system_config_store (Task 2), not Settings.
    # omnivoice_use_server and the conversation LLM's base_url now live in
    # Model Registry entries (`kind="tts"`/`kind="llm"`), not SystemConfig
    # (Task 7 removed the `omnivoice` group entirely) -- no override needed
    # here for either since each test's fresh tmp DB (see `_tmp_db` below)
    # starts with zero registry entries; tests that exercise the real
    # OmniVoice sidecar path mock it explicitly (see test_omnivoice_provider.py).
    # Patch the *instance method* (not `.set()`/the DB row) so this never writes
    # through to the shared config_system DB row -- system_config_store is a
    # true singleton shared by every test in the run (and by any test that
    # builds its own SystemConfigStore pointed at the same test DB, since the
    # row is keyed by a fixed id, not by path), so a `.set()` write here would
    # leak into and corrupt unrelated tests' expectations.
    # Auth must default to disabled regardless of the developer's real .env --
    # otherwise a dev machine with ADMIN_PASSWORD/ADMIN_BOOTSTRAP_PASSWORD set
    # makes settings.auth_enabled True during the run, and every WS/HTTP test
    # that doesn't itself log in gets rejected with 401/403. Tests that need
    # auth explicitly monkeypatch these back on (see test_ws_auth.py,
    # test_lugo_auth.py's `_with_password` fixture) -- since that happens
    # later in the same test function, it overrides this default fine.
    from app.core.settings import settings

    monkeypatch.setattr(settings, "admin_password", "")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "")
    monkeypatch.setattr(settings, "device_auth_token", "")

    # Every WS session spawns an untracked `asyncio.create_task(_warm_and_notify())`
    # (ConversationSession.__init__, livehost's connect handler) that calls
    # warm_providers() -> provider.warm() in a thread. Boot-time warmup is
    # already disabled above, so in tests this per-session warm is never a
    # no-op cache-hit -- it always attempts a REAL model load for whatever the
    # default STT/TTS engine is, for any test that doesn't itself override
    # stt_engine/tts_engine to a stub. That real load can hang (e.g. a model
    # fetch with no network route in a sandboxed run), and since the task is
    # never tracked/cancelled, the WS test's TestClient portal thread then
    # blocks forever joining it on teardown -- indistinguishable from a plain
    # hung test. Neutralize only these two call sites (both bind the name at
    # module-import time, so patching source app.services.warmup.warm_providers
    # wouldn't reach them anyway) -- NOT the source function itself, since
    # app.main._warm_default_engines() re-imports it fresh per call and
    # test_warmup.py tests that boot-warmup path directly with spy providers.
    async def _no_op_warm(*_providers: object) -> None:
        return None

    monkeypatch.setattr("app.services.conversation.session.warm_providers", _no_op_warm)
    monkeypatch.setattr("app.api.routes.livehost.warm_providers", _no_op_warm)

    from app.services.system_config import system_config_store

    _real_get = system_config_store.get

    def _get_with_warmup_off():
        cfg = _real_get()
        return cfg.model_copy(update={
            "engines": cfg.engines.model_copy(update={"warmup_on_startup": False}),
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
    from app.services.model_registry.store import model_registry_store

    monkeypatch.setattr(settings, "profiles_path", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(settings, "tts_profiles_path", str(tmp_path / "tts_profiles.json"))
    monkeypatch.setattr(settings, "mcp_servers_path", str(tmp_path / "mcp_servers.json"))
    monkeypatch.setattr(settings, "system_config_path", str(tmp_path / "system_config.json"))
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    cfg_engine.configure(f"sqlite:///{tmp_path}/test.db")
    # model_registry_store now caches rows in memory (see store.py) -- without
    # invalidating here, a cache warmed by an earlier test (pointed at ITS tmp
    # DB) would leak into this test even though the DB underneath just changed.
    model_registry_store.invalidate()
    yield
    db_engine.configure()
    cfg_engine.configure()
    model_registry_store.invalidate()
