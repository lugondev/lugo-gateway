"""Local filesystem artifact store for generated audio.

This is the foundation implementation. The architecture allows swapping this
for an S3-compatible object store later without touching callers.
"""

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path

from app.core.settings import settings

logger = logging.getLogger(__name__)

# Only files this store itself created (uuid4().hex + extension) are ever
# pruned. The directory is shared: OmniVoice keeps its pinned voice reference
# (_omnivoice_voice_ref.wav -- deleting it makes the cloned voice change) and
# its open sidecar log here, and operators may drop files in too.
_ARTIFACT_FILENAME = re.compile(r"^[0-9a-f]{32}\.(wav|mp3)$")


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

    def save_mp3(self, data: bytes) -> tuple[str, str]:
        """Persist MP3 bytes; return (artifact_id, public_url)."""
        artifact_id = uuid.uuid4().hex
        filename = f"{artifact_id}.mp3"
        (self.base_dir / filename).write_bytes(data)
        return artifact_id, f"{self.url_prefix}/{filename}"

    def prune(self, max_age_s: float) -> int:
        """Delete artifacts older than `max_age_s`; return how many were removed.

        Every synthesized sentence writes a file here and nothing else ever
        deleted them, so an always-on deployment grew without bound. Blocking
        directory walk -- call via `asyncio.to_thread` from async code (see
        `prune_loop`)."""
        cutoff = time.time() - max_age_s
        removed = 0
        for path in self.base_dir.iterdir():
            if not _ARTIFACT_FILENAME.match(path.name):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                # Raced with a concurrent delete/replace -- skip, next run gets it.
                continue
        return removed


async def prune_loop(store: ArtifactStore, max_age_s: float, interval_s: float) -> None:
    """Periodically prune old artifacts. Sleeps BEFORE the first prune so that
    merely starting the app (including test lifespans pointed at the real
    artifacts dir) never deletes anything by itself."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            removed = await asyncio.to_thread(store.prune, max_age_s)
            if removed:
                logger.info("artifact prune: removed %d files older than %.0fh", removed, max_age_s / 3600)
        except Exception:  # noqa: BLE001 - the janitor must survive any single failure
            logger.exception("artifact prune failed; retrying next interval")


artifact_store = ArtifactStore(settings.artifacts_dir)
