"""Local filesystem artifact store for generated audio.

This is the foundation implementation. The architecture allows swapping this
for an S3-compatible object store later without touching callers.
"""

import uuid
from pathlib import Path

from app.core.settings import settings


class ArtifactStore:
    def __init__(self, base_dir: str, url_prefix: str = "/artifacts") -> None:
        self.base_dir = Path(base_dir)
        self.url_prefix = url_prefix.rstrip("/")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_wav(self, data: bytes) -> tuple[str, str]:
        """Persist WAV bytes; return (artifact_id, public_url)."""
        artifact_id = uuid.uuid4().hex
        filename = f"{artifact_id}.wav"
        (self.base_dir / filename).write_bytes(data)
        return artifact_id, f"{self.url_prefix}/{filename}"


artifact_store = ArtifactStore(settings.artifacts_dir)
