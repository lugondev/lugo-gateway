from pydantic import BaseModel, Field, field_validator

from app.services.artifacts import artifact_store


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    engine: str = "omnivoice"
    # Registry row selector: multiple rows can share an engine (e.g. three
    # http_tts rows pointing at different service base_urls), so the engine
    # alone is ambiguous. Empty = let the provider fall back to its first
    # enabled row (legacy behavior for engines with a single row).
    model_id: str = ""
    language: str | None = None
    speed: float | None = None
    instruct: str | None = None
    ref_audio_path: str | None = None
    ref_text: str | None = None
    voice: str | None = None  # VieNeu preset voice id

    @field_validator("ref_audio_path")
    @classmethod
    def _ref_audio_path_must_stay_in_artifacts_dir(cls, v: str | None) -> str | None:
        """Six TTS providers feed this straight into `Path(...).read_bytes()`.
        Without this check any logged-in user could read arbitrary files off
        the gateway (e.g. `/app/.env`) or hang a worker on a device node.
        `POST /v1/tts/reference-audio` legitimately returns absolute paths
        under the artifacts dir, so this accepts those -- it only rejects
        paths that resolve outside `artifact_store.base_dir`."""
        if v is None:
            return v
        if not artifact_store.contains(v):
            raise ValueError("ref_audio_path must be inside the artifacts directory")
        return v


class TTSResult(BaseModel):
    engine: str
    sample_rate: int
    audio_url: str | None = None
    duration_seconds: float | None = None  # length of the produced audio
    process_seconds: float | None = None  # wall-clock time spent synthesizing it
    job_id: str | None = None
    text: str | None = None
