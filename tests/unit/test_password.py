from app.services.auth.password import hash_password, verify_password


def test_hash_then_verify_round_trip():
    encoded = hash_password("correct-horse")
    assert verify_password("correct-horse", encoded) is True


def test_verify_rejects_wrong_password():
    encoded = hash_password("correct-horse")
    assert verify_password("wrong", encoded) is False


def test_hash_is_salted_differently_each_time():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a) is True
    assert verify_password("same-password", b) is True


def test_verify_rejects_malformed_hash():
    assert verify_password("anything", "not-a-valid-hash") is False
