from sqlalchemy import select
from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent


async def test_usage_event_roundtrips():
    await init_db()
    async with db_session() as s:
        s.add(UsageEvent(
            id="u1", user_id="user-a", profile_id="p1", provider_id="prov-1",
            kind="llm", engine="openrouter", model_id="qwen-max", unit="tokens",
            native_amount=1500, prompt_tokens=1000, completion_tokens=500,
            cost_usd=0.0021, status="ok",
        ))
        await s.commit()
    async with db_session() as s:
        row = (await s.execute(select(UsageEvent))).scalars().one()
    assert row.kind == "llm" and row.prompt_tokens == 1000
    assert row.cost_usd == 0.0021
    assert row.status == "ok"
    assert row.request_id is None
