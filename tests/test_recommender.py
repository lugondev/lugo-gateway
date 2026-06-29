"""Tests for the config-aware model recommender (pure scoring/ranking logic)."""

from app.services.recommend.capabilities import Capabilities
from app.services.recommend.recommender import Candidate, evaluate, rank


def caps(**over) -> Capabilities:
    base = dict(
        os="linux",
        arch="x86_64",
        apple_silicon=False,
        cpu_count=8,
        ram_total_gb=8.0,
        disk_free_gb=100.0,
        mlx=False,
        mlx_vlm_version=None,
        cuda=False,
        libopus=False,
        ollama=False,
        modules={"faster_whisper": True, "vosk": True},
    )
    base.update(over)
    return Capabilities(**base)


def cand(**over) -> Candidate:
    base = dict(
        category="stt",
        id="phowhisper-medium",
        engine="whisper",
        label="PhoWhisper Medium",
        chip="cpu",
        tier="high",
        vietnamese=True,
        size_gb=1.5,
        size_estimate="~1.5 GB",
        min_ram_gb=4.0,
        requires=["faster_whisper"],
        action={"kind": "download", "method": "POST", "path": "/v1/models/whisper/download",
                "payload": {"size": "phowhisper-medium"}},
    )
    base.update(over)
    return Candidate(**base)


def test_cpu_engine_present_but_not_downloaded_is_runnable_and_recommended():
    r = evaluate(cand(), caps(), installed_ids=set())
    assert r["status"] == "runnable"
    assert r["recommended"] is True
    assert r["chip"] == "cpu"
    assert r["fit_score"] == 50 + 30 + 10  # base + high tier + vietnamese


def test_installed_model_gets_installed_status_and_bonus():
    r = evaluate(cand(), caps(), installed_ids={"phowhisper-medium"})
    assert r["status"] == "installed"
    assert r["fit_score"] == 50 + 30 + 10 + 10  # + installed bonus


def test_apple_only_engine_is_incompatible_on_linux():
    c = cand(id="qwen-4bit", engine="qwen_omni", chip="apple_silicon",
             requires=["mlx"], min_ram_gb=24.0)
    r = evaluate(c, caps(), installed_ids=set())
    assert r["status"] == "incompatible:apple_silicon"
    assert r["fit_score"] == 0
    assert r["recommended"] is False


def test_apple_engine_runnable_on_apple_silicon_with_mlx():
    c = cand(id="qwen-4bit", engine="qwen_omni", chip="apple_silicon",
             requires=["mlx"], min_ram_gb=24.0)
    r = evaluate(c, caps(os="darwin", arch="arm64", apple_silicon=True, mlx=True,
                         ram_total_gb=32.0), installed_ids=set())
    assert r["status"] == "runnable"
    assert r["recommended"] is True


def test_apple_engine_without_mlx_needs_mlx():
    c = cand(id="qwen-4bit", engine="qwen_omni", chip="apple_silicon",
             requires=["mlx"], min_ram_gb=24.0)
    r = evaluate(c, caps(os="darwin", arch="arm64", apple_silicon=True, mlx=False,
                         ram_total_gb=32.0), installed_ids=set())
    assert r["status"] == "needs:mlx"
    assert r["recommended"] is False


def test_gpu_engine_incompatible_without_cuda():
    c = cand(category="tts", id="vieneu-turbo", engine="vieneu", chip="nvidia_gpu",
             requires=["vieneu"], tier="high", min_ram_gb=None)
    r = evaluate(c, caps(modules={"vieneu": True}), installed_ids=set())
    assert r["status"] == "incompatible:nvidia_gpu"
    assert r["fit_score"] == 0


def test_ram_gate_marks_incompatible():
    c = cand(min_ram_gb=24.0)
    r = evaluate(c, caps(ram_total_gb=8.0), installed_ids=set())
    assert r["status"] == "incompatible:ram"
    assert r["fit_score"] == 0


def test_disk_gate_blocks_download_when_not_installed():
    c = cand(size_gb=50.0)
    r = evaluate(c, caps(disk_free_gb=10.0), installed_ids=set())
    assert r["status"] == "needs:disk"
    assert r["recommended"] is False


def test_low_tier_multilingual_runnable_but_below_recommend_threshold():
    c = cand(id="tiny", tier="low", vietnamese=False, min_ram_gb=1.0)
    r = evaluate(c, caps(), installed_ids=set())
    assert r["status"] == "runnable"
    assert r["fit_score"] == 50 + 5  # 55, below 60
    assert r["recommended"] is False


def test_missing_engine_module_needs_it_not_incompatible():
    c = cand()  # requires faster_whisper
    r = evaluate(c, caps(modules={"faster_whisper": False, "vosk": True}), installed_ids=set())
    assert r["status"] == "needs:faster_whisper"
    assert r["recommended"] is False


def test_rank_sorts_by_fit_descending():
    cs = [
        cand(id="tiny", tier="low", vietnamese=False, min_ram_gb=1.0),
        cand(id="phowhisper-medium", tier="high", vietnamese=True),
        cand(id="qwen", chip="apple_silicon", requires=["mlx"], min_ram_gb=24.0),
    ]
    ranked = rank(cs, caps(), installed_ids=set())
    ids = [r["id"] for r in ranked]
    assert ids[0] == "phowhisper-medium"  # 90
    assert ids[1] == "tiny"               # 55
    assert ids[2] == "qwen"               # 0 incompatible -> bottom
