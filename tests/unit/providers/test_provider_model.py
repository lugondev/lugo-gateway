from sqlalchemy import select

from app.services.db.engine import db_session, init_db
from app.services.db.models import Provider


async def test_provider_row_roundtrips():
    await init_db()
    async with db_session() as s:
        s.add(Provider(id="p1", name="openai", label="OpenAI",
                       base_url="https://api.openai.com/v1", api_key="sk-x", enabled=True))
        await s.commit()
    async with db_session() as s:
        row = (await s.execute(select(Provider))).scalars().one()
    assert row.name == "openai"
    assert row.base_url == "https://api.openai.com/v1"
    assert row.api_key == "sk-x"
    assert row.enabled is True
    assert row.config == {}
