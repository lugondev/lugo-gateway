from app.services.usage.pricing import compute_cost


def test_llm_1m_tokens_split():
    price = {"unit": "1M_tokens", "in": 0.15, "out": 0.60}
    # 1000 in, 500 out
    cost = compute_cost(price, 1000, 500, 1500)
    assert abs(cost - (1000/1_000_000*0.15 + 500/1_000_000*0.60)) < 1e-12


def test_stt_minute():
    price = {"unit": "minute", "rate": 0.006}
    assert abs(compute_cost(price, None, None, 90.0) - (90.0/60*0.006)) < 1e-12


def test_tts_1k_chars():
    price = {"unit": "1k_chars", "rate": 0.015}
    assert abs(compute_cost(price, None, None, 500.0) - (500.0/1000*0.015)) < 1e-12


def test_missing_or_unknown_price_is_zero():
    assert compute_cost(None, 1000, 500, 1500) == 0.0
    assert compute_cost({}, 1000, 500, 1500) == 0.0
    assert compute_cost({"unit": "furlongs"}, None, None, 5) == 0.0
