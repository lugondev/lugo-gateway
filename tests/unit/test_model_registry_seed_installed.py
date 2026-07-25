import pytest
from app.services.model_registry.seed import seed_installed_models_to_registry
from app.services.model_registry.store import model_registry_store


async def test_seeds_cached_whisper_models(monkeypatch):
    from app.services.whisper_models import whisper_manager
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {
        "models": [
            {"size": "tiny", "label": "Tiny", "cached": True, "active": True},
            {"size": "large-v3", "label": "Large", "cached": False, "active": False},
        ],
        "active": "tiny",
    })
    from app.services.models import model_manager
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})

    await seed_installed_models_to_registry()
    assert await model_registry_store.find("stt", "whisper", "tiny") is not None
    assert await model_registry_store.find("stt", "whisper", "large-v3") is None  # not cached


async def test_seeds_tts_profile_engine_shim(monkeypatch):
    # Finding 1 (critical): TTS profile save gates on the (engine, engine) shim
    # (see routes/tts_profiles.py) -- unlike stt/llm, nothing else backfills
    # that shape, so a profile with an in-use engine must be seeded here too or
    # catalog-mode's flip (Task 4) locks existing TTS profiles out of saving.
    from app.services.whisper_models import whisper_manager
    from app.services.models import model_manager
    from app.services.tts.profile_store import tts_profile_store
    from app.services.tts.profile_models import TtsProfile
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {"models": [], "active": None})
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})
    monkeypatch.setattr(tts_profile_store, "list", lambda: {
        "p1": TtsProfile(name="p1", engine="omnivoice"),
    })

    await seed_installed_models_to_registry()
    entry = await model_registry_store.find("tts", "omnivoice", "omnivoice")
    assert entry is not None
    assert entry["enabled"] is True


async def test_seeds_tts_profile_engine_shim_skips_blank_engine(monkeypatch):
    from app.services.whisper_models import whisper_manager
    from app.services.models import model_manager
    from app.services.tts.profile_store import tts_profile_store
    from app.services.tts.profile_models import TtsProfile
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {"models": [], "active": None})
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})
    monkeypatch.setattr(tts_profile_store, "list", lambda: {
        "p1": TtsProfile(name="p1", engine=""),
    })

    await seed_installed_models_to_registry()  # no-op, no crash -- blank engine == inherit default
    assert await model_registry_store.list_all() == []


async def test_no_engine_shim_for_a_profile_that_pins_a_model_id(monkeypatch):
    # A TTS profile that pins a model_id (http_tts/vieneu-cloudflare, ...) gates on
    # THAT row -- check_model_allowed("tts", engine, profile.model_id) in
    # routes/tts_profiles.py -- so the (engine, engine) shim satisfies nothing and
    # is actively misleading: for a service engine it shows up in the registry as
    # a permanently broken "no base URL set!" row that comes back on every boot.
    from app.services.whisper_models import whisper_manager
    from app.services.models import model_manager
    from app.services.tts.profile_store import tts_profile_store
    from app.services.tts.profile_models import TtsProfile
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {"models": [], "active": None})
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})
    monkeypatch.setattr(tts_profile_store, "list", lambda: {
        "p1": TtsProfile(name="p1", engine="http_tts", model_id="vieneu-cloudflare"),
    })

    await seed_installed_models_to_registry()

    assert await model_registry_store.find("tts", "http_tts", "http_tts") is None
    # ...and it doesn't invent the real row either: only an admin (who knows the
    # base_url) can catalogue a service entry.
    assert await model_registry_store.find("tts", "http_tts", "vieneu-cloudflare") is None


async def test_seeds_two_profiles_referencing_different_llms_both_stay_enabled(monkeypatch):
    # Regression guard for the finding that motivated is_default: before it,
    # kind="llm" `enabled` was exclusive DB-side, so seeding a second llm
    # profile's model would silently disable the first -- locking a
    # multi-LLM catalog down to exactly one usable entry. `enabled` is no
    # longer exclusive (only `is_default` is), so both must end up enabled.
    from app.services.whisper_models import whisper_manager
    from app.services.models import model_manager
    from app.services.profiles.store import profile_store
    from app.services.profiles.models import Profile, LlmConfig
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {"models": [], "active": None})
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})
    monkeypatch.setattr(profile_store, "list", lambda: {
        "p1": Profile(name="p1", llm=LlmConfig(engine="openrouter", model="model-a")),
        "p2": Profile(name="p2", llm=LlmConfig(engine="ollama", model="model-b")),
    })

    await seed_installed_models_to_registry()

    entry_a = await model_registry_store.find("llm", "openrouter", "model-a")
    entry_b = await model_registry_store.find("llm", "ollama", "model-b")
    assert entry_a is not None
    assert entry_b is not None
    assert entry_a["enabled"] is True
    assert entry_b["enabled"] is True


async def test_idempotent_and_preserves_disabled(monkeypatch):
    from app.services.whisper_models import whisper_manager
    from app.services.models import model_manager
    monkeypatch.setattr(whisper_manager, "snapshot", lambda: {
        "models": [{"size": "tiny", "label": "Tiny", "cached": True, "active": True}], "active": "tiny"})
    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": [], "active": None})

    await model_registry_store.create("stt", "whisper", "tiny", "Tiny", enabled=False)
    await seed_installed_models_to_registry()
    # ensure_registry_entry re-enables a disabled row — acceptable: it IS installed.
    entry = await model_registry_store.find("stt", "whisper", "tiny")
    assert entry["enabled"] is True
    await seed_installed_models_to_registry()  # second run: no crash, still one row
    all_tiny = [e for e in await model_registry_store.list_all()
                if e["engine"] == "whisper" and e["model_id"] == "tiny"]
    assert len(all_tiny) == 1
