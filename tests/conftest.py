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

    # warmup._ready_ids tracks readiness by id(provider), which is safe in
    # production (providers are long-lived process-wide singletons) but not
    # across a test run: many tests register short-lived stub provider
    # instances, and CPython can reuse a freed object's id() for a later one,
    # making an unrelated test's provider spuriously read back as "ready".
    # Reset it per test so no test starts with another test's stale ids.
    from app.services import warmup as warmup_module

    monkeypatch.setattr(warmup_module, "_ready_ids", set())


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    """Point the DB engine at a per-test tmp file so no test ever touches
    the real data/app.db, even tests that only indirectly hit routes
    backed by the DB (chat/session/memory endpoints, conversation WS)."""
    from app.services.db import engine as db_engine
    from app.services.db import sync_engine as cfg_engine

    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    cfg_engine.configure(f"sqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()
    cfg_engine.configure()
