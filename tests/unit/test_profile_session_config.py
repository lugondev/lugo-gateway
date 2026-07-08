from app.services.profiles.models import Profile, SessionConfig


def test_profile_has_default_session_timeout():
    p = Profile(name="d")
    assert isinstance(p.session, SessionConfig)
    assert p.session.idle_timeout_s == 30


def test_profile_session_timeout_override():
    p = Profile(name="d", session=SessionConfig(idle_timeout_s=10))
    assert p.session.idle_timeout_s == 10


def test_profile_loads_without_session_field():
    # Legacy profiles.json entries omit `session` -> must default, not error.
    p = Profile.model_validate({"name": "esp32-assistant"})
    assert p.session.idle_timeout_s == 30
