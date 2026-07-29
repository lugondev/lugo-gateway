import pytest

from app.services.usage.price_schema import (
    PRICE_UNIT_BY_KIND,
    apply_price_to_config,
    validate_price,
)
from app.services.usage.pricing import compute_cost


def test_llm_price_normalized_with_unit_filled_from_kind():
    assert validate_price("llm", {"in": 0.15, "out": 0.6}) == {
        "unit": "1M_tokens", "in": 0.15, "out": 0.6,
    }


def test_missing_rate_key_defaults_to_zero():
    # An embedding model priced input-only is the normal case.
    assert validate_price("embed", {"in": 0.02}) == {"unit": "1M_tokens", "in": 0.02, "out": 0.0}


def test_stt_and_tts_units():
    assert validate_price("stt", {"rate": 0.0032}) == {"unit": "minute", "rate": 0.0032}
    assert validate_price("tts", {"rate": 0.015}) == {"unit": "1k_chars", "rate": 0.015}


def test_explicit_matching_unit_is_accepted():
    assert validate_price("tts", {"unit": "1k_chars", "rate": 1.0})["rate"] == 1.0


def test_wrong_unit_for_kind_is_rejected():
    with pytest.raises(ValueError, match="must be '1k_chars'"):
        validate_price("tts", {"unit": "1M_tokens", "in": 1.0})


def test_unknown_field_is_rejected():
    # The whole point: "input" instead of "in" used to cost $0 forever, silently.
    with pytest.raises(ValueError, match="unknown price field"):
        validate_price("llm", {"input": 0.15})


def test_no_rate_key_at_all_is_rejected():
    with pytest.raises(ValueError, match="at least one of"):
        validate_price("llm", {"unit": "1M_tokens"})


def test_bool_and_negative_and_nonnumeric_rates_are_rejected():
    with pytest.raises(ValueError, match="must be a number"):
        validate_price("stt", {"rate": True})
    with pytest.raises(ValueError, match="must be a number"):
        validate_price("stt", {"rate": "0.01"})
    with pytest.raises(ValueError, match=">= 0"):
        validate_price("stt", {"rate": -1.0})


def test_empty_or_none_means_no_price():
    assert validate_price("llm", None) is None
    assert validate_price("llm", {}) is None


def test_non_dict_price_is_rejected():
    with pytest.raises(ValueError, match="must be an object"):
        validate_price("llm", [0.15, 0.6])


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown kind"):
        validate_price("vision", {"in": 1.0})


def test_every_kind_has_a_unit_compute_cost_understands():
    # Guards the contract between this module and pricing.compute_cost: a unit
    # this module blesses but compute_cost ignores would be a silent $0.
    for kind, unit in PRICE_UNIT_BY_KIND.items():
        price = validate_price(kind, {"in": 1.0} if unit == "1M_tokens" else {"rate": 60.0})
        cost = compute_cost(price, 1_000_000, 0, 60.0)
        assert cost > 0, f"{kind}/{unit} costed nothing"


def test_apply_price_preserves_other_config_keys():
    config = {"provider_id": "prov-1", "device": "cpu"}
    merged = apply_price_to_config("llm", config, {"in": 0.15})
    assert merged == {"provider_id": "prov-1", "device": "cpu",
                      "price": {"unit": "1M_tokens", "in": 0.15, "out": 0.0}}
    assert config == {"provider_id": "prov-1", "device": "cpu"}  # not mutated


def test_apply_price_none_clears_only_the_price_key():
    config = {"provider_id": "prov-1", "price": {"unit": "minute", "rate": 1.0}}
    assert apply_price_to_config("stt", config, None) == {"provider_id": "prov-1"}
