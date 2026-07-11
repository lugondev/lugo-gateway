import pytest

from app.core.errors import UsernameTakenError
from app.services.auth.users import UserStore


@pytest.fixture
def store():
    return UserStore()


@pytest.mark.asyncio
async def test_create_and_get_by_username(store):
    await store.create("toan", "s3cret", role="admin")
    user = await store.get_by_username("toan")
    assert user is not None
    assert user.role == "admin"
    assert user.disabled is False
    assert user.can_use_testing is False


@pytest.mark.asyncio
async def test_create_duplicate_username_raises(store):
    await store.create("toan", "s3cret")
    with pytest.raises(UsernameTakenError):
        await store.create("toan", "different")


@pytest.mark.asyncio
async def test_verify_login_correct_and_wrong_password(store):
    await store.create("toan", "s3cret")
    ok = await store.verify_login("toan", "s3cret")
    assert ok is not None and ok.username == "toan"
    assert await store.verify_login("toan", "wrong") is None
    assert await store.verify_login("nobody", "s3cret") is None


@pytest.mark.asyncio
async def test_list_and_count(store):
    assert await store.count() == 0
    await store.create("a", "pw1")
    await store.create("b", "pw2")
    assert await store.count() == 2
    usernames = sorted(u["username"] for u in await store.list())
    assert usernames == ["a", "b"]


@pytest.mark.asyncio
async def test_set_fields_updates_disabled_role_testing(store):
    created = await store.create("toan", "s3cret")
    updated = await store.set_fields(created["id"], disabled=True, can_use_testing=True)
    assert updated["disabled"] is True
    assert updated["can_use_testing"] is True
    assert await store.set_fields("missing-id", disabled=True) is None


@pytest.mark.asyncio
async def test_reset_password_changes_login(store):
    created = await store.create("toan", "old-pw")
    assert await store.reset_password(created["id"], "new-pw") is True
    assert await store.verify_login("toan", "old-pw") is None
    assert await store.verify_login("toan", "new-pw") is not None
    assert await store.reset_password("missing-id", "x") is False
