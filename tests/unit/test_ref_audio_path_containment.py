"""ref_audio_path must never escape the artifacts directory.

It reaches Path(...).read_bytes() in six providers, so an unvalidated value is
an arbitrary local file read (and, via http_tts, an exfiltration channel)."""

import pytest
from pydantic import ValidationError

from app.schemas.tts import TTSRequest
from app.services.artifacts import artifact_store


def _inside(name: str) -> str:
    return str((artifact_store.base_dir / name).resolve())


def test_accepts_path_inside_artifacts_dir():
    req = TTSRequest(text="hi", ref_audio_path=_inside("refs/voice.wav"))
    assert req.ref_audio_path is not None


def test_accepts_none():
    assert TTSRequest(text="hi").ref_audio_path is None


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "/dev/zero",
        "/app/.env",
        "../../../../etc/passwd",
        "relative/escape/../../../etc/passwd",
    ],
)
def test_rejects_paths_outside_artifacts_dir(bad):
    with pytest.raises(ValidationError):
        TTSRequest(text="hi", ref_audio_path=bad)


def test_rejects_traversal_that_starts_inside():
    escape = str(artifact_store.base_dir / ".." / ".." / "etc" / "passwd")
    with pytest.raises(ValidationError):
        TTSRequest(text="hi", ref_audio_path=escape)


def test_live_stored_profile_shape_still_validates():
    """The three live TTS profiles that set ref_audio_path all store absolute
    paths shaped like <repo>/artifacts/refs/<name>.wav (manually-placed
    files, not upload-generated) -- confirm that shape keeps validating."""
    live_shape = str((artifact_store.base_dir / "refs" / "omnivoice-nu-tre-ref.wav").resolve())
    req = TTSRequest(text="hi", ref_audio_path=live_shape)
    assert req.ref_audio_path == live_shape
