import asyncio

import pytest

from app.schemas.health import EngineHealth
from app.services.health import check_profile_health, check_resolved_engines


@pytest.mark.asyncio
async def test_runs_both_checks_concurrently(monkeypatch):
    """Both engines can be remote; running them in series would double the
    worst-case connect latency a user waits through."""
    order = []

    async def slow_stt(engine, model=""):
        order.append("stt_start")
        await asyncio.sleep(0.05)
        order.append("stt_end")
        return EngineHealth(engine=engine, status="ok")

    async def slow_tts(engine, model_id=""):
        order.append("tts_start")
        await asyncio.sleep(0.05)
        order.append("tts_end")
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", slow_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", slow_tts)

    await check_resolved_engines("http_stt", "", "http_tts", "")
    # Interleaved starts prove gather, not sequential awaits.
    assert order[:2] == ["stt_start", "tts_start"]


@pytest.mark.asyncio
async def test_returns_both_healths_in_order(monkeypatch):
    async def fake_stt(engine, model=""):
        return EngineHealth(engine=engine, status="unavailable", detail="down")

    async def fake_tts(engine, model_id=""):
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", fake_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", fake_tts)

    stt, tts = await check_resolved_engines("http_stt", "m1", "vieneu", "")
    assert stt.engine == "http_stt" and stt.status == "unavailable"
    assert tts.engine == "vieneu" and tts.status == "ok"


@pytest.mark.asyncio
async def test_check_profile_health_resolves_from_profile(monkeypatch, tmp_path):
    from app.services.profiles.models import Profile, SttConfig
    from app.services.profiles.store import ProfileStore

    store = ProfileStore(str(tmp_path / "p.json"))
    store.upsert(Profile(name="dev", stt=SttConfig(engine="http_stt", model="m1")))
    monkeypatch.setattr("app.services.health.profile_store", store)

    seen = {}

    async def fake_stt(engine, model=""):
        seen["stt"] = (engine, model)
        return EngineHealth(engine=engine, status="ok")

    async def fake_tts(engine, model_id=""):
        seen["tts"] = (engine, model_id)
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", fake_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", fake_tts)

    health = await check_profile_health("dev")
    assert health.profile == "dev"
    assert seen["stt"] == ("http_stt", "m1")


@pytest.mark.asyncio
async def test_check_profile_health_unknown_profile_uses_defaults(monkeypatch, tmp_path):
    from app.services.profiles.store import ProfileStore

    monkeypatch.setattr("app.services.health.profile_store", ProfileStore(str(tmp_path / "p.json")))

    async def fake_stt(engine, model=""):
        return EngineHealth(engine=engine, status="ok")

    async def fake_tts(engine, model_id=""):
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", fake_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", fake_tts)

    health = await check_profile_health("ghost")
    assert health.profile == "ghost"
    assert health.stt.status == "ok"


def test_health_endpoint_returns_profile_health(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.profiles.models import Profile, SttConfig
    from app.services.profiles.store import ProfileStore

    store = ProfileStore(str(tmp_path / "ep.json"))
    store.upsert(Profile(name="dev", stt=SttConfig(engine="http_stt")))
    monkeypatch.setattr("app.services.health.profile_store", store)

    async def fake_stt(engine, model=""):
        return EngineHealth(engine=engine, status="unavailable", detail="unreachable")

    async def fake_tts(engine, model_id=""):
        return EngineHealth(engine=engine, status="ok")

    monkeypatch.setattr("app.services.health.stt_service.check_engine", fake_stt)
    monkeypatch.setattr("app.services.health.tts_service.check_engine", fake_tts)

    resp = TestClient(app).get("/v1/profiles/dev/health")
    assert resp.status_code == 200
    resp_json = resp.json()
    assert resp_json["success"] is True
    data = resp_json["data"]
    assert data["profile"] == "dev"
    assert data["stt"]["status"] == "unavailable"
    assert data["stt"]["detail"] == "unreachable"
    assert data["tts"]["status"] == "ok"


def test_health_endpoint_404s_for_another_users_private_profile(monkeypatch):
    """Mirrors test_get_other_users_private_profile_is_404 in
    test_profile_ownership.py: GET /{name}/health must apply the same
    ownership scoping as every sibling profile route, not just report
    unavailable. Without it, any logged-in user could confirm another user's
    private profile exists by name and read its resolved engine + verbatim
    base_url (EngineHealth.detail) through this endpoint alone."""
    from fastapi.testclient import TestClient

    from app.core.settings import settings
    from app.main import app

    monkeypatch.setattr(settings, "admin_password", "s3cret")

    client = TestClient(app)
    client.post("/api/auth/signup", json={"username": "a", "password": "pw"})
    client.post("/api/auth/login", json={"username": "a", "password": "pw"})
    client.post("/v1/profiles", json={"name": "a-private"})

    client.post("/api/auth/signup", json={"username": "b", "password": "pw"})
    client.post("/api/auth/login", json={"username": "b", "password": "pw"})
    resp = client.get("/v1/profiles/a-private/health")
    assert resp.status_code == 404
