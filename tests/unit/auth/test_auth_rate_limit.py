"""Password endpoints had no rate limiting of any kind: the only limiters in
the codebase were on device pairing, so /api/auth/login, /api/auth/token and
/api/auth/signup accepted unlimited guesses at full speed.

Only FAILED attempts are charged. Charging successes would let anyone lock a
real user out of their own account by burning the budget on purpose.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import (
    LOGIN_MAX_FAILURES_PER_ACCOUNT,
    login_account_limiter,
    login_ip_limiter,
    signup_limiter,
)
from app.core.settings import settings
from app.main import app


@pytest.fixture(autouse=True)
def _fresh_limiters():
    for limiter in (login_account_limiter, login_ip_limiter, signup_limiter):
        limiter.reset()
    yield
    for limiter in (login_account_limiter, login_ip_limiter, signup_limiter):
        limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


def _signup(client, username, password):
    assert client.post(
        "/api/auth/signup", json={"username": username, "password": password}
    ).status_code == 200


def test_repeated_wrong_passwords_are_eventually_refused(client):
    _signup(client, "brutus", "correct-horse")
    statuses = [
        client.post(
            "/api/auth/login", json={"username": "brutus", "password": f"guess{i}"}
        ).status_code
        for i in range(LOGIN_MAX_FAILURES_PER_ACCOUNT + 3)
    ]
    assert statuses[0] == 401
    assert 429 in statuses


def test_the_token_endpoint_is_limited_too(client):
    _signup(client, "brutus-token", "correct-horse")
    statuses = [
        client.post(
            "/api/auth/token",
            json={"username": "brutus-token", "password": f"guess{i}"},
        ).status_code
        for i in range(LOGIN_MAX_FAILURES_PER_ACCOUNT + 3)
    ]
    assert 429 in statuses


def test_a_correct_password_is_never_charged_against_the_limit(client):
    """Otherwise anyone could lock a user out of their own account by spending
    their budget for them."""
    _signup(client, "steady", "correct-horse")
    for _ in range(LOGIN_MAX_FAILURES_PER_ACCOUNT * 3):
        resp = client.post(
            "/api/auth/login", json={"username": "steady", "password": "correct-horse"}
        )
        assert resp.status_code == 200


def test_a_locked_out_account_does_not_lock_out_a_different_account(client):
    _signup(client, "target-a", "correct-horse")
    _signup(client, "target-b", "correct-horse")
    for i in range(LOGIN_MAX_FAILURES_PER_ACCOUNT + 3):
        client.post(
            "/api/auth/login", json={"username": "target-a", "password": f"g{i}"}
        )
    assert client.post(
        "/api/auth/login", json={"username": "target-b", "password": "correct-horse"}
    ).status_code == 200


def test_signup_is_rate_limited(client):
    statuses = [
        client.post(
            "/api/auth/signup", json={"username": f"spam{i}", "password": "correct-horse"}
        ).status_code
        for i in range(40)
    ]
    assert 429 in statuses


def test_login_does_not_leak_whether_a_username_exists_via_timing(client):
    """verify_login used to return before hashing when the username missed, so
    a miss came back ~27x faster than a hit -- a free user-enumeration oracle
    in front of a 600k-round PBKDF2."""
    _signup(client, "known-user", "correct-horse")

    def _elapsed_ms(username):
        login_account_limiter.reset()
        login_ip_limiter.reset()
        started = time.perf_counter()
        client.post("/api/auth/login", json={"username": username, "password": "nope"})
        return (time.perf_counter() - started) * 1000

    existing = min(_elapsed_ms("known-user") for _ in range(3))
    missing = min(_elapsed_ms("no-such-user-at-all") for _ in range(3))

    # Generous bound: the point is that the missing-user path now does the same
    # PBKDF2 work, not that the two are identical to the microsecond.
    assert missing > existing * 0.5, f"existing={existing:.1f}ms missing={missing:.1f}ms"
