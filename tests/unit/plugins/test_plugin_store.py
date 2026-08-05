import pytest

from app.services.plugins.models import Plugin, PluginMount
from app.services.plugins.store import plugin_store


@pytest.fixture(autouse=True)
def _fresh_store():
    plugin_store.invalidate()
    yield
    plugin_store.invalidate()


def _plugin(name: str = "livehost", **over) -> Plugin:
    data = {
        "name": name,
        "url": "http://127.0.0.1:8091",
        "secret": "s3cret",
        "mounts": [PluginMount(path="/v1/livehost/stream", kind="ws")],
    }
    data.update(over)
    return Plugin(**data)


def test_roundtrip_through_the_store():
    plugin_store.upsert(_plugin())
    got = plugin_store.get("livehost")
    assert got is not None
    assert got.url == "http://127.0.0.1:8091"
    assert got.secret == "s3cret"
    assert got.enabled is True
    assert got.kind == "feature"
    assert got.mounts[0].path == "/v1/livehost/stream"
    assert got.mounts[0].kind == "ws"


def test_exists_reports_occupancy_for_the_409_check():
    assert plugin_store.exists("livehost") is False
    plugin_store.upsert(_plugin())
    assert plugin_store.exists("livehost") is True


def test_delete_removes_the_row():
    plugin_store.upsert(_plugin())
    plugin_store.delete("livehost")
    assert plugin_store.get("livehost") is None
    assert plugin_store.exists("livehost") is False


def test_list_returns_every_plugin_keyed_by_name():
    plugin_store.upsert(_plugin("livehost"))
    plugin_store.upsert(_plugin("lugo", url="http://127.0.0.1:8092"))
    assert sorted(plugin_store.list()) == ["livehost", "lugo"]


@pytest.mark.parametrize("bad", ["ftp://x", "file:///etc/passwd", "not-a-url"])
def test_url_scheme_is_refused(bad):
    with pytest.raises(ValueError):
        _plugin(url=bad)


def test_https_url_is_accepted():
    assert _plugin(url="https://livehost.internal:8091").url.startswith("https://")
