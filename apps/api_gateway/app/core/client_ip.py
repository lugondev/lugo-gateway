"""Which address a rate limiter should key on.

`request.client.host` is the SOCKET peer. Behind the production reverse proxy
that is the proxy itself, identical for every real client -- so an IP-keyed
limiter silently becomes a global cap rather than a per-client one. That was
already flagged as "coarser than intended" for device pairing; for the login
limiter it would be worse than coarse, since one attacker could then spend the
whole deployment's login budget and lock everybody out.

X-Forwarded-For carries the real client, but only its rightmost entries are
trustworthy: each proxy APPENDS the address it saw, so with N proxies of our
own in front, the last N entries were written by us and everything to their
left was supplied by the caller and can say anything. Reading the leftmost
entry (the common mistake) lets any caller forge a fresh key per request and
skip the limiter entirely.

So the number of hops we control is configuration, not a guess:
`TRUSTED_PROXY_HOPS` (settings.trusted_proxy_hops), default 0 = don't read the
header at all. Set it to 1 behind a single reverse proxy.
"""

from __future__ import annotations

from app.core.settings import settings

_UNKNOWN = "unknown"


def client_ip(request) -> str:
    """The caller's address, or "unknown" when there is no peer (e.g. an ASGI
    scope with no client, which TestClient and some transports produce)."""
    peer = request.client.host if getattr(request, "client", None) else _UNKNOWN

    hops = settings.trusted_proxy_hops
    if hops <= 0:
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [part.strip() for part in forwarded.split(",") if part.strip()]
    # entries[-hops] is the address the outermost proxy WE control observed.
    # Fewer entries than configured hops means the chain isn't what we were
    # told it is -- fall back to the peer rather than trust a caller-supplied
    # entry that happens to sit at that index.
    if len(entries) < hops:
        return peer
    return entries[-hops]
