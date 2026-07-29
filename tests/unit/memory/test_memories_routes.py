import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.profiles.models import Profile
from app.services.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _profiles(tmp_path, monkeypatch):
    # The memories routes now gate on the parent profile's ownership, so these
    # profiles must exist for the CRUD under test to be reachable at all.
    # owner_id=None (template) + dev-mode role "admin" keeps writes allowed --
    # ownership itself is covered by tests/unit/memory/test_memory_ownership.py.
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    for name in ("pet", "other"):
        fresh.upsert(Profile(name=name))
    monkeypatch.setattr("app.api.routes.memories.profile_store", fresh)
    monkeypatch.setattr("app.api.routes.profiles.profile_store", fresh)
    monkeypatch.setattr("app.services.profiles.store.profile_store", fresh)


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


def test_cross_profile_isolation(client):
    mid = client.post("/v1/profiles/pet/memories", json={"content": "secret"}).json()["data"]["id"]
    # another profile's URL cannot edit or delete it
    assert client.put(f"/v1/profiles/other/memories/{mid}", json={"content": "hax"}).status_code == 404
    assert client.delete(f"/v1/profiles/other/memories/{mid}").status_code == 404
    # owner still can
    assert client.delete(f"/v1/profiles/pet/memories/{mid}").status_code == 200
