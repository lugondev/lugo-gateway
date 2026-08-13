"""Client-IP extraction for the rate limiters.

`request.client.host` is the proxy's address behind a reverse proxy, which
collapses every client onto one key -- so an IP-keyed limiter becomes a global
cap rather than a per-client one (flagged in services/auth/pairing.py's
docstring). X-Forwarded-For fixes that, but only when we know how many hops we
control: trusting it unconditionally lets any caller forge a fresh key per
request and skip the limiter entirely. Hence an explicit hop count, defaulting
to 0 (= don't trust the header at all).
"""


from app.core.client_ip import client_ip


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, host="10.0.0.1", forwarded=None):
        self.client = _FakeClient(host) if host else None
        self.headers = {} if forwarded is None else {"x-forwarded-for": forwarded}


def test_uses_socket_peer_when_no_proxy_is_trusted(monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    request = _FakeRequest(host="10.0.0.1", forwarded="1.2.3.4")
    assert client_ip(request) == "10.0.0.1"


def test_forged_forwarded_header_cannot_change_the_key_when_untrusted(monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    a = client_ip(_FakeRequest(host="10.0.0.1", forwarded="1.1.1.1"))
    b = client_ip(_FakeRequest(host="10.0.0.1", forwarded="2.2.2.2"))
    assert a == b


def test_one_trusted_hop_reads_the_last_forwarded_entry(monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    request = _FakeRequest(host="10.0.0.1", forwarded="9.9.9.9, 1.2.3.4")
    assert client_ip(request) == "1.2.3.4"


def test_client_supplied_entries_beyond_the_trusted_hops_are_ignored(monkeypatch):
    """With one trusted hop, only the rightmost entry was written by our own
    proxy; everything left of it is attacker-controlled and must not be read."""
    from app.core.settings import settings

    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    forged = client_ip(_FakeRequest(host="10.0.0.1", forwarded="evil, 1.2.3.4"))
    plain = client_ip(_FakeRequest(host="10.0.0.1", forwarded="1.2.3.4"))
    assert forged == plain == "1.2.3.4"


def test_falls_back_to_the_peer_when_there_are_fewer_hops_than_configured(monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert client_ip(_FakeRequest(host="10.0.0.1", forwarded="1.2.3.4")) == "10.0.0.1"


def test_missing_peer_is_reported_rather_than_crashing(monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    assert client_ip(_FakeRequest(host=None)) == "unknown"
