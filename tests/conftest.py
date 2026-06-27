import pytest

from app.core.settings import settings


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Keep tests independent of the developer's .env / running services:
    use mock TTS, the built-in echo responder, and never spawn the OmniVoice
    sidecar or call a real LLM.
    """
    monkeypatch.setattr(settings, "enable_mock_engines", True)
    monkeypatch.setattr(settings, "conversation_llm_base_url", "")
    monkeypatch.setattr(settings, "omnivoice_use_server", False)
