from pydantic import BaseModel, Field, field_validator

from app.services.artifacts import artifact_store


class TTSRequest(BaseModel):
    # 10,000 chars (~1,500-2,000 words, roughly 10-15 minutes of spoken audio)
    # comfortably covers any legitimate synthesize/stream call -- /v1/tts/stream
    # segments this into ~200-char chunks (segmenter.py's default max_chars) for
    # pseudo-streaming playback, so even a full-length narration script fits.
    # Without a cap, a single request hands one provider call (or, for
    # /v1/tts/stream, dozens of segment calls run back-to-back inside one
    # fire-and-forget task) an unbounded payload -- M5, see
    # docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md. Shared
    # by both routes on purpose: /v1/tts/stream needs the same ceiling, just
    # applied before segmentation instead of per-provider-call.
    text: str = Field(..., min_length=1, max_length=10_000)
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
