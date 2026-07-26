from app.services.db.engine import init_db
from app.services.model_registry.store import model_registry_store
from app.services.usage.attribution import resolve_usage_model


async def test_both_present_is_returned_unchanged():
    assert await resolve_usage_model("llm", "openai", "gpt-4o") == ("openai", "gpt-4o")


async def test_stt_blank_model_uses_the_engines_catalog_default(monkeypatch):
    await init_db()
    monkeypatch.setattr(
        "app.services.usage.attribution.resolve_default_stt_model",
        lambda engine: "large-v3-turbo" if engine == "whisper" else None,
    )
    assert await resolve_usage_model("stt", "whisper", "") == ("whisper", "large-v3-turbo")


async def test_blank_model_resolves_the_engines_single_registry_row():
    await init_db()
    await model_registry_store.create("tts", "vieneu-attr", "vieneu-attr", "VieNeu")
    assert await resolve_usage_model("tts", "vieneu-attr", "") == ("vieneu-attr", "vieneu-attr")


async def test_sentinel_config_rows_are_never_used_as_the_model():
    await init_db()
    # model_id="" is an engine-config sentinel, not a selectable model.
    await model_registry_store.create("stt", "sent-attr", "", "engine config")
    await model_registry_store.create("stt", "sent-attr", "real-model", "Real")
    assert await resolve_usage_model("stt", "sent-attr", "") == ("sent-attr", "real-model")


async def test_ambiguous_engine_stays_blank(monkeypatch):
    await init_db()
    monkeypatch.setattr(
        "app.services.usage.attribution.resolve_default_stt_model", lambda engine: None
    )
    await model_registry_store.create("stt", "amb-attr", "model-a", "A")
    await model_registry_store.create("stt", "amb-attr", "model-b", "B")
    # Two candidates: picking either would invent data.
    assert await resolve_usage_model("stt", "amb-attr", "") == ("amb-attr", "")


async def test_enabled_row_wins_over_disabled_when_resolving_an_engine():
    await init_db()
    await model_registry_store.create("tts", "pref-attr", "old-model", "Old", enabled=False)
    await model_registry_store.create("tts", "pref-attr", "new-model", "New", enabled=True)
    assert await resolve_usage_model("tts", "pref-attr", "") == ("pref-attr", "new-model")


async def test_blank_engine_and_model_for_llm_uses_the_active_default():
    await init_db()
    await model_registry_store.create(
        "llm", "def-attr", "default-model", "Default", is_default=True
    )
    assert await resolve_usage_model("llm", "", "") == ("def-attr", "default-model")


async def test_blank_engine_is_recovered_from_the_model_id():
    await init_db()
    await model_registry_store.create("llm", "rev-attr", "rev-model", "Rev")
    assert await resolve_usage_model("llm", "", "rev-model") == ("rev-attr", "rev-model")


async def test_unknown_engine_returns_the_inputs_untouched(monkeypatch):
    await init_db()
    monkeypatch.setattr(
        "app.services.usage.attribution.resolve_default_stt_model", lambda engine: None
    )
    assert await resolve_usage_model("stt", "nothing-registered", "") == ("nothing-registered", "")


async def test_never_raises_when_the_store_is_broken(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(model_registry_store, "list_all", boom)
    monkeypatch.setattr(model_registry_store, "find_default", boom)
    assert await resolve_usage_model("tts", "some-engine", "") == ("some-engine", "")


async def test_ambiguous_engine_warns_once_with_the_candidate_models(caplog):
    """The dashboard shows these rows as unattributed; the log has to say WHY
    and how to fix it, or an admin sees "(not recorded)" with no lead. Once per
    (kind, engine), not once per request -- attribution runs on every turn."""
    import logging

    from app.services.usage import attribution

    await init_db()
    attribution._warned_ambiguous.clear()
    await model_registry_store.create("stt", "qwencloud-warn", "fun-asr", "Fun ASR")
    await model_registry_store.create("stt", "qwencloud-warn", "qwen3-asr-flash", "Flash")

    with caplog.at_level(logging.WARNING, logger="app.services.usage.attribution"):
        assert await resolve_usage_model("stt", "qwencloud-warn", "") == ("qwencloud-warn", "")
        assert await resolve_usage_model("stt", "qwencloud-warn", "") == ("qwencloud-warn", "")

    warnings = [r for r in caplog.records if "candidate models" in r.getMessage()]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"
    message = warnings[0].getMessage()
    assert "fun-asr" in message and "qwen3-asr-flash" in message
    assert "pins a model" in message  # tells the admin the way out


async def test_unambiguous_engine_never_warns(caplog):
    import logging

    from app.services.usage import attribution

    await init_db()
    attribution._warned_ambiguous.clear()
    await model_registry_store.create("stt", "solo-warn", "only-model", "Only")

    with caplog.at_level(logging.WARNING, logger="app.services.usage.attribution"):
        assert await resolve_usage_model("stt", "solo-warn", "") == ("solo-warn", "only-model")
    assert [r for r in caplog.records if "candidate models" in r.getMessage()] == []
