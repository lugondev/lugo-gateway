import pytest

from app.schemas.health import EngineHealth, ProfileHealth
from app.services.stt.service import stt_service
from app.services.tts.service import tts_service


def test_unavailable_blocks_session():
    assert EngineHealth(engine="http_stt", status="unavailable", detail="down").blocks_session is True


def test_ok_does_not_block():
    assert EngineHealth(engine="vosk", status="ok").blocks_session is False


def test_not_ready_does_not_block():
    """A local engine still loading its model is not a failure -- session_started
    already reports stt_ready/tts_ready for this case."""
    assert EngineHealth(engine="whisper", status="not_ready").blocks_session is False


def test_detail_defaults_to_empty_string():
    assert EngineHealth(engine="vosk", status="ok").detail == ""


def test_profile_health_serializes_nested_engines():
    payload = ProfileHealth(
        profile="default",
        stt=EngineHealth(engine="http_stt", status="unavailable", detail="unreachable"),
        tts=EngineHealth(engine="vieneu", status="ok"),
    ).model_dump()
    assert payload["profile"] == "default"
    assert payload["stt"]["status"] == "unavailable"
    assert payload["stt"]["detail"] == "unreachable"
    assert payload["tts"]["engine"] == "vieneu"


_STT_ROW = {
    "id": "s1", "kind": "stt", "engine": "http_stt", "model_id": "Qwen/Qwen3-ASR-0.6B",
    "label": "local", "enabled": True, "stage": "stable",
    "api_key": "tok", "base_url": "http://127.0.0.1:8100/v1", "config": {},
}
_TTS_ROW = {
    "id": "t1", "kind": "tts", "engine": "http_tts", "model_id": "vieneu",
    "label": "local", "enabled": True, "stage": "stable",
    "api_key": "tok", "base_url": "http://127.0.0.1:8101/v1", "config": {},
}


def _patch_probe(monkeypatch, target: str, ok: bool, reason: str | None):
    async def fake_probe(base_url, api_key, timeout=3.0):
        return ok, reason

    monkeypatch.setattr(target, fake_probe)


@pytest.mark.asyncio
async def test_stt_unknown_engine_is_unavailable():
    health = await stt_service.check_engine("no_such_engine")
    assert health.status == "unavailable"
    assert health.engine == "no_such_engine"


@pytest.mark.asyncio
async def test_stt_http_stt_unavailable_when_no_row(monkeypatch):
    async def no_row(kind, engine, model_id=""):
        return None

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", no_row)
    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find_enabled",
        lambda kind, engine=None: no_row(kind, engine))
    health = await stt_service.check_engine("http_stt")
    assert health.status == "unavailable"
    assert "not configured" in health.detail


@pytest.mark.asyncio
async def test_stt_http_stt_unavailable_when_probe_fails(monkeypatch):
    async def row(kind, engine, model_id=""):
        return dict(_STT_ROW)

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", row)
    _patch_probe(monkeypatch, "app.services.stt.service.probe_service_health",
                 False, "All connection attempts failed")
    health = await stt_service.check_engine("http_stt", "Qwen/Qwen3-ASR-0.6B")
    assert health.status == "unavailable"
    assert "All connection attempts failed" in health.detail


@pytest.mark.asyncio
async def test_stt_http_stt_ok_when_probe_succeeds(monkeypatch):
    async def row(kind, engine, model_id=""):
        return dict(_STT_ROW)

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", row)
    _patch_probe(monkeypatch, "app.services.stt.service.probe_service_health", True, None)
    health = await stt_service.check_engine("http_stt", "Qwen/Qwen3-ASR-0.6B")
    assert health.status == "ok"


@pytest.mark.asyncio
async def test_stt_local_engine_not_ready_while_warming(monkeypatch):
    monkeypatch.setattr("app.services.stt.service.is_ready", lambda p: False)
    monkeypatch.setattr("app.services.stt.service._needs_warming", lambda p: True)
    health = await stt_service.check_engine("vosk")
    assert health.status == "not_ready"


@pytest.mark.asyncio
async def test_stt_local_engine_ok_when_warm(monkeypatch):
    monkeypatch.setattr("app.services.stt.service.is_ready", lambda p: True)
    monkeypatch.setattr("app.services.stt.service._needs_warming", lambda p: True)
    health = await stt_service.check_engine("vosk")
    assert health.status == "ok"


@pytest.mark.asyncio
async def test_stt_cloud_engine_is_never_probed(monkeypatch):
    """qwencloud has no free health endpoint -- config check only, no network."""
    called = {"n": 0}

    async def spy(base_url, api_key, timeout=3.0):
        called["n"] += 1
        return True, None

    monkeypatch.setattr("app.services.stt.service.probe_service_health", spy)
    await stt_service.check_engine("qwencloud")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_tts_unknown_engine_is_unavailable():
    health = await tts_service.check_engine("no_such_engine")
    assert health.status == "unavailable"


@pytest.mark.asyncio
async def test_tts_http_tts_unavailable_when_probe_fails(monkeypatch):
    async def row(kind, engine, model_id=""):
        return dict(_TTS_ROW)

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", row)
    _patch_probe(monkeypatch, "app.services.tts.service.probe_service_health",
                 False, "All connection attempts failed")
    health = await tts_service.check_engine("http_tts", "vieneu")
    assert health.status == "unavailable"
    assert "All connection attempts failed" in health.detail


@pytest.mark.asyncio
async def test_tts_http_tts_ok_when_probe_succeeds(monkeypatch):
    async def row(kind, engine, model_id=""):
        return dict(_TTS_ROW)

    monkeypatch.setattr(
        "app.services.model_registry.store.model_registry_store.find", row)
    _patch_probe(monkeypatch, "app.services.tts.service.probe_service_health", True, None)
    health = await tts_service.check_engine("http_tts", "vieneu")
    assert health.status == "ok"


@pytest.mark.asyncio
async def test_tts_local_engine_unavailable_when_provider_says_so(monkeypatch):
    monkeypatch.setattr(
        tts_service.providers["vieneu"], "available", lambda: False, raising=False)
    health = await tts_service.check_engine("vieneu")
    assert health.status == "unavailable"
