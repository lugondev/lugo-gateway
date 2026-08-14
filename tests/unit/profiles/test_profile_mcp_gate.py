"""C1 regression: Profile.mcp_servers is a user-writable field that
_build_tool_registry (app/services/conversation/session.py) merges into the
SAME fetch path as admin-managed MCP rows, with profile entries winning on
name collision, using the entry's own url+headers. Task 6 (a previous branch)
gated /v1/mcp/servers writes to admins only -- but a non-admin could set
mcp_servers directly on their OWN profile via POST/PUT /v1/profiles and get
the identical SSRF-with-reflection primitive (arbitrary url, e.g.
http://169.254.169.254/..., fetched by the gateway and reflected into the LLM
turn), fully bypassing that gate.

Fix (apps/api_gateway/app/api/routes/profiles.py): create/clone force
mcp_servers=[] for a non-admin; update preserves whatever was already stored.
Silently dropped rather than 403'd (see task-3-report.md for the rationale) --
these tests assert the malicious entries never persist, not that the request
itself errors.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth.users import user_store
from app.services.profiles.store import profile_store


@pytest.fixture
def client():
    return TestClient(app)


def _as_user(client: TestClient, role: str) -> str:
    """Same helper as test_conversation_authz.py -- duplicated locally so this
    file has no import-order coupling to that module."""
    username = f"{role}-{uuid.uuid4().hex[:10]}"
    password = "s3cret-password"
    signup = client.post("/api/auth/signup", json={"username": username, "password": password})
    assert signup.status_code == 200, signup.text
    if role == "admin":
        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return asyncio.run(user_store.get_by_username(username)).id


_MALICIOUS_SERVERS = [
    {"name": "ssrf", "url": "http://169.254.169.254/latest/meta-data/", "headers": {"X-Attack": "1"}}
]


def test_nonadmin_create_drops_mcp_servers(client):
    _as_user(client, "user")
    resp = client.post("/v1/profiles", json={
        "name": "p-" + uuid.uuid4().hex[:8],
        "mcp_servers": _MALICIOUS_SERVERS,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["mcp_servers"] == []

    stored = profile_store.get(resp.json()["data"]["name"])
    assert stored.mcp_servers == []


def test_nonadmin_update_cannot_add_mcp_servers(client):
    _as_user(client, "user")
    name = "p-" + uuid.uuid4().hex[:8]
    create = client.post("/v1/profiles", json={"name": name})
    assert create.status_code == 200, create.text

    resp = client.put(f"/v1/profiles/{name}", json={
        "name": name,
        "mcp_servers": _MALICIOUS_SERVERS,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["mcp_servers"] == []
    assert profile_store.get(name).mcp_servers == []


def test_nonadmin_update_cannot_change_existing_mcp_servers(client):
    """Even when a row somehow already has admin-set mcp_servers (e.g. an
    admin created it, or a pre-fix row), a non-admin PUT must not be able to
    swap them out for their own -- the existing value must be preserved
    unchanged, not merely cleared."""
    _as_user(client, "admin")
    name = "p-" + uuid.uuid4().hex[:8]
    good_server = {"name": "internal", "url": "https://internal.example/mcp", "headers": {}}
    create = client.post("/v1/profiles", json={"name": name, "mcp_servers": [good_server]})
    assert create.status_code == 200, create.text
    assert create.json()["data"]["mcp_servers"][0]["url"] == "https://internal.example/mcp"

    other_client = TestClient(app)
    _as_user(other_client, "user")
    resp = other_client.put(f"/v1/profiles/{name}", json={
        "name": name,
        "mcp_servers": _MALICIOUS_SERVERS,
    })
    # Non-owner, non-admin PUT to someone else's row 404s regardless -- but
    # confirm the row genuinely never changed either way.
    assert resp.status_code == 404, resp.text
    assert profile_store.get(name).mcp_servers[0].url == "https://internal.example/mcp"


def test_owner_update_cannot_add_mcp_servers_to_own_profile(client):
    """The realistic H2/C1 attack shape: a non-admin owns their own profile
    and tries to attach mcp_servers to it via PUT."""
    _as_user(client, "user")
    name = "p-" + uuid.uuid4().hex[:8]
    create = client.post("/v1/profiles", json={"name": name})
    assert create.status_code == 200, create.text
    assert create.json()["data"]["mcp_servers"] == []

    resp = client.put(f"/v1/profiles/{name}", json={
        "name": name,
        "mcp_servers": _MALICIOUS_SERVERS,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["mcp_servers"] == []
    assert profile_store.get(name).mcp_servers == []


def test_nonadmin_clone_of_admin_template_drops_mcp_servers(client):
    """Cloning an admin template that legitimately has mcp_servers must not
    hand the cloning (now-owning) non-admin user a live copy of them."""
    admin_client = TestClient(app)
    _as_user(admin_client, "admin")
    template_name = "tmpl-" + uuid.uuid4().hex[:8]
    good_server = {"name": "internal", "url": "https://internal.example/mcp", "headers": {}}
    create = admin_client.post("/v1/profiles", json={"name": template_name, "mcp_servers": [good_server], "shared": True})
    assert create.status_code == 200, create.text
    assert len(create.json()["data"]["mcp_servers"]) == 1

    _as_user(client, "user")
    clone_name = "clone-" + uuid.uuid4().hex[:8]
    resp = client.post(f"/v1/profiles/{template_name}/clone", json={"new_name": clone_name})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["mcp_servers"] == []
    assert profile_store.get(clone_name).mcp_servers == []
    # The template itself is untouched.
    assert len(profile_store.get(template_name).mcp_servers) == 1


def test_admin_create_can_set_mcp_servers(client):
    _as_user(client, "admin")
    name = "p-" + uuid.uuid4().hex[:8]
    resp = client.post("/v1/profiles", json={"name": name, "mcp_servers": _MALICIOUS_SERVERS})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]["mcp_servers"]) == 1
    assert profile_store.get(name).mcp_servers[0].url == _MALICIOUS_SERVERS[0]["url"]


def test_admin_update_can_change_mcp_servers(client):
    _as_user(client, "admin")
    name = "p-" + uuid.uuid4().hex[:8]
    create = client.post("/v1/profiles", json={"name": name})
    assert create.status_code == 200, create.text

    resp = client.put(f"/v1/profiles/{name}", json={"name": name, "mcp_servers": _MALICIOUS_SERVERS})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]["mcp_servers"]) == 1
    assert profile_store.get(name).mcp_servers[0].url == _MALICIOUS_SERVERS[0]["url"]
