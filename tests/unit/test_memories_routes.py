import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.db import engine as db_engine


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_engine.configure(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield
    db_engine.configure()


@pytest.fixture
def client():
    return TestClient(app)


def test_memories_crud(client):
    # empty
    assert client.get("/v1/profiles/pet/memories").json()["data"] == []
    # add
    resp = client.post("/v1/profiles/pet/memories", json={"content": "likes tea"})
    assert resp.status_code == 200
    mid = resp.json()["data"]["id"]
    # list
    rows = client.get("/v1/profiles/pet/memories").json()["data"]
    assert len(rows) == 1 and rows[0]["content"] == "likes tea"
    # edit
    resp = client.put(f"/v1/profiles/pet/memories/{mid}", json={"content": "likes coffee"})
    assert resp.json()["data"]["content"] == "likes coffee"
    # edit missing -> 404
    assert client.put("/v1/profiles/pet/memories/ghost", json={"content": "x"}).status_code == 404
    # delete one
    assert client.delete(f"/v1/profiles/pet/memories/{mid}").json()["data"]["deleted"] is True
    assert client.delete(f"/v1/profiles/pet/memories/{mid}").status_code == 404


def test_delete_all_memories(client):
    client.post("/v1/profiles/pet/memories", json={"content": "a"})
    client.post("/v1/profiles/pet/memories", json={"content": "b"})
    resp = client.delete("/v1/profiles/pet/memories")
    assert resp.json()["data"]["deleted"] == 2
    assert client.get("/v1/profiles/pet/memories").json()["data"] == []


def test_empty_content_rejected(client):
    assert client.post("/v1/profiles/pet/memories", json={"content": "  "}).status_code == 422
