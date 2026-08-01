"""Parsers for client-supplied query params.

Sample rates live next to the codecs that constrain them (core/audio.py's
parse_sample_rate for PCM/WAV, core/opus.py's parse_opus_sample_rate for Opus);
what lands here is everything else the WS/HTTP routes read off a query string.

Deliberately lenient: an unrecognized spelling on a flag falls back to the
server default rather than refusing the request. That is NOT the contract for
config (services/model_registry/resolve.py coerces env vars strictly and raises
EnvVarError on anything it doesn't recognize) -- an operator typo must fail
loudly, a caller typo on `?denoise=maybe` must not drop a connection.
"""

# Same spellings the strict env coercer accepts, kept in one place so a value
# that works in one entry point works in all of them.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def parse_bool(raw: str | None) -> bool | None:
    """None when the param was absent, else whether it reads as true."""
    if raw is None:
        return None
    return raw.strip().lower() in _TRUE_VALUES


def parse_bool_or(raw: str | None, default: bool) -> bool:
    """Same, with the server default substituted for an absent param."""
    parsed = parse_bool(raw)
    return default if parsed is None else parsed
