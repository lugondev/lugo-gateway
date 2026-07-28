from app.schemas.health import EngineHealth, ProfileHealth


def test_unavailable_blocks_session():
    assert EngineHealth(engine="http_stt", status="unavailable", detail="down").blocks_session is True


def test_ok_does_not_block():
    assert EngineHealth(engine="vosk", status="ok").blocks_session is False


def test_not_ready_does_not_block():
    """A local engine still loading its model is not a failure -- session_started
    already reports stt_ready/tts_ready for this case."""
    assert EngineHealth(engine="whisper", status="not_ready").blocks_session is False


def test_detail_defaults_to_empty_string():
    assert EngineHealth(engine="vosk", status="ok").detail == ""


def test_profile_health_serializes_nested_engines():
    payload = ProfileHealth(
        profile="default",
        stt=EngineHealth(engine="http_stt", status="unavailable", detail="unreachable"),
        tts=EngineHealth(engine="vieneu", status="ok"),
    ).model_dump()
    assert payload["profile"] == "default"
    assert payload["stt"]["status"] == "unavailable"
    assert payload["stt"]["detail"] == "unreachable"
    assert payload["tts"]["engine"] == "vieneu"
