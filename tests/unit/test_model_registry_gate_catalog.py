import pytest

from app.core.errors import ModelNotAllowedError
from app.services.model_registry.gate import check_model_allowed
from app.services.model_registry.store import model_registry_store


async def test_no_entry_now_rejected():
    with pytest.raises(ModelNotAllowedError):
        await check_model_allowed("stt", "whisper", "tiny", None)


async def test_enabled_entry_allowed():
    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=True)
    await check_model_allowed("stt", "whisper", "tiny", None)  # no raise


async def test_empty_selection_is_unrestricted():
    await check_model_allowed("stt", "", "", None)  # inherit-global, no raise
