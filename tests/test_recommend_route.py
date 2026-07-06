"""Route + service integration test for GET /v1/models/recommend."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_recommend_endpoint_shape_and_sorting():
    resp = client.get("/v1/models/recommend")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]

    caps = data["capabilities"]
    for key in ("os", "arch", "apple_silicon", "mlx", "cuda", "ram_total_gb"):
        assert key in caps

    categories = data["categories"]
    for cat in ("stt", "tts", "llm", "vad"):
        assert cat in categories
        items = categories[cat]
        assert isinstance(items, list) and items, f"{cat} should have candidates"
        # sorted by fit_score descending
        scores = [it["fit_score"] for it in items]
        assert scores == sorted(scores, reverse=True)
        for it in items:
            assert it["chip"] in ("apple_silicon", "cpu", "nvidia_gpu")
            assert "status" in it and "recommended" in it and "action" in it


def test_apple_only_engines_incompatible_on_non_apple_host():
    # The test/CI host is Linux x86 (no Apple Silicon). whisper_mlx must therefore be
    # reported incompatible, never recommended.
    import platform

    if platform.system().lower() == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        return  # skip the assertion on Apple Silicon dev machines

    data = client.get("/v1/models/recommend").json()["data"]
    apple_engines = {"whisper_mlx"}
    apple_items = [
        it for cat in data["categories"].values() for it in cat if it["engine"] in apple_engines
    ]
    assert apple_items, "expected apple-only engines in the catalog"
    for it in apple_items:
        assert it["status"].startswith("incompatible")
        assert it["recommended"] is False
