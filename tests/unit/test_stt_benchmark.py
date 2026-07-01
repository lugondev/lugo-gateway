import pytest

from app.services.stt.benchmark import (
    ClipResult,
    aggregate,
    build_configs,
    duration_bucket,
    format_report,
)


def test_build_configs_plain_engine():
    cfgs = build_configs(["whisper"], glossary="", qwen3_models=[])
    assert [(c.label, c.engine, c.glossary_path, c.qwen3_model) for c in cfgs] == [
        ("whisper", "whisper", "", "")
    ]


def test_build_configs_adds_glossary_variant_for_whisper_family():
    cfgs = build_configs(["whisper"], glossary="g.txt", qwen3_models=[])
    labels = [c.label for c in cfgs]
    assert labels == ["whisper", "whisper+glossary"]
    assert cfgs[1].glossary_path == "g.txt"


def test_build_configs_glossary_not_applied_to_qwen():
    cfgs = build_configs(["qwen3_asr"], glossary="g.txt", qwen3_models=[])
    assert [c.label for c in cfgs] == ["qwen3_asr"]  # no +glossary (no-op for qwen)


def test_build_configs_expands_qwen3_model_sizes():
    cfgs = build_configs(["qwen3_asr"], glossary="", qwen3_models=["0.6B", "1.7B"])
    assert [(c.label, c.qwen3_model) for c in cfgs] == [
        ("qwen3_asr@0.6B", "0.6B"),
        ("qwen3_asr@1.7B", "1.7B"),
    ]


def test_build_configs_mixed():
    cfgs = build_configs(["whisper", "qwen3_asr"], glossary="g.txt", qwen3_models=["1.7B"])
    assert [c.label for c in cfgs] == ["whisper", "whisper+glossary", "qwen3_asr@1.7B"]


def test_duration_bucket_edges():
    assert duration_bucket(0.5) == "≤1.5s"
    assert duration_bucket(1.5) == "1.5–3s"  # lower edge is inclusive
    assert duration_bucket(2.9) == "1.5–3s"
    assert duration_bucket(3.0) == ">3s"
    assert duration_bucket(10.0) == ">3s"


def _r(label, dur, lat, c, w):
    return ClipResult(label=label, duration_s=dur, latency_s=lat, cer=c, wer=w)


def test_aggregate_means_over_a_group():
    results = [
        _r("A", 1.0, 0.2, 0.10, 0.20),
        _r("A", 1.0, 0.4, 0.20, 0.40),
    ]
    summ = aggregate(results, by_bucket=False)
    assert len(summ) == 1
    s = summ[0]
    assert s.label == "A"
    assert s.count == 2
    assert s.cer_mean == pytest.approx(0.15)
    assert s.wer_mean == pytest.approx(0.30)
    assert s.lat_mean == pytest.approx(0.30)


def test_aggregate_groups_by_label():
    results = [_r("A", 1, 0.2, 0.1, 0.1), _r("B", 1, 0.5, 0.3, 0.3)]
    summ = aggregate(results, by_bucket=False)
    assert {s.label for s in summ} == {"A", "B"}


def test_aggregate_by_bucket_splits_durations():
    results = [
        _r("A", 0.8, 0.2, 0.1, 0.1),  # ≤1.5s
        _r("A", 5.0, 0.9, 0.2, 0.2),  # >3s
    ]
    summ = aggregate(results, by_bucket=True)
    buckets = {s.bucket for s in summ}
    assert buckets == {"≤1.5s", ">3s"}
    assert all(s.count == 1 for s in summ)


def test_aggregate_reports_latency_percentiles():
    results = [_r("A", 1, lat, 0.1, 0.1) for lat in (0.1, 0.2, 0.3, 0.4)]
    s = aggregate(results, by_bucket=False)[0]
    assert s.lat_p50 == pytest.approx(0.25)
    assert s.lat_p95 == pytest.approx(0.385, abs=1e-3)


def test_format_report_contains_headers_and_labels():
    results = [_r("phowhisper", 1, 0.2, 0.1, 0.2), _r("phowhisper+glossary", 1, 0.2, 0.05, 0.1)]
    report = format_report(aggregate(results, by_bucket=False))
    assert "CER" in report and "WER" in report
    assert "phowhisper" in report
    assert "phowhisper+glossary" in report
