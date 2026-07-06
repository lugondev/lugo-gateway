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


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    """Point the DB engine at a per-test tmp file so no test ever touches
    the real data/app.db, even tests that only indirectly hit routes
    backed by the DB (chat/session/memory endpoints, conversation WS)."""
    from app.services.db import engine as db_engine

    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()
