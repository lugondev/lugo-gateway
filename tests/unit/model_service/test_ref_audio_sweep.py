import os
import time

from app.services.artifacts import artifact_store
from model_service.app.main import sweep_stale_ref_audio

_STALE_SECONDS = 7200  # comfortably past main.py's _STALE_AGE_SECONDS (3600)


def test_sweep_removes_leftover_tmp_refs_but_keeps_reference_audio():
    stale = artifact_store.base_dir / ("a1" * 16 + ".wav")
    # A fresh bare-hex .wav must survive: it could be a sibling process's
    # in-flight temp file (the four local engines run as separate native
    # processes sharing this directory), so only files older than the mtime
    # threshold are fair game.
    fresh_tmp_ref = artifact_store.base_dir / ("b2" * 16 + ".wav")
    # Inherited from the deleted prune() tests: OmniVoice's pinned voice
    # reference and user-uploaded clips must survive every sweep -- deleting
    # the pinned file silently changes the cloned voice. The `.wav.bak` name
    # is 32 hex chars followed by ".wav" followed by more text -- it pins the
    # regex's end anchor: an unanchored `^[0-9a-f]{32}\.wav` would wrongly
    # match and delete it.
    keepers = [
        artifact_store.base_dir / "ref_deadbeef.wav",
        artifact_store.base_dir / "_omnivoice_voice_ref.wav",
        artifact_store.base_dir / "notes.txt",
        artifact_store.base_dir / (("aa" * 16) + ".wav.bak"),
        fresh_tmp_ref,
    ]
    stale.write_bytes(b"RIFF")
    for keep in keepers:
        keep.write_bytes(b"RIFF")

    old = time.time() - _STALE_SECONDS
    os.utime(stale, (old, old))

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
