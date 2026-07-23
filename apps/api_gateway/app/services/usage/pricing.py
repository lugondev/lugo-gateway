"""Convert a usage measurement to USD using a model's config.price.

Price shapes (stored in a Model Registry entry's config["price"]):
  LLM: {"unit": "1M_tokens", "in": <usd per 1M input>, "out": <usd per 1M output>}
  STT: {"unit": "minute",    "rate": <usd per minute of audio>}
  TTS: {"unit": "1k_chars",  "rate": <usd per 1000 characters>}
Anything missing/unrecognized costs 0.0 (usage is still recorded, just uncosted).
"""
from __future__ import annotations


def compute_cost(price, prompt_tokens, completion_tokens, native_amount):
    if not price or not isinstance(price, dict):
        return 0.0
    unit = price.get("unit")
    if unit == "1M_tokens":
        pin = float(price.get("in", 0.0))
        pout = float(price.get("out", 0.0))
        return (prompt_tokens or 0) / 1_000_000 * pin + (completion_tokens or 0) / 1_000_000 * pout
    if unit == "minute":
        return float(native_amount or 0.0) / 60.0 * float(price.get("rate", 0.0))
    if unit == "1k_chars":
        return float(native_amount or 0.0) / 1000.0 * float(price.get("rate", 0.0))
    return 0.0
