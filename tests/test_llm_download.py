"""LLM (Ollama) download must fail clearly when no Ollama endpoint is configured,
instead of leaking httpx's cryptic 'missing protocol' error (seen on the Coolify
deploy when no kind="llm" Model Registry entry was enabled)."""

from app.services.llm_models import llm_manager


async def test_llm_download_without_ollama_gives_clear_error(monkeypatch):
    # The conversation LLM's base_url now lives in a Model Registry kind="llm"
    # entry; each test's fresh tmp DB starts with none enabled, so this is
    # already unconfigured without any extra patching.
    llm_manager._jobs.pop("gemma2:2b", None)

    await llm_manager.download("gemma2:2b")

    job = (await llm_manager.snapshot())["jobs"]["gemma2:2b"]
    assert job["state"] == "error"
    assert "Ollama" in job["error"]
    assert "protocol" not in job["error"].lower()
