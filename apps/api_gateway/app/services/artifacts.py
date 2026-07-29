"""Local filesystem store for voice-clone reference audio.

Synthesized audio is never persisted -- TTS providers return bytes
(`render_audio`) that go straight out over the request or socket. What lives
here is user-uploaded reference audio for voice cloning, plus OmniVoice's
pinned voice reference, and it is never served over HTTP.
"""

import uuid
from pathlib import Path

from app.core.settings import settings


class ArtifactStore:
    def __init__(self, base_dir: str, url_prefix: str = "/artifacts") -> None:
        self.base_dir = Path(base_dir)
        self.url_prefix = url_prefix.rstrip("/")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_reference_audio(self, data: bytes) -> tuple[str, str]:
        """Persist a voice-clone reference clip; return (artifact_id, public_url).

        Prefixed `ref_` so it's clearly distinguished from OmniVoice's own
        pinned reference file (`_omnivoice_voice_ref.wav`) sharing this
        directory -- see the module docstring above."""
        artifact_id = f"ref_{uuid.uuid4().hex}"
        filename = f"{artifact_id}.wav"
        (self.base_dir / filename).write_bytes(data)
        return artifact_id, f"{self.url_prefix}/{filename}"

    def contains(self, candidate: str) -> bool:
        """True iff `candidate` resolves to a path inside this store's
        base_dir.

        `TTSRequest.ref_audio_path` (schemas/tts.py) is fed straight into
        `Path(...).read_bytes()` by six TTS providers, so an unconstrained
        value there is an arbitrary local file read (and, via http_tts, an
        exfiltration channel -- the bytes get base64'd into an outbound HTTP
        request). `.resolve()` on both sides normalizes `..` segments and
        follows symlinks before the comparison, so neither traversal nor a
        symlink planted inside the artifacts dir can point outside it. See
        docs/superpowers/sdd/2026-07-28-critical-authz-fixes/task-5-brief.md.
        """
        try:
            resolved = Path(candidate).resolve()
        except (OSError, ValueError):
            return False
        return resolved.is_relative_to(self.base_dir.resolve())

    def path_for(self, artifact_id: str) -> Path:
        """Resolve an id from save_reference_audio back to its file on disk."""
        path = self.base_dir / f"{artifact_id}.wav"
        if path.exists():
            return path
        raise FileNotFoundError(artifact_id)


artifact_store = ArtifactStore(settings.artifacts_dir_resolved)
