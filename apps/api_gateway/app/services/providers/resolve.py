from __future__ import annotations

from app.services.providers.store import provider_store

# Prefilled base_url defaults for the 3 supported OpenAI-compatible providers.
# EDITABLE in the UI. NOTE: verify the QwenCloud/DashScope compatible-mode URL
# against current Alibaba Cloud docs before shipping (int'l vs CN endpoints
# differ, e.g. dashscope-intl.* vs dashscope.*). Do not treat as authoritative.
PROVIDER_PRESETS: list[dict] = [
    {"name": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"name": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    {"name": "qwencloud", "label": "Qwen Cloud (DashScope)",
     "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
]


def _provider_id(entry: dict) -> str:
    return (entry.get("config") or {}).get("provider_id") or ""


def _from_provider(provider: dict | None, entry: dict) -> tuple[str, str]:
    if provider:
        return provider["base_url"], provider["api_key"]
    return entry.get("base_url", ""), entry.get("api_key", "")


def resolve_credentials_sync(entry: dict) -> tuple[str, str]:
    """(base_url, api_key) for a registry entry, cache-only. If config.provider_id
    resolves to a provider, use it; else fall back to the entry's own fields."""
    pid = _provider_id(entry)
    provider = provider_store.get_sync(pid) if pid else None
    return _from_provider(provider, entry)


async def resolve_credentials(entry: dict) -> tuple[str, str]:
    pid = _provider_id(entry)
    provider = await provider_store.get(pid) if pid else None
    return _from_provider(provider, entry)
