from app.services.tts.service import tts_service


def test_tts_list_engines_has_mode_and_http_tts_is_remote():
    engines = tts_service.list_engines()
    assert engines, "expected at least one tts engine registered"
    by_name = {e["engine"]: e for e in engines}
    # every engine carries a mode in {local, remote}
    assert all(e.get("mode") in ("local", "remote") for e in engines)
    # http_tts is the one remote (OpenAI-compatible HTTP) tts engine
    if "http_tts" in by_name:
        assert by_name["http_tts"]["mode"] == "remote"
    # a representative local engine is "local" (edge_tts always registers)
    local_names = [n for n, e in by_name.items() if e["mode"] == "local"]
    assert local_names, "expected at least one local tts engine"
    assert "http_tts" not in local_names
