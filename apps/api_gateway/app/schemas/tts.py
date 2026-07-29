from pydantic import BaseModel, Field, field_validator

# Deliberate exception to the "schemas don't import services" layering rule
# (A7 in the structure-refactor audit): `artifact_store.contains()` below is
# the path-traversal containment check for `ref_audio_path`, the security
# boundary from two authz-hardening rounds (see the field_validator's
# docstring). It MUST run on this REQUEST model, not a persisted one and not
# a route handler, so a bad/host-mismatched stored row can't skip it and one
# provider's synth call can't read arbitrary files off the gateway. Moving
# this check to break the schemas->services import would either weaken it or
# relocate it somewhere that runs at the wrong time -- not worth it for
# layering purity alone. Left as-is on purpose; A7 stays open/documented.
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
        paths that resolve outside `artifact_store.base_dir`.

        Falsy (None or "") short-circuits as "not set": `TtsProfile` uses ""
        as its not-set sentinel (see routes/tts_profiles.py's
        `_require_ref_audio_path_contained`), and an API client that echoes
        a profile's fields straight into /v1/tts/synthesize would otherwise
        get a spurious 422 here -- `contains("")` resolves "" to the
        artifacts dir itself, which isn't even a readable file, let alone
        one outside it. A real (non-empty) path still runs the full
        containment check below."""
        if not v:
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
