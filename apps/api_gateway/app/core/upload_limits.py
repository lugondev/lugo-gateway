"""Shared upload-size limits.

Kept in one place because the same number is enforced twice for the same
route -- once at the ASGI layer, before Starlette's multipart parser ever
sees the request (main.py's UploadSizeLimitMiddleware), and once inside the
route handler as a belt-and-suspenders check (routes/tts.py). Importing one
constant into both means the two enforcement points can never silently
drift apart.
"""

# Reference-audio clips are short voice-clone utterances (a handful of
# seconds up to maybe a minute of speech), not full recordings. At the
# highest realistic bitrate this route accepts (48kHz/16-bit stereo WAV,
# ~192KB/s) a full minute is ~11.5MB, so 10MB comfortably covers legitimate
# clips while still bounding how long the upload -- and the (now off-loop,
# but still O(size)) downstream base64 encode/HTTP forward in
# http_tts_provider._render_wav -- can run. See H3 in
# docs/superpowers/specs/2026-07-29-adversarial-audit-findings.md.
REFERENCE_AUDIO_MAX_BYTES = 10 * 1024 * 1024
