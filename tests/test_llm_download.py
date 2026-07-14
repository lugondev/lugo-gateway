"""LLM (Ollama) download must fail clearly when no Ollama endpoint is configured,
instead of leaking httpx's cryptic 'missing protocol' error (seen on the Coolify
deploy where CONVERSATION_LLM_BASE_URL is empty)."""

from app.services.llm_models import llm_manager


async def test_llm_download_without_ollama_gives_clear_error(monkeypatch):
    # conversation_llm_base_url now lives on system_config_store (Task 3), not
    # Settings; the module-level conftest._hermetic fixture already zeroes it.
    llm_manager._jobs.pop("gemma2:2b", None)

    await llm_manager.download("gemma2:2b")

    job = llm_manager.snapshot()["jobs"]["gemma2:2b"]
    assert job["state"] == "error"
    assert "Ollama" in job["error"]
    assert "protocol" not in job["error"].lower()
