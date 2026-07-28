import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.health import EngineHealth
from app.services.stt.service import stt_service
from app.services.system_config import system_config_store
from app.services.tts.service import tts_service


@pytest.fixture
def client():
    return TestClient(app)


async def _ok_health(stt_engine, stt_model, tts_engine, tts_model):
    return (
        EngineHealth(engine=stt_engine, status="ok"),
        EngineHealth(engine=tts_engine, status="ok"),
    )


@pytest.fixture(autouse=True)
def _pin_conversation_engines(monkeypatch):
    """Pin the session's engines to the ones these tests stub. The readiness
    check consults the CONFIGURED default engines (system config), so
    when the default tts engine changed (vieneu -> omnivoice) these tests
    silently started probing the real omnivoice provider's warm state instead
    of the fakes -- failing on any machine where it isn't warm."""
    _real_get = system_config_store.get

    def _get_pinned():
        cfg = _real_get()
        return cfg.model_copy(update={
            "engines": cfg.engines.model_copy(update={
                "default_stt_engine": "whisper",
                "default_tts_engine": "vieneu",
            })
        })

    monkeypatch.setattr(system_config_store, "get", _get_pinned)
    # These tests exercise the cold/warm readiness signal via fake providers
    # registered ad hoc under "whisper"/"vieneu" -- not the real faster-whisper
    # install or the (never-installed, per repo hermeticity rules) vieneu
    # extra. The Task 7 health gate's check_resolved_engines() would otherwise
    # consult the real engine listing for those names and refuse the session
    # before session_started is even emitted. Stub it out; this file tests
    # readiness reporting, not the gate.
    monkeypatch.setattr("app.api.routes.conversation.check_resolved_engines", _ok_health)


class _ColdProvider:
    """A provider whose warm() hasn't run yet — simulates a cold model load."""

    name = "fake"

    def warm(self) -> None:
        pass

    def detail(self) -> str:
        return "fake"


def test_session_started_reports_cold_engines_then_sends_engines_ready(client, monkeypatch):
    fake_stt, fake_tts = _ColdProvider(), _ColdProvider()
    monkeypatch.setitem(stt_service.providers, "whisper", fake_stt)
    monkeypatch.setitem(stt_service.providers, "whisper_local", fake_stt)
    monkeypatch.setitem(tts_service.providers, "vieneu", fake_tts)

    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        started = ws.receive_json()
        assert started["event"] == "session_started"
        assert started["stt_ready"] is False
        assert started["tts_ready"] is False

        notify = ws.receive_json()
        assert notify == {"event": "engines_ready"}


def test_session_started_reports_ready_when_already_warm(client, monkeypatch):
    from app.services.warmup import _ready_ids

    fake_stt, fake_tts = _ColdProvider(), _ColdProvider()
    _ready_ids.add(id(fake_stt))
    _ready_ids.add(id(fake_tts))
    monkeypatch.setitem(stt_service.providers, "whisper", fake_stt)
    monkeypatch.setitem(stt_service.providers, "whisper_local", fake_stt)
    monkeypatch.setitem(tts_service.providers, "vieneu", fake_tts)

    with client.websocket_connect("/v1/conversation/stream?output=text") as ws:
        started = ws.receive_json()
        assert started["stt_ready"] is True
        assert started["tts_ready"] is True
