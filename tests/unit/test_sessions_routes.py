import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.history.store import session_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def seeded(client):
    import asyncio

    async def _seed():
        await session_store.create("s1", profile_id="pet")
        await session_store.append_message("s1", 1, "user", "hello")
        await session_store.append_message("s1", 1, "assistant", "hi there")
        await session_store.create("s2", profile_id="other")

    asyncio.run(_seed())


def test_list_sessions(client, seeded):
    resp = client.get("/v1/sessions", params={"profile": "pet"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["id"] == "s1"
    assert data[0]["preview"] == "hello"
    assert data[0]["message_count"] == 2


def test_get_session_with_messages(client, seeded):
    resp = client.get("/v1/sessions/s1")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["profile_id"] == "pet"
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]


def test_get_missing_session_404(client):
    assert client.get("/v1/sessions/ghost").status_code == 404


def test_delete_session(client, seeded):
    resp = client.delete("/v1/sessions/s1")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    assert client.get("/v1/sessions/s1").status_code == 404


def test_delete_missing_404(client):
    assert client.delete("/v1/sessions/ghost").status_code == 404


def test_bulk_delete(client, seeded):
    resp = client.post("/v1/sessions/bulk_delete", json={"ids": ["s1", "s2", "ghost"]})
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == 2
    assert client.get("/v1/sessions/s1").status_code == 404
    assert client.get("/v1/sessions/s2").status_code == 404


def test_bulk_delete_empty_list(client, seeded):
    resp = client.post("/v1/sessions/bulk_delete", json={"ids": []})
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == 0
    assert client.get("/v1/sessions/s1").status_code == 200


def test_clear_only_empty_in_scope(client, seeded):
    # s1 (profile "pet") has messages; s2 (profile "other") is empty.
    resp = client.delete("/v1/sessions", params={"only_empty": "true"})
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == 1
    assert client.get("/v1/sessions/s1").status_code == 200
    assert client.get("/v1/sessions/s2").status_code == 404


def test_clear_all_scoped_to_profile(client, seeded):
    resp = client.delete("/v1/sessions", params={"profile": "pet"})
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == 1
    assert client.get("/v1/sessions/s1").status_code == 404
    # other profile untouched
    assert client.get("/v1/sessions/s2").status_code == 200


def test_clear_all_no_profile_clears_everything(client, seeded):
    resp = client.delete("/v1/sessions")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == 2
    assert client.get("/v1/sessions").json()["data"] == []
