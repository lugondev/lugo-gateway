"""Stdlib PBKDF2 password hashing -- no new dependency, matching the codebase's
existing preference for stdlib crypto primitives (hmac.compare_digest for the
device token). Encoded format: "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>",
with the iteration count stored alongside the hash so the cost factor can be
raised later without breaking already-stored hashes."""

from __future__ import annotations

import hashlib
import hmac
import os

_SCHEME = "pbkdf2_sha256"
_ITERATIONS = 600_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_SCHEME}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations_s, salt_hex, hash_hex = encoded.split("$")
        if scheme != _SCHEME:
            return False
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)
