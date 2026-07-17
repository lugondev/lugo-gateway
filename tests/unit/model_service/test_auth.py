import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from model_service.app.auth import make_auth_dependency


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/guarded", dependencies=[Depends(make_auth_dependency("s3cret"))])
    def guarded():
        return {"ok": True}

    return TestClient(app)


def test_accepts_the_right_token(client):
    r = client.get("/guarded", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_rejects_a_wrong_token(client):
    r = client.get("/guarded", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_rejects_a_missing_header(client):
    assert client.get("/guarded").status_code == 401


def test_rejects_a_non_bearer_scheme(client):
    r = client.get("/guarded", headers={"Authorization": "Basic s3cret"})
    assert r.status_code == 401


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
def test_accepts_any_casing_of_the_bearer_scheme(client, scheme):
    # RFC 7235: auth-scheme names are case-insensitive.
    r = client.get("/guarded", headers={"Authorization": f"{scheme} s3cret"})
    assert r.status_code == 200


def test_still_rejects_a_wrong_token_regardless_of_scheme_casing(client):
    # Case-insensitivity must apply only to the scheme, not the token.
    r = client.get("/guarded", headers={"Authorization": "BEARER nope"})
    assert r.status_code == 401
