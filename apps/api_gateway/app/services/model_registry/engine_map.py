"""Maps a Models-page artifact (page-engine + its id field) to the Model
Registry coordinates auto-sync should ensure/disable. Local models only --
remote/BYO engines are added directly in the Registry, not via the Models page."""

from __future__ import annotations


def registry_ref(page_engine: str, artifact_id: str) -> tuple[str, str, str, str] | None:
    table = {
        "whisper": ("stt", "whisper"),
        "vosk": ("stt", "vosk"),
        "omnivoice": ("tts", "omnivoice"),
        "vieneu": ("tts", "vieneu"),
        "llm": ("llm", "ollama"),
    }
    ref = table.get(page_engine)
    if ref is None or not artifact_id:
        return None
    kind, engine = ref
    return (kind, engine, artifact_id, f"{engine} — {artifact_id}")
