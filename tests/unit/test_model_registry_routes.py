import pytest
from fastapi.testclient import TestClient

from app.core.settings import settings
from app.main import app
from app.schemas.tts import TTSResult
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


class _OkStub(STTProvider):
    name = "stub-registry-ok"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        from app.schemas.stt import STTResult
        return STTResult(engine=self.name, text="ok", is_final=True)


class _FailStub(STTProvider):
    name = "stub-registry-fail"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        raise RuntimeError("engine unavailable")


class _TtsOkStub(TTSProvider):
    name = "stub-tts-registry-ok"

    async def synthesize(self, payload):
        return TTSResult(engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav", text=payload.text)


@pytest.fixture(autouse=True)
def _register_stubs():
    stt_service.providers["stub-registry-ok"] = _OkStub()
    stt_service.providers["stub-registry-fail"] = _FailStub()
    tts_service.providers["stub-tts-registry-ok"] = _TtsOkStub()
    yield
    stt_service.providers.pop("stub-registry-ok", None)
    stt_service.providers.pop("stub-registry-fail", None)
    tts_service.providers.pop("stub-tts-registry-ok", None)


def test_regular_user_cannot_reach_model_registry(client, _with_password):
    _signup_login(client, "toan", role="user")
    resp = client.get("/v1/model_registry")
    assert resp.status_code == 403


def test_create_stt_entry_runs_real_test_call_and_succeeds(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True


def test_create_stt_entry_test_call_fails_rejects_and_does_not_persist(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-fail", "model_id": "v1", "label": "Stub Fail",
    })
    assert resp.status_code == 400
    listed = client.get("/v1/model_registry").json()["data"]
    assert not any(e["engine"] == "stub-registry-fail" for e in listed)


def test_create_tts_entry_runs_real_test_call(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "tts", "engine": "stub-tts-registry-ok", "model_id": "stub-tts-registry-ok",
        "label": "Stub TTS OK", "sample_text": "xin chào",
    })
    assert resp.status_code == 200


def test_patch_toggles_enabled_and_stage_without_retest(client, _with_password):
    _signup_login(client, "root", role="admin")
    created = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
    }).json()["data"]
    resp = client.patch(f"/v1/model_registry/{created['id']}", json={"enabled": False, "stage": "testing"})
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False
    assert resp.json()["data"]["stage"] == "testing"
