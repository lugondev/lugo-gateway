"""Task 4: REST routes must pre-flight-gate on quota and return HTTP 429 when
an applicable enabled quota is at/over its limit, BEFORE doing any provider
work.

Approach: use a GLOBAL-scope quota (scope="global", scope_id="") so the test
doesn't need to wire the exact provider_id resolution -- a global quota
applies regardless of user/provider (see `_applies` in
app.services.quota.gate). Seed a usage_events row with cost over the limit,
then drive /transcribe and /synthesize via the sync TestClient with stub
providers (same pattern as tests/unit/test_routes_usage_metering.py) and
assert 429. Control: no quota at all -> normal 200.
"""

import uuid

from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.main import app
from app.schemas.stt import STTResult
from app.schemas.tts import TTSResult
from app.services.db.engine import db_session
from app.services.db.models import UsageEvent
from app.services.quota.store import quota_store
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service

SR = 16000


class _StubSTT(STTProvider):
    # STTRequest.engine is regex-restricted to known engine ids, so this stub
    # is registered under the real "vosk" key (swapped back after each test).
    name = "vosk"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None) -> STTResult:
        return STTResult(engine=self.name, text="xin chao", is_final=True)


class _StubTTS(TTSProvider):
    name = "stub-quota-route-tts"

    async def synthesize(self, payload) -> TTSResult:
        return TTSResult(
            engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav",
            duration_seconds=0.1, text=payload.text,
        )

    async def render_audio(self, payload) -> tuple[bytes, str]:
        # /v1/tts/synthesize now calls this bytes-returning seam directly
        # (see app.services.tts.base.TTSProvider.render_audio). Must be a
        # real WAV -- the route computes duration via wav_duration_seconds.
        return pcm16_to_wav_bytes(b"\x00\x00" * 10, sample_rate=24000), "audio/wav"


def _wav_bytes(ms: int = 300) -> bytes:
    n = int(SR * ms / 1000)
    pcm = b"\x00\x00" * n
    return pcm16_to_wav_bytes(pcm, sample_rate=SR)


async def _seed_over_limit_global_quota(limit_usd: float = 0.01, cost: float = 5.0) -> None:
    await quota_store.create(scope="global", scope_id="", limit_usd=limit_usd, period="total")
    async with db_session() as s:
        s.add(UsageEvent(
            id=str(uuid.uuid4()), user_id="someone-else", profile_id="", provider_id="provX",
            kind="llm", engine="e", model_id="m", unit="tokens", native_amount=1, cost_usd=cost,
        ))
        await s.commit()


async def test_transcribe_blocked_by_global_quota():
    original = stt_service.providers.get(_StubSTT.name)
    stt_service.providers[_StubSTT.name] = _StubSTT()
    try:
        await _seed_over_limit_global_quota()
        client = TestClient(app)
        resp = client.post(
            "/v1/stt/transcribe",
            files={"audio": ("clip.wav", _wav_bytes(), "audio/wav")},
            data={"engine": _StubSTT.name, "denoise": "false", "vad": "false"},
        )
        assert resp.status_code == 429
    finally:
        if original is not None:
            stt_service.providers[_StubSTT.name] = original
        else:
            stt_service.providers.pop(_StubSTT.name, None)


async def test_transcribe_allowed_when_no_quota():
    original = stt_service.providers.get(_StubSTT.name)
    stt_service.providers[_StubSTT.name] = _StubSTT()
    try:
        client = TestClient(app)
        resp = client.post(
            "/v1/stt/transcribe",
            files={"audio": ("clip.wav", _wav_bytes(), "audio/wav")},
            data={"engine": _StubSTT.name, "denoise": "false", "vad": "false"},
        )
        assert resp.status_code == 200
    finally:
        if original is not None:
            stt_service.providers[_StubSTT.name] = original
        else:
            stt_service.providers.pop(_StubSTT.name, None)


async def test_synthesize_blocked_by_global_quota():
    tts_service.providers[_StubTTS.name] = _StubTTS()
    try:
        await _seed_over_limit_global_quota()
        client = TestClient(app)
        resp = client.post("/v1/tts/synthesize", json={"text": "xin chao", "engine": _StubTTS.name})
        assert resp.status_code == 429
    finally:
        tts_service.providers.pop(_StubTTS.name, None)


async def test_synthesize_allowed_when_no_quota():
    tts_service.providers[_StubTTS.name] = _StubTTS()
    try:
        client = TestClient(app)
        resp = client.post("/v1/tts/synthesize", json={"text": "xin chao", "engine": _StubTTS.name})
        assert resp.status_code == 200
    finally:
        tts_service.providers.pop(_StubTTS.name, None)


async def test_chat_blocked_by_global_quota():
    await _seed_over_limit_global_quota()
    client = TestClient(app)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 429


async def test_chat_allowed_when_no_quota():
    client = TestClient(app)
    resp = client.post("/v1/conversation/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
