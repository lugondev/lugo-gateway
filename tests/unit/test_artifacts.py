from app.services.artifacts import artifact_store


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
