"""Removing a registry row clears the profile bindings that pinned exactly it,
so the profile falls back to the server default instead of a row that's gone.
See docs/superpowers/specs/2026-07-25-registry-delete-cascade-design.md."""

from app.services.model_registry.cascade import clear_bindings_for
from app.services.profiles.models import LlmConfig, Profile, SttConfig
from app.services.profiles.store import profile_store
from app.services.tts.profile_models import TtsProfile
from app.services.tts.profile_store import tts_profile_store


async def test_clears_stt_binding_and_leaves_the_rest_of_the_profile_alone():
    profile_store.upsert(Profile(
        name="p1",
        nickname="keep me",
        stt=SttConfig(engine="http_stt", model="Qwen/Qwen3-ASR-0.6B", language="vi"),
        llm=LlmConfig(engine="OA", model="gpt-4o-mini"),
    ))

    cleared = await clear_bindings_for("stt", "http_stt", "Qwen/Qwen3-ASR-0.6B")

    assert cleared == ["p1 (stt)"]
    after = profile_store.get("p1")
    assert (after.stt.engine, after.stt.model) == ("", "")
    # only the pinned row is cleared -- language/nickname/llm are the admin's
    assert after.stt.language == "vi"
    assert after.nickname == "keep me"
    assert (after.llm.engine, after.llm.model) == ("OA", "gpt-4o-mini")


async def test_clears_llm_binding():
    profile_store.upsert(Profile(name="p1", llm=LlmConfig(engine="OA", model="gpt-4o-mini")))

    cleared = await clear_bindings_for("llm", "OA", "gpt-4o-mini")

    assert cleared == ["p1 (llm)"]
    after = profile_store.get("p1")
    assert (after.llm.engine, after.llm.model) == ("", "")


async def test_clears_tts_profile_binding():
    tts_profile_store.upsert(TtsProfile(
        name="vn-fly", engine="http_tts", model_id="vieneu-fly", language="vi"))

    cleared = await clear_bindings_for("tts", "http_tts", "vieneu-fly")

    assert cleared == ["vn-fly (tts profile)"]
    after = tts_profile_store.get("vn-fly")
    assert (after.engine, after.model_id) == ("", "")
    assert after.language == "vi"


async def test_clears_every_profile_pinning_the_same_row():
    profile_store.upsert(Profile(name="a", stt=SttConfig(engine="whisper", model="large-v3")))
    profile_store.upsert(Profile(name="b", stt=SttConfig(engine="whisper", model="large-v3")))
    profile_store.upsert(Profile(name="c", stt=SttConfig(engine="whisper", model="large-v3-turbo")))

    cleared = await clear_bindings_for("stt", "whisper", "large-v3")

    assert sorted(cleared) == ["a (stt)", "b (stt)"]
    assert profile_store.get("c").stt.model == "large-v3-turbo"  # different row, untouched


async def test_no_match_changes_nothing():
    profile_store.upsert(Profile(name="p1", stt=SttConfig(engine="whisper", model="large-v3")))

    assert await clear_bindings_for("stt", "vosk", "vosk-model-small-en-us-0.15") == []
    assert profile_store.get("p1").stt.model == "large-v3"


async def test_a_blank_binding_is_never_a_match():
    # engine="" / model="" already MEANS "inherit the server default" -- it pins
    # no row, so no row's removal may rewrite it.
    profile_store.upsert(Profile(name="p1", stt=SttConfig(engine="", model="")))
    tts_profile_store.upsert(TtsProfile(name="t1", engine="vieneu", model_id=""))

    assert await clear_bindings_for("stt", "", "") == []
    assert await clear_bindings_for("tts", "vieneu", "") == []
    # the (engine, engine) shim shape doesn't match a model_id-less profile either
    assert await clear_bindings_for("tts", "vieneu", "vieneu") == []
    assert tts_profile_store.get("t1").engine == "vieneu"


async def test_kind_scopes_the_match():
    # same (engine, model) text under a different kind must not match
    profile_store.upsert(Profile(name="p1", llm=LlmConfig(engine="dup", model="dup")))
    profile_store.upsert(Profile(name="p2", stt=SttConfig(engine="dup", model="dup")))

    assert await clear_bindings_for("llm", "dup", "dup") == ["p1 (llm)"]
    assert profile_store.get("p2").stt.engine == "dup"
