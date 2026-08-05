import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # for concurrency_guard import

from concurrency_guard import running_foreign_pytest_pids  # noqa: E402


def _reset_auth_limiters() -> None:
    """Clear every process-global rate limiter between tests.

    They are keyed by client IP, and TestClient always presents the same one,
    so a budget spent by one test would otherwise 429 an unrelated later test.
    """
    from app.api.routes.auth import (
        login_account_limiter,
        login_ip_limiter,
        signup_limiter,
    )
    from app.services.auth.pairing import claim_rate_limiter, init_rate_limiter

    for limiter in (
        login_account_limiter,
        login_ip_limiter,
        signup_limiter,
        claim_rate_limiter,
        init_rate_limiter,
    ):
        limiter.reset()


def pytest_sessionstart(session):
    """Fail fast if another pytest is already running: concurrent runs of
    this suite deadlock each other (both wedge at 0% CPU mid-run -- shared
    model caches), so starting a second one buys a silent multi-minute hang,
    not results. Set PYTEST_ALLOW_CONCURRENT=1 to bypass deliberately."""
    if os.environ.get("PYTEST_ALLOW_CONCURRENT"):
        return
    pids = running_foreign_pytest_pids()
    if pids:
        pid_list = " ".join(str(p) for p in pids)
        pytest.exit(
            f"another pytest is already running (PIDs: {pid_list}). Concurrent runs "
            f"deadlock this suite mid-run. Kill it first (kill {pid_list}) or set "
            "PYTEST_ALLOW_CONCURRENT=1 to bypass.",
            returncode=2,
        )


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Keep tests independent of the developer's .env / running services:
    the built-in echo responder, and never spawn the OmniVoice sidecar or
    call a real LLM.
    """
    # Don't load real STT/TTS models when TestClient(app) runs the app lifespan.
    # warmup_on_startup lives on Settings (env), not system_config_store (Task 2
    # moved it off the admin-editable SystemConfig -- see main.py's lifespan).
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
    monkeypatch.setattr(settings, "warmup_on_startup", False)

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

    # warmup._ready_ids tracks readiness by id(provider), which is safe in
    # production (providers are long-lived process-wide singletons) but not
    # across a test run: many tests register short-lived stub provider
    # instances, and CPython can reuse a freed object's id() for a later one,
    # making an unrelated test's provider spuriously read back as "ready".
    # Reset it per test so no test starts with another test's stale ids.
    from app.services import warmup as warmup_module

    monkeypatch.setattr(warmup_module, "_ready_ids", set())


@pytest.fixture(autouse=True)
def _hermetic_engine_health(monkeypatch):
    """Default the Task 7 WS health gate to "everything's fine" so tests that
    connect to /v1/conversation/stream or /v1/lugo/stream aren't refused (or
    KeyError'd -- see app/services/stt/service.py:241's `list_engines()`
    fallback, which only recognizes a fixed set of real engine names) by a
    real check_resolved_engines() call against engines that are either
    genuinely unconfigured in this hermetic environment (the real defaults,
    e.g. vosk/omnivoice) or synthetic per-test stub names it has never heard
    of. Both route modules bind the name at import time (`from
    app.services.health import check_resolved_engines`), so -- same as
    warm_providers above -- patching the source function wouldn't reach
    either call site; each module binding must be patched individually.

    Tests that want to exercise the gate itself (test_session_health_gate.py)
    monkeypatch this same attribute again inside the test body; that later
    setattr wins over this fixture's earlier one for the duration of the
    test, so this default never shadows an intentional override.
    """
    from app.schemas.health import EngineHealth

    async def _ok_health(stt_engine, stt_model, tts_engine, tts_model):
        return (
            EngineHealth(engine=stt_engine, status="ok"),
            EngineHealth(engine=tts_engine, status="ok"),
        )

    monkeypatch.setattr("app.api.routes.conversation.check_resolved_engines", _ok_health)
    monkeypatch.setattr("app.api.routes.lugo.check_resolved_engines", _ok_health)


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
    from app.services.mcp.server_store import mcp_server_store
    from app.services.model_registry.store import model_registry_store
    from app.services.plugins.store import plugin_store
    from app.services.profiles.store import profile_store
    from app.services.providers.store import provider_store
    from app.services.quota.store import quota_store
    from app.services.tts.profile_store import tts_profile_store

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
    provider_store.invalidate()
    quota_store.invalidate()
    # Same reason for the config stores (profiles/tts profiles/mcp servers): their
    # cache is process-global, so without this the FIRST test to touch one keeps
    # serving its rows -- and, worse, later tests never re-run init_config_tables()
    # against their own tmp DB ("no such table: config_profiles").
    profile_store.invalidate()
    tts_profile_store.invalidate()
    mcp_server_store.invalidate()
    plugin_store.invalidate()
    # The auth rate limiters (api/routes/auth.py) are process-global sliding
    # windows keyed by client IP, and every TestClient request arrives from the
    # same "testclient" address -- so without this the signup/login budget is
    # shared by the WHOLE run and the 20th test to sign a user up starts getting
    # 429s from a limit some earlier, unrelated test spent. Same class of leak
    # as the config-store caches above.
    _reset_auth_limiters()
    yield
    _reset_auth_limiters()
    db_engine.configure()
    cfg_engine.configure()
    model_registry_store.invalidate()
    provider_store.invalidate()
    quota_store.invalidate()
    # Same reason for the config stores (profiles/tts profiles/mcp servers): their
    # cache is process-global, so without this the FIRST test to touch one keeps
    # serving its rows -- and, worse, later tests never re-run init_config_tables()
    # against their own tmp DB ("no such table: config_profiles").
    profile_store.invalidate()
    tts_profile_store.invalidate()
    mcp_server_store.invalidate()
    plugin_store.invalidate()
