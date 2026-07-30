"""Structural guarantee: nothing in the gateway can persist synthesized audio.

This is not a behavior test -- it is the guard that keeps the artifact-writing
mechanism from creeping back in. See
docs/superpowers/specs/2026-07-29-drop-audio-artifacts-design.md.
"""
from fastapi.testclient import TestClient

from app.main import app
from app.services.artifacts import ArtifactStore, artifact_store
from app.services.tts.base import TTSProvider


def test_artifact_store_cannot_write_generated_audio():
    for gone in ("save_wav", "save_mp3", "prune"):
        assert not hasattr(ArtifactStore, gone), f"{gone} must not exist"


def test_reference_audio_api_survives():
    for kept in ("save_reference_audio", "contains", "path_for"):
        assert hasattr(artifact_store, kept)


def test_render_audio_is_the_only_audio_seam():
    assert not hasattr(TTSProvider, "synthesize")
    assert "render_audio" in TTSProvider.__abstractmethods__


def test_artifacts_are_not_served_over_http():
    name = "deadbeef" * 4 + ".wav"
    path = artifact_store.base_dir / name
    path.write_bytes(b"RIFFfake")
    try:
        assert TestClient(app).get(f"/artifacts/{name}").status_code == 404
    finally:
        path.unlink(missing_ok=True)
