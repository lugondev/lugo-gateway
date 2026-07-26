"""Write-time validation for a Model Registry entry's config["price"].

compute_cost (usage/pricing.py) resolves an unrecognized price to $0.0 --
the right runtime fallback, but a terrible sole feedback channel for an
admin who typed "input" instead of "in". This module is the write-time gate:
every path that stores a price runs it, so a bad shape is a 400 at save time
rather than a silently free month of billing.

The unit is derived from the kind and never free-typed:
  llm/embed -> "1M_tokens", USD per 1M tokens, keys "in"/"out"
  stt       -> "minute",    USD per minute of audio, key "rate"
  tts       -> "1k_chars",  USD per 1000 characters, key "rate"
"""

from __future__ import annotations

PRICE_UNIT_BY_KIND = {
    "llm": "1M_tokens",
    "embed": "1M_tokens",
    "stt": "minute",
    "tts": "1k_chars",
}

# Rate keys each unit accepts, named exactly as compute_cost reads them.
_RATE_KEYS = {"1M_tokens": ("in", "out"), "minute": ("rate",), "1k_chars": ("rate",)}


def _as_rate(value, key: str) -> float:
    # bool before the numeric check -- bool is an int subclass, so without this
    # price={"rate": True} would quietly become $1.00 per minute.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"price.{key} must be a number, got {value!r}")
    rate = float(value)
    if rate < 0:
        raise ValueError(f"price.{key} must be >= 0, got {rate}")
    return rate


def validate_price(kind: str, price) -> dict | None:
    """Normalized price for `kind`, or None meaning "no price / clear it".

    Raises ValueError (message is surfaced verbatim to the admin) otherwise.
    """
    if kind not in PRICE_UNIT_BY_KIND:
        raise ValueError(
            f"unknown kind '{kind}' for pricing "
            f"(expected one of {sorted(PRICE_UNIT_BY_KIND)})"
        )
    if price is None or price == {}:
        return None
    if not isinstance(price, dict):
        raise ValueError(f"price must be an object, got {type(price).__name__}")

    unit = PRICE_UNIT_BY_KIND[kind]
    given_unit = price.get("unit")
    if given_unit is not None and given_unit != unit:
        raise ValueError(f"price.unit for kind '{kind}' must be '{unit}', got '{given_unit}'")

    rate_keys = _RATE_KEYS[unit]
    unknown = sorted(set(price) - {"unit"} - set(rate_keys))
    if unknown:
        raise ValueError(
            f"unknown price field(s) {unknown} for unit '{unit}' (expected {list(rate_keys)})"
        )
    if not any(key in price for key in rate_keys):
        raise ValueError(f"price for unit '{unit}' needs at least one of {list(rate_keys)}")

    normalized = {"unit": unit}
    for key in rate_keys:
        normalized[key] = _as_rate(price[key], key) if key in price else 0.0
    return normalized


def apply_price_to_config(kind: str, config: dict, price) -> dict:
    """A copy of `config` with a validated price merged in (or the "price" key
    removed when price is None/{}). Merges rather than replaces so provider_id
    and the engine's own config keys survive a pricing edit."""
    validated = validate_price(kind, price)
    merged = dict(config or {})
    if validated is None:
        merged.pop("price", None)
    else:
        merged["price"] = validated
    return merged
