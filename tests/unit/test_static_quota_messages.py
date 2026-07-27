"""The static admin UI is a real client of the quota-enforcing endpoints.

A 429 from these routes carries the precise reason ("user quota exceeded for u1:
$12.04 / $12.00 (monthly)"). helpers.js's quotaMessage() surfaces it verbatim;
a client that skips the helper renders the raw error body instead, which is the
difference between a user knowing they are out of budget and thinking the app is
broken. This test keeps the set of quota-capable clients from drifting apart --
tts-stream.js was the one client that had been left out.
"""

from pathlib import Path

JS = Path(__file__).resolve().parents[2] / "apps" / "api_gateway" / "app" / "static" / "js"

# Static clients that POST to a route which can answer 429 from the quota gate:
# /v1/stt/transcribe, /v1/tts/synthesize and /v1/tts/stream respectively.
_QUOTA_CAPABLE_CLIENTS = ("stt-batch.js", "tts-batch.js", "tts-stream.js")


def test_every_quota_capable_static_client_surfaces_the_quota_reason():
    problems = []
    for name in _QUOTA_CAPABLE_CLIENTS:
        text = (JS / name).read_text(encoding="utf-8")
        if "quotaMessage" not in text:
            problems.append(f"{name}: never mentions quotaMessage")
        elif "quotaMessage(response, body)" not in text:
            # Importing it and not calling it on the response is the same bug
            # with extra steps.
            problems.append(f"{name}: imports quotaMessage but never calls it on the response")
    assert not problems, (
        "Static client(s) posting to a 429-capable route without the shared "
        "helper -- import quotaMessage from ./helpers.js and branch on it before "
        "printing the body, the way stt-batch.js does:\n  " + "\n  ".join(problems)
    )
