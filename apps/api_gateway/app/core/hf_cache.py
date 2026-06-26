"""Hugging Face hub cache helpers shared by model managers.

Both Whisper and TTS engines download into the shared hub cache. Directory sizes
are memoized by mtime so repeated ``/v1/models`` / status polls don't re-walk
multi-GB model directories on every request.
"""

from pathlib import Path

_size_cache: dict[str, tuple[float, int]] = {}


def hub_dir() -> Path:
    return Path.home() / ".cache" / "huggingface" / "hub"


def repo_dir(repo: str) -> Path:
    """Cache directory for a HF repo id (e.g. ``models--k2-fsa--OmniVoice``)."""
    return hub_dir() / f"models--{repo.replace('/', '--')}"


def repo_cached(repo: str | None) -> bool:
    return bool(repo) and repo_dir(repo).is_dir()


def dir_size_bytes(path: Path) -> int:
    """Total size of files under ``path`` (memoized by directory mtime)."""
    if not path.is_dir():
        return 0
    key = str(path)
    mtime = path.stat().st_mtime
    cached = _size_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    _size_cache[key] = (mtime, total)
    return total
