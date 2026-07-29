"""Cleanup for the (engine, engine) TTS shim rows seed_installed_models_to_registry
used to create for EVERY TTS profile engine, model_id-pinning ones included."""

from app.services.model_registry.seed import migrate_drop_stale_tts_engine_shims
from app.services.model_registry.store import model_registry_store


async def test_drops_shim_no_profile_needs_anymore(monkeypatch):
    # http_tts profiles all pin a model_id, so nothing gates on the shim -- and it
    # can never work (no base_url), which is exactly what the admin sees in the
    # registry as "http_tts/http_tts — service — no base URL set!".
    from app.services.tts.profile_store import tts_profile_store
    from app.services.tts.profile_models import TtsProfile
    monkeypatch.setattr(tts_profile_store, "list", lambda: {
        "vn-cf": TtsProfile(name="vn-cf", engine="http_tts", model_id="vieneu-cloudflare"),
    })
    await model_registry_store.create(
        "tts", "http_tts", "http_tts", "http_tts — http_tts (in use)")

    await migrate_drop_stale_tts_engine_shims()

    assert await model_registry_store.find("tts", "http_tts", "http_tts") is None


async def test_keeps_shim_a_model_id_less_profile_still_needs(monkeypatch):
    # A profile that pins no model_id has the shim as its ONLY selectable registry
    # row -- list_options() excludes model_id="" sentinels, so dropping this would
    # remove the engine from the TTS-profile picker.
    from app.services.tts.profile_store import tts_profile_store
    from app.services.tts.profile_models import TtsProfile
    monkeypatch.setattr(tts_profile_store, "list", lambda: {
        "p1": TtsProfile(name="p1", engine="vieneu"),
    })
    await model_registry_store.create(
        "tts", "vieneu", "vieneu", "vieneu — vieneu (in use)")

    await migrate_drop_stale_tts_engine_shims()

    assert await model_registry_store.find("tts", "vieneu", "vieneu") is not None


async def test_keeps_admin_created_row_even_when_unreferenced(monkeypatch):
    # Same (engine, engine) shape, but the label isn't the seeder's fingerprint:
    # an admin catalogued this deliberately. Never delete an admin's row.
    from app.services.tts.profile_store import tts_profile_store
    monkeypatch.setattr(tts_profile_store, "list", lambda: {})
    await model_registry_store.create("tts", "edge_tts", "edge_tts", "Edge TTS (vi)")

    await migrate_drop_stale_tts_engine_shims()

    assert await model_registry_store.find("tts", "edge_tts", "edge_tts") is not None


async def test_leaves_real_model_rows_and_sentinels_alone(monkeypatch):
    from app.services.tts.profile_store import tts_profile_store
    monkeypatch.setattr(tts_profile_store, "list", lambda: {})
    await model_registry_store.create(
        "tts", "http_tts", "vieneu", "VieNeu (local service)",
        base_url="http://127.0.0.1:8101/v1")
    await model_registry_store.create("tts", "omnivoice", "", "OmniVoice (engine config)")

    await migrate_drop_stale_tts_engine_shims()

    assert await model_registry_store.find("tts", "http_tts", "vieneu") is not None
    assert await model_registry_store.find("tts", "omnivoice", "") is not None


async def test_idempotent(monkeypatch):
    from app.services.tts.profile_store import tts_profile_store
    from app.services.tts.profile_models import TtsProfile
    monkeypatch.setattr(tts_profile_store, "list", lambda: {
        "vn-cf": TtsProfile(name="vn-cf", engine="http_tts", model_id="vieneu-cloudflare"),
    })
    await model_registry_store.create(
        "tts", "http_tts", "http_tts", "http_tts — http_tts (in use)")

    await migrate_drop_stale_tts_engine_shims()
    await migrate_drop_stale_tts_engine_shims()  # second boot: no crash, still gone

    assert await model_registry_store.find("tts", "http_tts", "http_tts") is None
