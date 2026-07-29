from app.services.tts.profile_models import TtsProfile


def test_tts_profile_defaults():
    p = TtsProfile(name="x")
    assert p.engine == ""
    assert p.voice_mode == "preset"
    assert p.voice == ""
    assert p.ref_audio_path == ""
    assert p.ref_text == ""
    assert p.instruct == ""
    assert p.speed is None
    assert p.language is None


def test_tts_profile_preset_full():
    p = TtsProfile(name="cohost-girl", engine="vieneu", voice_mode="preset", voice="vi-female-1")
    assert p.name == "cohost-girl"
    assert p.engine == "vieneu"
    assert p.voice == "vi-female-1"


def test_tts_profile_clone_full():
    p = TtsProfile(
        name="cloned-host", engine="omnivoice", voice_mode="clone",
        ref_audio_path="artifacts/refs/host.wav", ref_text="Xin chao cac ban",
        instruct="cheerful", speed=1.2, language="vi",
    )
    assert p.voice_mode == "clone"
    assert p.ref_audio_path == "artifacts/refs/host.wav"
    assert p.ref_text == "Xin chao cac ban"
    assert p.instruct == "cheerful"
    assert p.speed == 1.2
    assert p.language == "vi"


def test_tts_profile_roundtrip():
    p = TtsProfile(name="rt", engine="vieneu", speed=0.9)
    data = p.model_dump()
    p2 = TtsProfile.model_validate(data)
    assert p2.engine == "vieneu"
    assert p2.speed == 0.9
