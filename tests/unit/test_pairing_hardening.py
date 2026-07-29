"""Adversarial regression tests for C3 (pairing-code brute-force -> device
hijack -> cross-user conversation read). See docs/superpowers/specs/
2026-07-29-adversarial-audit-findings.md and
app/services/auth/pairing.py's module docstring for the vulnerability and
the three-layer fix (burn-after-N, widened entropy, rate limiting).
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth import pairing as pairing_module


@pytest.fixture(autouse=True)
def _reset_pairing_state():
    """These tests deliberately burn codes and trigger rate-limit bursts
    against process-global singletons shared with every other test file in
    this run -- reset before and after so nothing leaks either direction."""
    pairing_module.pending_pairings.reset()
    pairing_module.claim_rate_limiter.reset()
    pairing_module.init_rate_limiter.reset()
    yield
    pairing_module.pending_pairings.reset()
    pairing_module.claim_rate_limiter.reset()
    pairing_module.init_rate_limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


def _login(client, username):
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


def test_correct_claim_within_limit_still_pairs(client):
    """Baseline: the legitimate flow -- init, display the code, claim once
    with the right code -- must keep working."""
    _login(client, "victim")
    init = client.post("/v1/devices/pair/init", json={"serial": "AA:BB:CC"}).json()["data"]

    status_before = client.get(f"/v1/devices/pair/status?poll_token={init['poll_token']}")
    assert status_before.json()["data"]["claimed"] is False

    claim = client.post(
        "/v1/devices/pair/claim", json={"code": init["code"], "name": "ESP32 desk"}
    )
    assert claim.status_code == 200
    assert claim.json()["data"]["id"]

    status_after = client.get(f"/v1/devices/pair/status?poll_token={init['poll_token']}")
    body = status_after.json()["data"]
    assert body["claimed"] is True
    assert body["device_id"]
    assert body["token"]


def test_brute_force_burns_the_code_before_it_can_be_found(client):
    """Core defense: N+1 failed pair/claim attempts against a pending
    pairing burn it -- a *subsequent claim with the correct code* then
    fails. This must go red without the fix (the old code had no attempt
    counter at all, so the correct code always worked)."""
    _login(client, "attacker")
    init = client.post("/v1/devices/pair/init", json={"serial": "DE:AD:BE:EF"}).json()["data"]
    real_code = init["code"]

    # N+1 wrong guesses (threshold is 5 -- see pairing._MAX_CLAIM_ATTEMPTS).
    # None of these can accidentally collide with the real 8-digit code.
    wrong_codes = [c for c in ("00000000", "11111111", "22222222", "33333333",
                                "44444444", "55555555") if c != real_code]
    assert len(wrong_codes) >= 6
    for code in wrong_codes[:6]:
        resp = client.post("/v1/devices/pair/claim", json={"code": code, "name": "x"})
        assert resp.status_code == 400

    # The pairing is now burned -- even the real code must fail.
    resp = client.post(
        "/v1/devices/pair/claim", json={"code": real_code, "name": "hijacked"}
    )
    assert resp.status_code == 400

    # And the device was never paired to anyone.
    mine = client.get("/v1/devices/mine").json()["data"]
    assert mine == []


def test_burn_threshold_tolerates_a_few_wrong_guesses_then_succeeds(client):
    """Fewer than the burn threshold worth of wrong guesses must not brick
    the legitimate flow -- a few typos are still recoverable."""
    _login(client, "clumsy-user")
    init = client.post("/v1/devices/pair/init", json={"serial": "11:22:33"}).json()["data"]
    real_code = init["code"]

    wrong_codes = [c for c in ("00000000", "11111111", "22222222") if c != real_code]
    for code in wrong_codes[:3]:
        resp = client.post("/v1/devices/pair/claim", json={"code": code, "name": "x"})
        assert resp.status_code == 400

    claim = client.post(
        "/v1/devices/pair/claim", json={"code": real_code, "name": "recovered"}
    )
    assert claim.status_code == 200


def test_sustained_wrong_claim_stream_cannot_indefinitely_block_pairing(client):
    """Round-2 DoS regression (this is the test that would have caught it):
    round 1 charged *every* failed claim against *every* currently-live
    pairing, full stop. A single low-cost account trickling wrong guesses
    could therefore burn any pairing -- including ones created *after* the
    trickle started -- within a handful of requests, indefinitely: no
    legitimate device could ever finish pairing while the attacker
    persisted, since re-init just yielded a fresh code the ongoing stream
    immediately burned again.

    The fix scopes collateral charging to pairings that already existed
    *before* the current miss streak became sustained (see pairing.py's
    module docstring, defense #1, "Round 2"). This test proves a pairing
    created *during* an already-ongoing wrong-claim stream survives the
    stream continuing around it and can still be claimed with its correct
    code -- i.e. the DoS is closed."""
    _login(client, "sustained-attacker")

    # Build a sustained miss streak (the burst threshold is 6 misses within
    # the burst window -- see pairing._BURST_MISS_THRESHOLD). No pairing
    # exists yet, so none of these have anything to burn.
    for i in range(6):
        resp = client.post(
            "/v1/devices/pair/claim", json={"code": f"9{i:07d}", "name": "x"}
        )
        assert resp.status_code == 400

    # A device inits *mid-streak* and gets a code.
    init = client.post(
        "/v1/devices/pair/init", json={"serial": "MID:STREAM:01"}
    ).json()["data"]
    real_code = init["code"]

    # The attacker keeps trickling wrong guesses -- well past the old
    # burn threshold -- while this pairing is live.
    wrong_codes = [c for c in (f"8{i:07d}" for i in range(8)) if c != real_code]
    for code in wrong_codes:
        resp = client.post("/v1/devices/pair/claim", json={"code": code, "name": "x"})
        assert resp.status_code == 400

    # It must still be claimable with its correct code -- closed, not burned.
    claim = client.post(
        "/v1/devices/pair/claim", json={"code": real_code, "name": "survived"}
    )
    assert claim.status_code == 200


def test_claim_burst_is_rate_limited(client):
    """Rate limiting (defense #3): a rapid burst of pair/claim calls from
    the same (IP, user) is throttled with 429, independent of whether any
    individual guess matches a live pairing."""
    _login(client, "burst-attacker")

    statuses = []
    for i in range(40):
        resp = client.post(
            "/v1/devices/pair/claim", json={"code": f"{i:08d}", "name": "x"}
        )
        statuses.append(resp.status_code)

    assert 429 in statuses, f"expected throttling in this burst, got {statuses}"
    # Throttling must kick in well before the whole burst completes -- the
    # limiter's budget (see pairing.claim_rate_limiter) is smaller than 40.
    first_429 = statuses.index(429)
    assert first_429 < 40


def test_init_burst_is_rate_limited(client):
    """pair/init is unauthenticated (device has no session yet) -- it must
    still be rate-limited per IP so an attacker can't spin up unlimited
    decoy pairings to dilute the burn-after-N defense."""
    statuses = []
    for i in range(50):
        resp = client.post("/v1/devices/pair/init", json={"serial": f"SERIAL-{i}"})
        statuses.append(resp.status_code)

    assert 429 in statuses, f"expected throttling in this burst, got {statuses}"
