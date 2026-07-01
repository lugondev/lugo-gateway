"""Aggregation + reporting for STT benchmarks.

Pure logic (no model/IO) so it can be unit-tested: it turns per-clip results into
grouped summaries (by config label, optionally split by utterance-duration bucket)
and formats a plain-text comparison table. The CLI in scripts/benchmark_stt.py
runs the models and feeds ClipResults here.
"""

from dataclasses import dataclass

from app.services.stt.metrics import percentile

# (lower_inclusive, upper_exclusive, label). Short/medium/long — the short bucket
# is what informs the fast-path routing threshold (conversation_fast_stt_max_ms).
DEFAULT_BUCKETS = (
    (0.0, 1.5, "≤1.5s"),
    (1.5, 3.0, "1.5–3s"),
    (3.0, float("inf"), ">3s"),
)


def duration_bucket(duration_s: float, buckets=DEFAULT_BUCKETS) -> str:
    for lo, hi, label in buckets:
        if lo <= duration_s < hi:
            return label
    return buckets[-1][2]


_WHISPER_FAMILY = ("whisper", "whisper_local", "whisper_mlx")


@dataclass
class BenchConfig:
    label: str
    engine: str
    glossary_path: str  # "" = no glossary
    qwen3_model: str  # "" = provider default; else size shorthand / repo id


def build_configs(engines: list[str], glossary: str, qwen3_models: list[str]) -> list["BenchConfig"]:
    """Expand engines into benchmark configs.

    Each qwen3_asr engine fans out over ``qwen3_models`` (labelled ``@<size>``);
    glossary adds a ``+glossary`` variant only for whisper-family engines (it is a
    no-op elsewhere).
    """
    configs: list[BenchConfig] = []
    for eng in engines:
        sizes = qwen3_models if (eng == "qwen3_asr" and qwen3_models) else [""]
        for size in sizes:
            label = f"{eng}@{size}" if size else eng
            configs.append(BenchConfig(label=label, engine=eng, glossary_path="", qwen3_model=size))
            if glossary and eng in _WHISPER_FAMILY:
                configs.append(
                    BenchConfig(
                        label=f"{label}+glossary", engine=eng, glossary_path=glossary, qwen3_model=size
                    )
                )
    return configs


@dataclass
class ClipResult:
    label: str  # config label, e.g. "phowhisper" or "phowhisper+glossary"
    duration_s: float
    latency_s: float
    cer: float
    wer: float


@dataclass
class Summary:
    label: str
    bucket: str  # "all" when not split by bucket
    count: int
    cer_mean: float
    wer_mean: float
    lat_mean: float
    lat_p50: float
    lat_p95: float


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def aggregate(results: list[ClipResult], by_bucket: bool = True) -> list[Summary]:
    """Group results by (label[, duration bucket]) and compute means/percentiles.

    Ordering: by label (first appearance), then by DEFAULT_BUCKETS order.
    """
    bucket_order = {label: i for i, (_, _, label) in enumerate(DEFAULT_BUCKETS)}
    label_order: dict[str, int] = {}
    groups: dict[tuple[str, str], list[ClipResult]] = {}
    for r in results:
        label_order.setdefault(r.label, len(label_order))
        bucket = duration_bucket(r.duration_s) if by_bucket else "all"
        groups.setdefault((r.label, bucket), []).append(r)

    summaries = []
    for (label, bucket), items in groups.items():
        lats = [i.latency_s for i in items]
        summaries.append(
            Summary(
                label=label,
                bucket=bucket,
                count=len(items),
                cer_mean=_mean([i.cer for i in items]),
                wer_mean=_mean([i.wer for i in items]),
                lat_mean=_mean(lats),
                lat_p50=percentile(lats, 50),
                lat_p95=percentile(lats, 95),
            )
        )
    summaries.sort(key=lambda s: (label_order[s.label], bucket_order.get(s.bucket, 99)))
    return summaries


def format_report(summaries: list[Summary]) -> str:
    """Render summaries as an aligned text table."""
    header = ("Config", "Bucket", "N", "CER%", "WER%", "lat_mean", "lat_p50", "lat_p95")
    rows = [header]
    for s in summaries:
        rows.append(
            (
                s.label,
                s.bucket,
                str(s.count),
                f"{s.cer_mean * 100:.1f}",
                f"{s.wer_mean * 100:.1f}",
                f"{s.lat_mean:.2f}s",
                f"{s.lat_p50:.2f}s",
                f"{s.lat_p95:.2f}s",
            )
        )
    widths = [max(len(row[c]) for row in rows) for c in range(len(header))]
    lines = []
    for i, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)))
        if i == 0:
            lines.append("  ".join("-" * widths[c] for c in range(len(header))))
    return "\n".join(lines)
