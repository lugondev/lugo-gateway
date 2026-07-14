#!/usr/bin/env python3
"""Benchmark STT accuracy (CER/WER) and latency across engine/glossary configs.

Measures the before/after of the FunASR-inspired optimizations so you can decide
what to enable in production (numbers come from real runs — nothing is assumed):
  - Glossary/hotword biasing (#1): compare "<engine>" vs "<engine>+glossary".
  - Fast-path routing (#4): compare engines per DURATION BUCKET — the ≤1.5s row
    tells you whether a fast engine is accurate enough for short commands and what
    to set conversation_fast_stt_max_ms to.

Manifest: JSONL, one object per line: {"audio": "clip.wav", "text": "phiên âm đúng"}
Audio must be mono 16 kHz WAV (durations are read from the WAV header). Paths are
resolved relative to the manifest file.

Usage:
  python scripts/benchmark_stt.py data/vi_manifest.jsonl \
      --engines whisper,qwen3_asr --glossary config/glossary_vi.txt --warmup

Requires the STT engines' models/deps to be installed (same env as the server).
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api_gateway"))

from app.core.audio import wav_duration_seconds  # noqa: E402
from app.services.stt.benchmark import (  # noqa: E402
    ClipResult,
    aggregate,
    build_configs,
    format_report,
)
from app.services.stt.metrics import cer, wer  # noqa: E402
from app.services.stt.providers.qwen3_asr_provider import (  # noqa: E402
    set_active_qwen3_asr_model,
)
from app.services.stt.service import stt_service  # noqa: E402
from app.services.system_config import system_config_store  # noqa: E402


def load_manifest(path: str) -> list[dict]:
    base = Path(path).resolve().parent
    clips = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            audio = row["audio"]
            if not os.path.isabs(audio):
                audio = str(base / audio)
            clips.append({"audio": audio, "text": row["text"]})
    return clips


async def transcribe_one(
    engine: str, glossary_path: str, qwen3_model: str, wav_bytes: bytes, language: str | None
):
    """Transcribe with a specific engine/glossary/qwen3-size; return (text, latency_s)."""
    # Glossary is read from system_config_store.get().stt_local at call time (cached
    # by path in glossary.py). stt_glossary_path moved off Settings onto the admin
    # System settings store (Task 4); mutate the cached SystemConfig in-process only
    # (not .set(), which would write through to the DB and could clobber whatever
    # the developer has configured there) -- fine for this one-off benchmark run.
    system_config_store.get().stt_local.stt_glossary_path = glossary_path
    if engine == "qwen3_asr":
        set_active_qwen3_asr_model(qwen3_model or None)
    provider = stt_service.get_provider(engine)
    start = time.perf_counter()
    result = await provider.transcribe_bytes(wav_bytes, language)
    latency = time.perf_counter() - start
    return (result.text or "").strip(), latency


async def run(args) -> int:
    clips = load_manifest(args.manifest)
    if args.limit:
        clips = clips[: args.limit]
    if not clips:
        print("No clips in manifest.", file=sys.stderr)
        return 1

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    qwen3_models = [m.strip() for m in args.qwen3_models.split(",") if m.strip()]
    configs = build_configs(engines, args.glossary, qwen3_models)
    print(f"{len(clips)} clips · {len(configs)} configs · language={args.language or 'auto'}\n")

    results: list[ClipResult] = []
    for cfg in configs:
        wavs = {c["audio"]: Path(c["audio"]).read_bytes() for c in clips}
        if args.warmup:
            first = clips[0]
            await transcribe_one(
                cfg.engine, cfg.glossary_path, cfg.qwen3_model, wavs[first["audio"]], args.language
            )
        for c in clips:
            wav_bytes = wavs[c["audio"]]
            try:
                hyp, latency = await transcribe_one(
                    cfg.engine, cfg.glossary_path, cfg.qwen3_model, wav_bytes, args.language
                )
            except Exception as exc:  # noqa: BLE001 - report and skip a bad clip
                print(f"  ! {cfg.label} failed on {c['audio']}: {exc}", file=sys.stderr)
                continue
            results.append(
                ClipResult(
                    label=cfg.label,
                    duration_s=wav_duration_seconds(wav_bytes),
                    latency_s=latency,
                    cer=cer(c["text"], hyp),
                    wer=wer(c["text"], hyp),
                )
            )
        print(f"  done: {cfg.label}")

    print("\n=== By duration bucket ===")
    print(format_report(aggregate(results, by_bucket=True)))
    print("\n=== Overall ===")
    print(format_report(aggregate(results, by_bucket=False)))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nPer-clip results written to {args.json_out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("manifest", help="JSONL manifest of {audio, text}")
    p.add_argument("--engines", default="whisper", help="comma-separated engine names")
    p.add_argument(
        "--qwen3-models",
        default="",
        help="comma-separated qwen3_asr sizes to A/B, e.g. '0.6B,1.7B' (only fans out qwen3_asr)",
    )
    p.add_argument("--glossary", default="", help="glossary file to A/B against (whisper-family only)")
    p.add_argument("--language", default=system_config_store.get().conversation.conversation_language or None)
    p.add_argument("--warmup", action="store_true", help="warm each engine on the first clip (excluded)")
    p.add_argument("--limit", type=int, default=0, help="only the first N clips")
    p.add_argument("--json-out", default="", help="write per-clip results to this JSON file")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
