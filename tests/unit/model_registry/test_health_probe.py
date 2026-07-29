import httpx
import pytest

from app.services.model_registry.health_probe import probe_service_health


@pytest.fixture
def mock_transport(monkeypatch):
    """Install a handler as httpx's transport; returns a dict capturing the request."""
    seen = {}

    def install(handler):
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return seen

    return install


@pytest.mark.asyncio
async def test_strips_v1_suffix_and_hits_health(mock_transport):
    seen = mock_transport(lambda req: (
        seen.__setitem__("url", str(req.url)),
        httpx.Response(200, json={"status": "ok"}),
    )[1])
    ok, reason = await probe_service_health("http://127.0.0.1:8100/v1", "tok")
    assert ok is True
    assert reason is None
    assert seen["url"] == "http://127.0.0.1:8100/health"


@pytest.mark.asyncio
async def test_sends_bearer_token_when_api_key_present(mock_transport):
    captured = {}

    def handler(req):
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200)

    mock_transport(handler)
    await probe_service_health("http://host:8100/v1", "s3cret")
    assert captured["auth"] == "Bearer s3cret"


@pytest.mark.asyncio
async def test_no_auth_header_when_api_key_blank(mock_transport):
    captured = {}

    def handler(req):
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200)

    mock_transport(handler)
    await probe_service_health("http://host:8100/v1", "")
    assert captured["auth"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 404, 500])
async def test_any_http_response_counts_as_reachable(mock_transport, status):
    """A process that answers at all is up -- even if it has no /health route
    or rejects our token. We are checking liveness, not the route contract."""
    mock_transport(lambda req: httpx.Response(status))
    ok, reason = await probe_service_health("http://host:8100/v1", "tok")
    assert ok is True
    assert reason is None


@pytest.mark.asyncio
async def test_connect_error_is_unreachable(mock_transport):
    def handler(req):
        raise httpx.ConnectError("All connection attempts failed")

    mock_transport(handler)
    ok, reason = await probe_service_health("http://host:8100/v1", "tok")
    assert ok is False
    assert "All connection attempts failed" in reason


@pytest.mark.asyncio
async def test_timeout_is_unreachable(mock_transport):
    def handler(req):
        raise httpx.ConnectTimeout("timed out")

    mock_transport(handler)
    ok, reason = await probe_service_health("http://host:8100/v1", "tok")
    assert ok is False
    assert "timed out" in reason


@pytest.mark.asyncio
async def test_blank_base_url_is_unreachable_without_calling(mock_transport):
    called = {"n": 0}

    def handler(req):
        called["n"] += 1
        return httpx.Response(200)

    mock_transport(handler)
    ok, reason = await probe_service_health("  ", "tok")
    assert ok is False
    assert reason == "no base_url configured"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_passes_timeout_to_client(mock_transport):
    seen = mock_transport(lambda req: httpx.Response(200))
    await probe_service_health("http://host:8100/v1", "tok", timeout=1.5)
    assert seen["timeout"] == 1.5


@pytest.mark.asyncio
async def test_malformed_base_url_is_unreachable_not_raised():
    """httpx.InvalidURL (e.g. a malformed base_url typo'd into the Model
    Registry UI, like a stray '[' in an IPv6-looking host) is NOT an
    httpx.HTTPError subclass -- it must still degrade to (False, reason)
    rather than escape and crash the WS connect path. No mock_transport here:
    InvalidURL is raised during request construction, before any transport
    is ever reached."""
    ok, reason = await probe_service_health("http://[::1/v1", "tok")
    assert ok is False
    assert reason
