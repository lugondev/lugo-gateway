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
