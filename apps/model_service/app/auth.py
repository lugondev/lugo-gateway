"""Bearer-token auth for the model service.

One static token from env, compared in constant time. There are no users and
no sessions here -- the only caller is the gateway, holding the token in its
Model Registry entry's api_key column.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import Header, HTTPException

_PREFIX = "Bearer "


def make_auth_dependency(expected_token: str) -> Callable:
    async def verify(authorization: str = Header(default="")) -> None:
        # RFC 7235: the auth-scheme token ("Bearer") is case-insensitive, so
        # "bearer ..."/"BEARER ..."/"Bearer ..." must all be accepted. Only
        # the scheme+separator slice is lowercased for the comparison -- the
        # token itself keeps its exact casing and is compared in constant
        # time below.
        if authorization[: len(_PREFIX)].lower() != _PREFIX.lower():
            raise HTTPException(status_code=401, detail="missing bearer token")
        supplied = authorization[len(_PREFIX):]
        # compare_digest, not ==, so a wrong token can't be recovered by timing.
        if not secrets.compare_digest(supplied, expected_token):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    return verify
