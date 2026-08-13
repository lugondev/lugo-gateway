import pytest

from app.core.errors import ModelNotAllowedError
from app.services.auth.users import UserStore
from app.services.model_registry.gate import check_model_allowed
from app.services.model_registry.store import ModelRegistryStore


@pytest.fixture
def store():
    return ModelRegistryStore()


@pytest.fixture
def users():
    return UserStore()


@pytest.mark.asyncio
async def test_no_matching_entry_is_now_rejected(users):
    # Catalog-mode (Task 4): a concrete (engine, model_id) with no registry
    # entry is rejected, not silently allowed. The registry is the single
    # source of truth for selectable models.
    user = await users.create("toan", "pw")
    with pytest.raises(ModelNotAllowedError):
        await check_model_allowed("llm", "some-custom-engine", "some-model", user)


@pytest.mark.asyncio
async def test_disabled_entry_is_rejected(store, users):
    user = await users.create("toan", "pw")
    created = await store.create("stt", "whisper", "medium", "Whisper Medium")
    await store.set_fields(created["id"], enabled=False)
    with pytest.raises(ModelNotAllowedError):
        await check_model_allowed("stt", "whisper", "medium", user)


@pytest.mark.asyncio
async def test_testing_stage_requires_can_use_testing(store, users):
    regular = await users.create("toan", "pw")
    tester = await users.create("linh", "pw")
    await users.set_fields(tester["id"], can_use_testing=True)
    await store.create("llm", "openrouter", "qwen3-asr-flash", "Qwen3 ASR Flash", stage="testing")

    regular_user = await users.get_by_id(regular["id"])
    tester_user = await users.get_by_id(tester["id"])

    with pytest.raises(ModelNotAllowedError):
        await check_model_allowed("llm", "openrouter", "qwen3-asr-flash", regular_user)
    await check_model_allowed("llm", "openrouter", "qwen3-asr-flash", tester_user)  # no raise
