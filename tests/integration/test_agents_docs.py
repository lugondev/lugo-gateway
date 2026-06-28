from fastapi.testclient import TestClient

from app.main import app


def test_agents_docs_bundle():
    r = TestClient(app).get("/agents-docs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    body = r.text
    assert "AGENTS.md" in body
    assert "docs/api.md" in body and "docs/device-integration.md" in body
    assert "WS /v1/conversation/stream" in body
