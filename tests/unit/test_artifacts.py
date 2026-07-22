import asyncio
import os
import time

from app.services.artifacts import ArtifactStore, artifact_store, prune_loop


def _age_file(path, seconds: float) -> None:
    stale = time.time() - seconds
    os.utime(path, (stale, stale))


def test_prune_removes_only_files_older_than_max_age(tmp_path):
    """Every TTS sentence writes a wav into artifacts/ and nothing ever deleted
    them -- disk grew without bound. prune() reclaims files past the TTL while
    leaving fresh ones (still referenced by live playback URLs) alone."""
    store = ArtifactStore(str(tmp_path))
    _, fresh_url = store.save_wav(b"fresh-wav")
    _, old_url = store.save_wav(b"old-wav")
    old = tmp_path / old_url.rsplit("/", 1)[-1]
    _age_file(old, seconds=7200)

    removed = store.prune(max_age_s=3600)

    assert removed == 1
    assert not old.exists()
    fresh_name = fresh_url.rsplit("/", 1)[-1]
    assert (tmp_path / fresh_name).exists()


def test_prune_leaves_non_artifact_files_alone(tmp_path):
    """OmniVoice keeps its pinned voice reference (_omnivoice_voice_ref.wav,
    written once, mtime never refreshed) and the sidecar's open log file in
    the SAME directory. Pruning them changes the cloned voice every TTL and
    orphans the open log inode -- only uuid-hex artifact files may be deleted."""
    store = ArtifactStore(str(tmp_path))
    keepers = ["_omnivoice_voice_ref.wav", "_omnivoice_sidecar.log", "notes.txt"]
    for name in keepers:
        path = tmp_path / name
        path.write_bytes(b"keep-me")
        _age_file(path, seconds=7200)

    assert store.prune(max_age_s=3600) == 0
    for name in keepers:
        assert (tmp_path / name).exists()


def test_prune_ignores_subdirectories(tmp_path):
    store = ArtifactStore(str(tmp_path))
    sub = tmp_path / "keep-dir"
    sub.mkdir()
    _age_file(sub, seconds=7200)

    assert store.prune(max_age_s=3600) == 0
    assert sub.exists()


async def test_prune_loop_prunes_periodically_and_sleeps_first(tmp_path):
    """The loop sleeps BEFORE the first prune so that merely starting the app
    (including test lifespans pointed at the real artifacts dir) doesn't
    immediately delete files."""
    store = ArtifactStore(str(tmp_path))
    _, old_url = store.save_wav(b"old-wav")
    old = tmp_path / old_url.rsplit("/", 1)[-1]
    _age_file(old, seconds=7200)

    task = asyncio.create_task(prune_loop(store, max_age_s=3600, interval_s=0.02))
    await asyncio.sleep(0)  # let the loop start; it must not have pruned yet
    assert old.exists()

    for _ in range(50):  # up to ~1s for the first interval to elapse
        if not old.exists():
            break
        await asyncio.sleep(0.02)
    task.cancel()

    assert not old.exists()


def test_save_reference_audio_writes_file_and_returns_path(tmp_path):
    store = ArtifactStore(str(tmp_path))
    data = b"reference-clip-bytes"

    ref_id, url = store.save_reference_audio(data)

    assert ref_id.startswith("ref_")
    assert url == f"{store.url_prefix}/{ref_id}.wav"
    resolved = store.path_for(ref_id)
    assert resolved.read_bytes() == data


def test_save_reference_audio_is_excluded_from_prune(tmp_path):
    """A voice-clone reference is meant to persist as long as the TtsProfile
    referencing it does -- it must not be swept up by the same TTL that
    reclaims ephemeral synthesized-speech artifacts."""
    store = ArtifactStore(str(tmp_path))
    ref_id, url = store.save_reference_audio(b"keep-me")
    path = tmp_path / url.rsplit("/", 1)[-1]
    _age_file(path, seconds=7200)

    assert store.prune(max_age_s=3600) == 0
    assert path.exists()


def test_path_for_raises_when_artifact_missing(tmp_path):
    import pytest

    store = ArtifactStore(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        store.path_for("does-not-exist")


def test_save_mp3_writes_file_and_returns_url():
    data = b"fake-mp3-bytes"
    artifact_id, url = artifact_store.save_mp3(data)

    saved_path = artifact_store.base_dir / f"{artifact_id}.mp3"
    try:
        assert url == f"{artifact_store.url_prefix}/{artifact_id}.mp3"
        assert saved_path.read_bytes() == data
    finally:
        # save_mp3 writes into the real artifacts/ dir (artifact_store.base_dir
        # is bound to settings.artifacts_dir) -- clean up so repeated test runs
        # don't leak stray files into it.
        saved_path.unlink(missing_ok=True)
