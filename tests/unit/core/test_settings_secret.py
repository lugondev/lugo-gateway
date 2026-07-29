from app.core.settings import settings


def test_effective_secret_uses_configured_value(monkeypatch):
    monkeypatch.setattr(settings, "session_secret", "configured-secret")
    assert settings.effective_session_secret == "configured-secret"


def test_effective_secret_is_stable_across_calls_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "session_secret", "")
    first = settings.effective_session_secret
    second = settings.effective_session_secret
    assert first == second
    assert len(first) >= 32
