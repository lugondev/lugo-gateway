"""Task 6 (A1) seam tests for the shared TTS-request build + degrade
classification (services/conversation/turn_tts.py), extracted out of
api/routes/livehost.py's and services/conversation/session.py's `_synth`
closures. The end-to-end degrade contract stays covered by
tests/unit/livehost/test_livehost_tts_profile.py::test_livehost_bad_ref_audio_path_degrades_to_tts_error
and tests/unit/conversation/test_session_bad_ref_audio_path_degrades.py --
both unchanged, still the pre-extraction contract fence. These tests drive
the extracted builder function directly."""

from app.schemas.tts import TTSRequest
from app.services.conversation.turn_tts import build_tts_request_or_degrade


def test_build_tts_request_or_degrade_succeeds_for_a_plain_request():
    request, exc = build_tts_request_or_degrade(
        text="hello", engine="stub-tts", model_id="", voice="v1",
        ref_audio_path=None, ref_text=None, instruct=None, speed=None, language=None,
    )
    assert exc is None
    assert isinstance(request, TTSRequest)
    assert request.text == "hello"
    assert request.engine == "stub-tts"
    assert request.voice == "v1"


def test_build_tts_request_or_degrade_reports_bad_ref_audio_path():
    request, exc = build_tts_request_or_degrade(
        text="hello", engine="stub-tts", model_id="", voice=None,
        ref_audio_path="/etc/passwd", ref_text="x", instruct=None, speed=None, language=None,
    )
    assert request is None
    assert exc is not None
    assert "ref_audio_path" in str(exc)
    assert "artifacts directory" in str(exc)
