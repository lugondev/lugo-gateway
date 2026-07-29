from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.usage.recorder import record_usage


async def test_records_with_cost_and_provider_from_registry():
    await init_db()
    await model_registry_store.create(
        "llm", "openrouter", "qwen-max", "Qwen Max",
        config={"provider_id": "prov-9", "price": {"unit": "1M_tokens", "in": 0.15, "out": 0.60}},
        is_default=True,
    )
    await record_usage(user_id="u1", profile_id="p1", kind="llm", engine="openrouter",
                       model_id="qwen-max", unit="tokens", native_amount=1500,
                       prompt_tokens=1000, completion_tokens=500)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert row.provider_id == "prov-9"
    assert row.prompt_tokens == 1000 and row.completion_tokens == 500
    assert abs(row.cost_usd - (1000/1e6*0.15 + 500/1e6*0.60)) < 1e-12
    assert row.user_id == "u1"


async def test_no_registry_entry_records_zero_cost_no_provider():
    await init_db()
    await record_usage(user_id="", profile_id="", kind="tts", engine="vieneu",
                       model_id="v1", unit="chars", native_amount=200)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert row.provider_id == "" and row.cost_usd == 0.0 and row.native_amount == 200


async def test_never_raises_on_bad_input(monkeypatch):
    # Force the store lookup to blow up; record_usage must swallow it.
    async def boom(*a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(model_registry_store, "find", boom)
    await init_db()
    # Must NOT raise:
    await record_usage(user_id="u", profile_id="p", kind="llm", engine="x",
                       model_id="y", unit="tokens", native_amount=1)


async def test_blank_model_is_resolved_from_the_registry_and_gets_priced():
    await init_db()
    await model_registry_store.create(
        "tts", "vieneu-rec", "vieneu-rec", "VieNeu",
        config={"provider_id": "prov-v", "price": {"unit": "1k_chars", "rate": 2.0}},
    )
    # Caller passes no model_id at all -- what /synthesize and the conversation
    # core do when a TTS profile pins no model.
    await record_usage(user_id="u1", profile_id="p1", kind="tts", engine="vieneu-rec",
                       model_id="", unit="chars", native_amount=1000)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert row.model_id == "vieneu-rec"      # no longer "" -> no more "(none)"
    assert row.provider_id == "prov-v"
    assert abs(row.cost_usd - 2.0) < 1e-12   # and now it can actually be costed


async def test_blank_engine_and_model_llm_resolves_to_the_active_default():
    await init_db()
    await model_registry_store.create(
        "llm", "openrouter-rec", "or/free-rec", "OR free", is_default=True,
    )
    await record_usage(user_id="u1", profile_id="", kind="llm", engine="", model_id="",
                       unit="tokens", native_amount=10, prompt_tokens=8, completion_tokens=2)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert (row.engine, row.model_id) == ("openrouter-rec", "or/free-rec")


async def test_unresolvable_blank_still_records_a_row():
    await init_db()
    await record_usage(user_id="u1", profile_id="", kind="tts", engine="ghost-engine",
                       model_id="", unit="chars", native_amount=5)
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    # Nothing to resolve against -> blank is preserved, but the row must exist:
    # losing usage data is worse than an unattributed row.
    assert row.engine == "ghost-engine" and row.model_id == ""
    assert row.native_amount == 5
