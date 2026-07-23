from app.services import system_config as sc_mod
from app.services.system_config import SystemConfigStore


def _patch_conversation_engines(monkeypatch, tmp_path, *, stt_engine="whisper", tts_engine="vieneu"):
    """conversation_stt_engine/conversation_tts_engine live on
    system_config_store's ``conversation`` group -- build a fresh, isolated
    store and patch it in at the point of use (app.services.system_config),
    following the pattern in tests/unit/test_stt_service_openrouter.py."""
    fresh = SystemConfigStore(str(tmp_path / "system_config.json"))
    fresh.set(
        fresh.get().model_copy(
            update={
                "conversation": fresh.get().conversation.model_copy(
                    update={"conversation_stt_engine": stt_engine, "conversation_tts_engine": tts_engine}
                ),
            }
        )
    )
    monkeypatch.setattr(sc_mod, "system_config_store", fresh)


def test_warmup_stt_engines_returns_the_conversation_engine(monkeypatch, tmp_path):
    _patch_conversation_engines(monkeypatch, tmp_path, stt_engine="whisper")
    assert sc_mod.warmup_stt_engines() == ["whisper"]


def test_warmup_tts_engines_returns_the_conversation_engine(monkeypatch, tmp_path):
    _patch_conversation_engines(monkeypatch, tmp_path, tts_engine="vieneu")
    assert sc_mod.warmup_tts_engines() == ["vieneu"]


# --- boot warm-up enumerates every profile / tts-profile engine ---
from app.services import warmup  # noqa: E402


class _FakeStore:
    def __init__(self, d):
        self._d = d

    def list(self):
        return self._d


def _fake_profile(stt_engine, stt_model=""):
    stt = type("S", (), {"profile": "", "engine": stt_engine, "language": "", "model": stt_model})()
    return type("P", (), {"stt": stt})()


def _fake_tts_profile(engine):
    return type("T", (), {"engine": engine})()


def test_boot_warmup_includes_profile_and_tts_profile_engines(monkeypatch, tmp_path):
    _patch_conversation_engines(monkeypatch, tmp_path, stt_engine="whisper", tts_engine="vieneu")
    monkeypatch.setattr(
        "app.services.profiles.store.profile_store",
        _FakeStore({"p": _fake_profile("qwen3_asr")}),
    )
    monkeypatch.setattr(
        "app.services.tts.profile_store.tts_profile_store",
        _FakeStore({"t": _fake_tts_profile("omnivoice")}),
    )
    stt, tts, stt_models = warmup.engines_for_boot_warmup()
    assert "whisper" in stt and "qwen3_asr" in stt   # settings default + profile
    assert "vieneu" in tts and "omnivoice" in tts     # settings default + tts profile
    assert len(stt) == len(set(stt)) and len(tts) == len(set(tts))  # de-duplicated
    assert stt_models == {}  # no profile set a model


def test_boot_warmup_collects_profile_stt_models(monkeypatch, tmp_path):
    _patch_conversation_engines(monkeypatch, tmp_path, stt_engine="whisper", tts_engine="vieneu")
    monkeypatch.setattr(
        "app.services.profiles.store.profile_store",
        _FakeStore({"p": _fake_profile("qwen3_asr", stt_model="1.7b")}),
    )
    monkeypatch.setattr(
        "app.services.tts.profile_store.tts_profile_store",
        _FakeStore({}),
    )
    _stt, _tts, stt_models = warmup.engines_for_boot_warmup()
    assert stt_models == {"qwen3_asr": "1.7b"}
