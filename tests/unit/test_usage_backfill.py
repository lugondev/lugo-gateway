from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.model_registry.store import model_registry_store
from app.services.usage.backfill import migrate_backfill_usage_model_ids
from app.services.usage.recorder import record_usage


async def _add_blank_row(kind, engine, native_amount=1.0):
    """A legacy row: written before attribution existed, so model_id is "".
    Inserted directly because record_usage now resolves blanks away."""
    import uuid

    async with db_session() as s:
        s.add(UsageEvent(
            id=str(uuid.uuid4()), user_id="u1", profile_id="p1", provider_id="",
            kind=kind, engine=engine, model_id="", unit="chars",
            native_amount=native_amount, cost_usd=0.0, status="ok",
        ))
        await s.commit()


async def _rows(engine):
    async with db_session() as s:
        rows = (await s.execute(select(UsageEvent))).scalars().all()
    return [r for r in rows if r.engine == engine]


async def test_backfills_when_the_engine_has_exactly_one_model():
    await init_db()
    await model_registry_store.create("tts", "vieneu-bf", "vieneu-bf", "VieNeu")
    await _add_blank_row("tts", "vieneu-bf")
    await _add_blank_row("tts", "vieneu-bf")

    assert await migrate_backfill_usage_model_ids() == 2
    assert {r.model_id for r in await _rows("vieneu-bf")} == {"vieneu-bf"}


async def test_skips_an_ambiguous_engine():
    await init_db()
    await model_registry_store.create("stt", "amb-bf", "fun-asr", "Fun")
    await model_registry_store.create("stt", "amb-bf", "qwen3-asr-flash", "Flash")
    await _add_blank_row("stt", "amb-bf")

    assert await migrate_backfill_usage_model_ids() == 0
    assert [r.model_id for r in await _rows("amb-bf")] == [""]


async def test_ignores_sentinel_rows_as_candidates():
    await init_db()
    await model_registry_store.create("stt", "sent-bf", "", "engine config")
    await model_registry_store.create("stt", "sent-bf", "real-bf", "Real")
    await _add_blank_row("stt", "sent-bf")

    assert await migrate_backfill_usage_model_ids() == 1
    assert [r.model_id for r in await _rows("sent-bf")] == ["real-bf"]


async def test_rows_with_a_blank_engine_are_left_alone():
    await init_db()
    await _add_blank_row("llm", "")
    assert await migrate_backfill_usage_model_ids() == 0
    assert [r.model_id for r in await _rows("")] == [""]


async def test_is_idempotent_and_leaves_cost_untouched():
    await init_db()
    await model_registry_store.create(
        "tts", "idem-bf", "idem-bf", "Idem",
        config={"price": {"unit": "1k_chars", "rate": 5.0}},
    )
    await _add_blank_row("tts", "idem-bf", native_amount=1000)

    assert await migrate_backfill_usage_model_ids() == 1
    assert await migrate_backfill_usage_model_ids() == 0  # nothing left to do
    row = (await _rows("idem-bf"))[0]
    assert row.model_id == "idem-bf"
    # A price entered today must not rewrite what a past request cost.
    assert row.cost_usd == 0.0


async def test_does_not_touch_rows_that_already_name_a_model():
    await init_db()
    await model_registry_store.create("tts", "keep-bf", "keep-model", "Keep")
    await record_usage(user_id="u1", profile_id="", kind="tts", engine="keep-bf",
                       model_id="keep-model", unit="chars", native_amount=10)
    assert await migrate_backfill_usage_model_ids() == 0
    assert [r.model_id for r in await _rows("keep-bf")] == ["keep-model"]
