from app.services.db.engine import init_db
from app.services.quota.store import quota_store


async def test_create_list_enabled_and_delete():
    await init_db()
    q = await quota_store.create(scope="user", scope_id="u1", limit_usd=10.0, period="monthly")
    assert q["scope"] == "user" and q["limit_usd"] == 10.0 and q["enabled"] is True
    enabled = await quota_store.list_enabled()
    assert any(e["id"] == q["id"] for e in enabled)
    await quota_store.set_fields(q["id"], enabled=False)
    assert all(e["id"] != q["id"] for e in await quota_store.list_enabled())
    assert await quota_store.delete(q["id"]) is True
    assert await quota_store.get(q["id"]) is None
