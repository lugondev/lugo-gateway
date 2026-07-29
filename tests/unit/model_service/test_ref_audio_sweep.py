import re

from app.services.artifacts import artifact_store
from model_service.app.main import sweep_stale_ref_audio


def test_sweep_removes_leftover_tmp_refs_but_keeps_reference_audio():
    stale = artifact_store.base_dir / ("a1" * 16 + ".wav")
    # Inherited from the deleted prune() tests: OmniVoice's pinned voice
    # reference and user-uploaded clips must survive every sweep -- deleting
    # the pinned file silently changes the cloned voice.
    keepers = [
        artifact_store.base_dir / "ref_deadbeef.wav",
        artifact_store.base_dir / "_omnivoice_voice_ref.wav",
        artifact_store.base_dir / "notes.txt",
    ]
    stale.write_bytes(b"RIFF")
    for keep in keepers:
        keep.write_bytes(b"RIFF")
    try:
        removed = sweep_stale_ref_audio(artifact_store.base_dir)
        assert removed >= 1
        assert not stale.exists()
        for keep in keepers:
            assert keep.exists()
    finally:
        stale.unlink(missing_ok=True)
        for keep in keepers:
            keep.unlink(missing_ok=True)
