import pytest

from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.providers.store import provider_store
from app.services.conversation.responder import resolve_llm_override_from_registry


@pytest.mark.asyncio
async def test_llm_override_uses_provider():
    await init_db()
    prov = await provider_store.create(
        name="qwencloud",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="sk-QWEN",
    )
    await model_registry_store.create(
        "llm", "qwencloud", "qwen-max", "Qwen Max",
        api_key="", base_url="", config={"provider_id": prov["id"]}, is_default=True,
    )
    base_url, api_key = await resolve_llm_override_from_registry("qwencloud", "qwen-max")
    assert base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert api_key == "sk-QWEN"
