"""A provider-scoped quota must fire on every entry point, not just /chat.

Every gate used to look the provider up with a blank model_id, which matches no
registry row, so `provider_id` stayed "" and `_applies()` skipped every
provider-scoped quota. Measured before this change: /transcribe, /synthesize and
the conversation turn all resolved to None.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.quota.store import quota_store
from app.services.usage.recorder import record_usage


async def _provider_over_quota(kind: str, engine: str, model_id: str, provider_id: str) -> None:
    """Register a priced model on `provider_id` and push that provider over a $1 quota."""
    await init_db()
    quota_store.invalidate()
    await model_registry_store.create(
        kind, engine, model_id, f"{engine}/{model_id}",
        config={"provider_id": provider_id, "price": {"unit": "1M_tokens", "in": 10.0, "out": 0.0}},
    )
    await record_usage(user_id="someone-else", profile_id="", kind=kind, engine=engine,
                       model_id=model_id, unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="provider", scope_id=provider_id, limit_usd=1.0, period="monthly")


async def test_transcribe_enforces_a_provider_quota():
    """The STT route knows only its engine; the model comes from the registry."""
    await _provider_over_quota("stt", "qwencloud", "fun-asr", "prov-qwen")
    client = TestClient(app)
    resp = client.post(
        "/v1/stt/transcribe",
        files={"audio": ("a.wav", b"RIFF0000WAVEfmt ", "audio/wav")},
        data={"engine": "qwencloud"},
    )
    assert resp.status_code == 429, resp.text
    assert "provider quota exceeded" in resp.json()["detail"]


async def test_synthesize_enforces_a_provider_quota():
    await _provider_over_quota("tts", "vieneu", "vieneu", "prov-vn")
    client = TestClient(app)
    resp = client.post("/v1/tts/synthesize", json={"text": "xin chao", "engine": "vieneu"})
    assert resp.status_code == 429, resp.text
    assert "provider quota exceeded" in resp.json()["detail"]


async def test_a_blocked_rest_request_leaves_an_audit_row():
    """Task 1's audit row, reached through a real route."""
    from sqlalchemy import select

    from app.services.db.engine import db_session
    from app.services.db.models import UsageEvent

    await _provider_over_quota("tts", "vieneu", "vieneu", "prov-audit")
    client = TestClient(app)
    assert client.post("/v1/tts/synthesize", json={"text": "hi", "engine": "vieneu"}).status_code == 429
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    blocked = [r for r in rows if r.status == "blocked"]
    assert len(blocked) == 1
    assert blocked[0].kind == "tts" and blocked[0].engine == "vieneu"


async def _pinned_engine_vs_default_registry(spend_on: str) -> None:
    """A profile pinning engine `eng-a` (provider A) but NO model, plus a
    registry default `eng-b`/`m-b` (provider B) -- which is what
    build_responder_ex() actually runs when no model is pinned. `spend_on` is
    the provider pushed over a $1 quota.
    """
    from app.services.profiles.models import LlmConfig, Profile
    from app.services.profiles.store import profile_store

    await init_db()
    quota_store.invalidate()
    price = {"unit": "1M_tokens", "in": 10.0, "out": 0.0}
    await model_registry_store.create(
        "llm", "eng-a", "m-a", "A", config={"provider_id": "prov-a", "price": price},
    )
    await model_registry_store.create(
        "llm", "eng-b", "m-b", "B", is_default=True,
        config={"provider_id": "prov-b", "price": price},
    )
    profile_store.upsert(Profile(name="pin-eng-only", llm=LlmConfig(engine="eng-a")))
    engine, model_id = ("eng-a", "m-a") if spend_on == "prov-a" else ("eng-b", "m-b")
    await record_usage(user_id="someone-else", profile_id="", kind="llm", engine=engine,
                       model_id=model_id, unit="tokens", native_amount=1_000_000,
                       prompt_tokens=1_000_000)
    await quota_store.create(scope="provider", scope_id=spend_on, limit_usd=1.0, period="monthly")


def _chat(client):
    return client.post(
        "/v1/conversation/chat?profile=pin-eng-only",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )


async def test_pinned_engine_with_no_model_does_not_check_the_pinned_engines_provider():
    """The gate must not pair the profile's engine with a model the profile
    never pinned: build_responder_ex() runs the registry DEFAULT (prov-b), so a
    prov-a quota refuses work prov-a will never be billed for."""
    await _pinned_engine_vs_default_registry(spend_on="prov-a")
    resp = _chat(TestClient(app))
    assert resp.status_code != 429, (
        "prov-a is over limit but prov-b gets billed -- blocking here is the "
        f"wrong-provider bug: {resp.text}"
    )


async def test_pinned_engine_with_no_model_checks_the_provider_that_gets_billed():
    """The other half: the default entry's provider is the one that runs, so
    its quota MUST fire even though the profile pins a different engine."""
    await _pinned_engine_vs_default_registry(spend_on="prov-b")
    resp = _chat(TestClient(app))
    assert resp.status_code == 429, resp.text
    assert "provider quota exceeded" in resp.json()["detail"]


async def test_an_unrelated_provider_quota_does_not_block():
    """The guard against over-correcting: resolving a provider must not make
    every provider's quota apply to every request."""
    await _provider_over_quota("tts", "vieneu", "vieneu", "prov-other")
    await model_registry_store.create(
        "tts", "edge_tts", "edge_tts", "Edge", config={"provider_id": "prov-innocent"},
    )
    client = TestClient(app)
    resp = client.post("/v1/tts/synthesize", json={"text": "hi", "engine": "edge_tts"})
    assert resp.status_code != 429, "edge_tts is on a different provider and must not be blocked"
