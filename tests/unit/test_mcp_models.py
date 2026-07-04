from app.services.mcp.models import McpServer


def test_headers_default_empty():
    s = McpServer(name="fs", url="http://localhost:3002/mcp")
    assert s.headers == {}


def test_headers_custom():
    s = McpServer(name="fs", url="http://localhost:3002/mcp", headers={"X-API-Key": "secret"})
    assert s.headers == {"X-API-Key": "secret"}


def test_enabled_defaults_true():
    s = McpServer(name="fs", url="http://localhost:3002/mcp")
    assert s.enabled is True


def test_enabled_can_be_false():
    s = McpServer(name="fs", url="http://localhost:3002/mcp", enabled=False)
    assert s.enabled is False
