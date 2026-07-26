"""POST /v1/tts/stream spawned a background job that synthesized speech with no
usage row, no quota check, and no identity at all -- the endpoint did not even
take a Request."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.schemas.tts import TTSResult
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service
from app.services.usage.recorder import record_usage


class _StubTTS(TTSProvider):
    name = "stub-stream-tts"

    def available(self) -> bool:
        return True

    async def synthesize(self, request) -> TTSResult:
        return TTSResult(audio_url="/artifacts/x.wav", duration_seconds=0.1,
                         sample_rate=16000, engine=self.name)


@pytest.fixture
def _stub_engine():
    tts_service.providers["stub-stream-tts"] = _StubTTS()
    yield
    tts_service.providers.pop("stub-stream-tts", None)


async def _rows(kind="tts"):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if r.kind == kind]


async def test_the_stream_job_meters_every_chunk_it_synthesizes(_stub_engine):
    await init_db()
    quota_store.invalidate()
    # Must be a context-managed client: TestClient(app) used bare opens a fresh
    # anyio portal (and event loop) per request and tears it down as soon as
    # the response is returned, which cancels the background asyncio.create_task
    # before it can run -- not flaky, deterministic 0 rows every time. `with`
    # keeps the same portal (and loop) alive across the polling loop below so
    # the spawned job actually gets to run to completion.
    with TestClient(app) as client:
        # Two sentences -> two segments -> two synthesize calls.
        resp = client.post("/v1/tts/stream", json={
            "text": "Xin chao ban. Hom nay the nao?", "engine": "stub-stream-tts",
        })
        assert resp.status_code == 200, resp.text

        # The job runs in the background; give it a moment to finish.
        for _ in range(50):
            rows = await _rows()
            if len(rows) >= 2:
                break
            await asyncio.sleep(0.02)

    rows = await _rows()
    assert len(rows) >= 2, f"expected one row per synthesized chunk, got {len(rows)}"
    assert all(r.engine == "stub-stream-tts" for r in rows)
    assert all(r.unit == "chars" for r in rows)
    total_chars = sum(r.native_amount for r in rows)
    assert total_chars > 0
    # Every chunk's characters are counted, and only the text sent to the provider.
    assert total_chars <= len("Xin chao ban. Hom nay the nao?")


async def test_an_over_quota_stream_job_is_refused_with_429_and_never_starts(_stub_engine):
    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        "tts", "stub-stream-tts", "stub-model", "Stub",
        config={"provider_id": "prov-t", "price": {"unit": "1k_chars", "rate": 100.0}},
    )
    await record_usage(user_id="", profile_id="", kind="tts", engine="stub-stream-tts",
                       model_id="stub-model", unit="chars", native_amount=1000)  # $100
    await quota_store.create(scope="global", scope_id="", limit_usd=1.0, period="monthly")

    with TestClient(app) as client:
        resp = client.post("/v1/tts/stream", json={"text": "Xin chao", "engine": "stub-stream-tts"})
    assert resp.status_code == 429, resp.text
    assert "quota exceeded" in resp.json()["detail"]

    await asyncio.sleep(0.1)  # a spawned job would have had time to record by now
    served = [r for r in await _rows() if r.status == "ok" and r.native_amount < 1000]
    assert served == [], "a refused job must not synthesize anything"
    blocked = [r for r in await _rows() if r.status == "blocked"]
    assert len(blocked) == 1, "the refusal must be audited"
