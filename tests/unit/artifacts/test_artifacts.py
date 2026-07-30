import pytest

from app.services.artifacts import ArtifactStore


def test_save_reference_audio_writes_file_and_returns_path(tmp_path):
    store = ArtifactStore(str(tmp_path))
    data = b"reference-clip-bytes"

    ref_id = store.save_reference_audio(data)

    assert ref_id.startswith("ref_")
    resolved = store.path_for(ref_id)
    assert resolved.read_bytes() == data


def test_path_for_raises_when_artifact_missing(tmp_path):
    store = ArtifactStore(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        store.path_for("does-not-exist")
