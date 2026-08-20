"""The knowledge block has to be reachable through the HTTP write path.

`ProfileRequest` omitted `knowledge` entirely, and every write goes through it
(`routes/profiles.py` create and update both do `payload.model_dump()` ->
`Profile(**data)` -> `upsert`). So `enabled`/`collection`/`description` could
not be set at all except by hand-editing SQLite -- and because PUT is
full-replace, the first save from the profile editor reset any block that had
been set that way.

Omission is preserve-on-omit, not reset: the same shape `shared` and
`llm.api_key` already use in update_profile. `static/js/profiles.js` sends no
`knowledge` key at all, and neither does any other client written before this
branch; resetting on omission would mean the admin UI silently disables the
feature every time someone edits an unrelated field. An explicit block still
overwrites wholesale, so it stays switch-off-able.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.profiles.store import ProfileStore

_BLOCK = {
    "enabled": True,
    "collection": "faq",
    "description": "Tra cứu sổ tay bảo hành và chính sách đổi trả",
    "top_k": 3,
    "min_score": 0.5,
    "embed_model": "text-embedding-3-small",
}


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    fresh = ProfileStore(str(tmp_path / "profiles.json"))
    monkeypatch.setattr("app.api.routes.profiles.profile_store", fresh)
    monkeypatch.setattr("app.services.profiles.store.profile_store", fresh)
    return fresh


@pytest.fixture
def client():
    return TestClient(app)


def test_a_knowledge_block_survives_the_http_boundary(client):
    resp = client.put("/v1/profiles/shop", json={"name": "shop", "knowledge": _BLOCK})
    assert resp.status_code == 200, resp.text

    got = client.get("/v1/profiles/shop").json()["data"]["knowledge"]
    assert got["enabled"] is True
    assert got["collection"] == "faq"
    # The description IS the feature -- a dropped one makes the tool useless.
    assert got["description"] == _BLOCK["description"]
    assert got["top_k"] == 3
    assert got["min_score"] == 0.5
    assert got["embed_model"] == "text-embedding-3-small"


def test_create_accepts_a_knowledge_block(client):
    resp = client.post("/v1/profiles", json={"name": "shop2", "knowledge": _BLOCK})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["knowledge"]["collection"] == "faq"


def test_a_put_that_omits_knowledge_preserves_the_stored_block(client):
    client.put("/v1/profiles/shop", json={"name": "shop", "knowledge": _BLOCK})
    # Exactly what static/js/profiles.js sends: no `knowledge` key at all.
    resp = client.put("/v1/profiles/shop", json={"name": "shop", "nickname": "Cửa hàng"})
    assert resp.status_code == 200, resp.text

    got = client.get("/v1/profiles/shop").json()["data"]
    assert got["nickname"] == "Cửa hàng"
    assert got["knowledge"]["enabled"] is True
    assert got["knowledge"]["collection"] == "faq"
    assert got["knowledge"]["description"] == _BLOCK["description"]


def test_an_explicit_knowledge_block_still_overwrites(client):
    """Preserve-on-omit must not become write-once: sending the block turns it off."""
    client.put("/v1/profiles/shop", json={"name": "shop", "knowledge": _BLOCK})
    resp = client.put(
        "/v1/profiles/shop",
        json={"name": "shop", "knowledge": {"enabled": False, "collection": ""}},
    )
    assert resp.status_code == 200, resp.text

    got = client.get("/v1/profiles/shop").json()["data"]["knowledge"]
    assert got["enabled"] is False
    assert got["collection"] == ""


def test_a_profile_that_never_had_a_block_still_gets_the_defaults(client):
    client.put("/v1/profiles/plain", json={"name": "plain"})
    got = client.get("/v1/profiles/plain").json()["data"]["knowledge"]
    assert got["enabled"] is False
    assert got["collection"] == ""
