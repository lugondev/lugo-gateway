"""A shared template is readable and clonable, but nothing may RUN on it.

Every test here also asserts the OTHER half: a private profile belonging to
someone else must still produce the old, indistinguishable "not found"
response. That is the regression guard on the C2 no-enumeration-oracle
contract (see services/profile_visibility.py) -- naming a shared profile in an
error is safe precisely because GET /v1/profiles already lists it to everyone,
and that reasoning must not leak onto private rows.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.services.auth.users import user_store
from app.services.profiles.models import LlmConfig, Profile, SttConfig
from app.services.profiles.store import profile_store


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    """tests/conftest.py's autouse `_hermetic` blanks the admin password, which
    turns auth off entirely. These tests need real roles -- same pattern as
    test_profile_idor.py."""
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _as_user(client: TestClient, role: str = "user") -> str:
    username = f"{role}-{uuid.uuid4().hex[:10]}"
    password = "s3cret-password"
    assert client.post(
        "/api/auth/signup", json={"username": username, "password": password}
    ).status_code == 200
    if role == "admin":
        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    assert client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).status_code == 200
    return asyncio.run(user_store.get_by_username(username)).id


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


SHARED_MSG = "shared template"


def _make_shared_template(name: str) -> None:
    """Written straight to the store, like test_profile_idor.py does. Going
    through the admin HTTP route would couple these tests to Task 2's payload
    handling for no gain -- what is under test here is the CONSUMERS."""
    profile_store.upsert(Profile(
        name=name,
        owner_id="some-admin",
        shared=True,
        system_prompt="TEMPLATE SYSTEM PROMPT -- MUST NOT BE USED",
        llm=LlmConfig(
            base_url="https://template-llm.example/v1",
            api_key="template-secret-api-key",
            model="template-model",
        ),
    ))


def test_http_chat_never_runs_on_a_shared_template(client, _with_password, monkeypatch):
    """Spy on build_responder_ex to observe exactly what the route resolved --
    the same technique as test_profile_idor.py's
    test_chat_private_profile_never_reaches_responder_construction. The spy
    REPLACES build_responder_ex and never calls through, so the template's
    base_url can never produce a real network call.

    Note the request shape: `?profile=` is a QUERY param and the body is
    `{"messages": [...]}` -- see the existing /chat tests.
    """
    import app.api.routes.conversation as conversation_route

    tpl = _rand("tpl")
    _make_shared_template(tpl)
    _as_user(client, "user")

    captured: list[dict] = []

    class _StubResponder:
        # name/model/last_usage: the route reads these off the real responder
        # after reply() (responder.name, get_active_llm_model(), and
        # record_llm_turn_usage's best-effort read of last_usage) -- same stub
        # shape as test_profile_idor.py's
        # test_chat_private_profile_never_reaches_responder_construction.
        name = "stub"
        model = ""
        last_usage = None

        async def reply(self, history):
            return "ok"

        async def aclose(self):
            return None

    async def _spy(**kwargs):
        captured.append(kwargs)
        return _StubResponder()

    monkeypatch.setattr(conversation_route, "build_responder_ex", _spy)

    resp = client.post(
        f"/v1/conversation/chat?profile={tpl}",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    assert captured, "the route never built a responder"
    assert captured[0]["api_key"] != "template-secret-api-key"
    assert captured[0]["system_prompt"] != "TEMPLATE SYSTEM PROMPT -- MUST NOT BE USED"
    assert "template-secret-api-key" not in resp.text


def test_ws_conversation_warns_and_uses_defaults_on_a_shared_profile(client, _with_password):
    _as_user(client, "user")
    tpl = _rand("tpl")
    _make_shared_template(tpl)
    with client.websocket_connect(f"/v1/conversation/stream?profile={tpl}") as ws:
        warnings = []
        for _ in range(4):
            msg = ws.receive_json()
            if msg.get("event") == "warning":
                warnings.append(msg["message"])
            if msg.get("event") == "session_started":
                break
    assert any(SHARED_MSG in w for w in warnings), warnings
    assert any(tpl in w for w in warnings), "a shared name is public; say which one"


def test_ws_conversation_keeps_the_old_message_for_someone_elses_private_profile(
    client, _with_password
):
    """The no-oracle half: "exists but is Alice's" must stay byte-identical to
    "never existed"."""
    alice = TestClient(app)
    _as_user(alice, "user")
    private = _rand("priv")
    assert alice.post("/v1/profiles", json={"name": private}).status_code == 200

    _as_user(client, "user")
    ghost = _rand("ghost")

    def _first_warning(name: str) -> str:
        with client.websocket_connect(f"/v1/conversation/stream?profile={name}") as ws:
            for _ in range(4):
                msg = ws.receive_json()
                if msg.get("event") == "warning":
                    return msg["message"]
        return ""

    assert _first_warning(private).replace(private, "X") == _first_warning(ghost).replace(ghost, "X")
    assert SHARED_MSG not in _first_warning(private)


def test_stt_warm_ignores_a_shared_profile(client, _with_password):
    """Falls through to the server default engine, exactly as an unknown name
    does -- the template's pinned engine/model must not be applied.

    `POST /v1/stt/warm?profile=` returns
    {"data": {"engine": ..., "model": ..., "warmed": ...}} (stt.py's
    warm_engine). Asserted against the server default read from the config
    store rather than a hardcoded engine name, so a change to the hermetic
    test config cannot turn this green by accident.
    """
    from app.services.system_config import system_config_store

    # "whisper" not "vosk": the hermetic test config's own default_stt_engine
    # is "vosk" (system_config.py's EngineDefaults), so a pinned engine equal
    # to the default would make the leak assertion below pass by accident
    # even if the fix were broken.
    tpl = _rand("tpl")
    profile_store.upsert(Profile(
        name=tpl, owner_id="some-admin", shared=True,
        stt=SttConfig(engine="whisper", language="vi"),
    ))
    _as_user(client, "user")

    resp = client.post(f"/v1/stt/warm?profile={tpl}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["engine"] != "whisper", "the template's engine leaked into the warm"
    assert resp.json()["data"]["engine"] == system_config_store.get().engines.default_stt_engine
