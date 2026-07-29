"""Structural guarantee: nothing in the gateway can persist synthesized audio.

This is not a behavior test -- it is the guard that keeps the artifact-writing
mechanism from creeping back in. See
docs/superpowers/specs/2026-07-29-drop-audio-artifacts-design.md.
"""
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
