# tests/unit/providers/test_provider_resolve.py
import pytest

from app.services.db.engine import init_db
from app.services.providers.resolve import (
    resolve_credentials, resolve_credentials_sync, PROVIDER_PRESETS,
)
from app.services.providers.store import provider_store


@pytest.mark.asyncio
async def test_uses_provider_when_linked():
    await init_db()
    p = await provider_store.create(name="openai", base_url="https://api.openai.com/v1", api_key="sk-P")
    entry = {"base_url": "", "api_key": "", "config": {"provider_id": p["id"]}}
    assert await resolve_credentials(entry) == ("https://api.openai.com/v1", "sk-P")
    assert resolve_credentials_sync(entry) == ("https://api.openai.com/v1", "sk-P")


@pytest.mark.asyncio
async def test_falls_back_to_entry_when_no_provider():
    await init_db()
    entry = {"base_url": "http://localhost:11434/v1", "api_key": "", "config": {}}
    assert await resolve_credentials(entry) == ("http://localhost:11434/v1", "")
    assert resolve_credentials_sync(entry) == ("http://localhost:11434/v1", "")


@pytest.mark.asyncio
async def test_falls_back_when_provider_id_dangling():
    await init_db()
    entry = {"base_url": "http://x/v1", "api_key": "k", "config": {"provider_id": "missing"}}
    assert await resolve_credentials(entry) == ("http://x/v1", "k")


def test_presets_cover_three_providers():
    names = {p["name"] for p in PROVIDER_PRESETS}
    assert {"openai", "openrouter", "qwencloud"} <= names
