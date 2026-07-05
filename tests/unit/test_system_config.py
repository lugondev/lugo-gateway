import pytest

from app.services.system_config import SystemConfigStore


@pytest.fixture
def store(tmp_path):
    return SystemConfigStore(str(tmp_path / "system_config.json"))


def test_default_base_context_is_empty(store):
    assert store.get().base_context == ""


def test_set_and_get_base_context(store):
    store.set_base_context("This is TeguVoice, an on-device assistant. Never give medical advice.")
    assert store.get().base_context == "This is TeguVoice, an on-device assistant. Never give medical advice."


def test_set_base_context_persists_across_instances(tmp_path):
    path = str(tmp_path / "system_config.json")
    s1 = SystemConfigStore(path)
    s1.set_base_context("persisted context")
    s2 = SystemConfigStore(path)
    assert s2.get().base_context == "persisted context"


def test_set_base_context_overwrites(store):
    store.set_base_context("first")
    store.set_base_context("second")
    assert store.get().base_context == "second"
