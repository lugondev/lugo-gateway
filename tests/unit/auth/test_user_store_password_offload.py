"""PBKDF2 (600k iterations, ~100-300ms of pure CPU) must not run on the event
loop: a burst of /auth/login attempts would stall every live voice session.
These tests spy on the hash/verify entry points used by UserStore and assert
they execute on a worker thread, not the loop thread.
"""

import asyncio
import threading

import pytest

from app.services.auth import users as users_module
from app.services.auth.users import UserStore


@pytest.fixture
def hash_threads(monkeypatch):
    threads: list[int] = []
    real_hash = users_module.hash_password

    def spy_hash(password: str) -> str:
        threads.append(threading.get_ident())
        return real_hash(password)

    monkeypatch.setattr(users_module, "hash_password", spy_hash)
    return threads


@pytest.fixture
def verify_threads(monkeypatch):
    threads: list[int] = []
    real_verify = users_module.verify_password

    def spy_verify(password: str, encoded: str) -> bool:
        threads.append(threading.get_ident())
        return real_verify(password, encoded)

    monkeypatch.setattr(users_module, "verify_password", spy_verify)
    return threads


async def test_create_hashes_password_off_the_event_loop(hash_threads):
    await UserStore().create("alice", "pw-alice")

    loop_thread = threading.get_ident()
    assert hash_threads, "hash_password was never called"
    assert all(t != loop_thread for t in hash_threads)


async def test_verify_login_verifies_off_the_event_loop(verify_threads):
    store = UserStore()
    await store.create("bob", "pw-bob")

    assert await store.verify_login("bob", "pw-bob") is not None
    assert await store.verify_login("bob", "wrong") is None

    loop_thread = threading.get_ident()
    assert verify_threads, "verify_password was never called"
    assert all(t != loop_thread for t in verify_threads)


async def test_concurrent_duplicate_signups_map_to_username_taken():
    """The off-loop hash opened a 100-300ms window between the uniqueness
    check and the INSERT: two concurrent signups for the same username both
    passed the check and the loser surfaced a raw IntegrityError (HTTP 500)
    instead of UsernameTakenError. The unique-constraint violation must be
    mapped, and neither request may 500."""
    from app.core.errors import UsernameTakenError

    store = UserStore()
    results = await asyncio.gather(
        store.create("dupe", "pw-one"),
        store.create("dupe", "pw-two"),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, dict)]
    taken = [r for r in results if isinstance(r, UsernameTakenError)]
    assert len(successes) == 1, f"expected exactly one winner, got {results!r}"
    assert len(taken) == 1, f"loser must raise UsernameTakenError, got {results!r}"


async def test_reset_password_hashes_off_the_event_loop(hash_threads):
    store = UserStore()
    user = await store.create("carol", "pw-old")
    hash_threads.clear()

    assert await store.reset_password(user["id"], "pw-new") is True

    loop_thread = threading.get_ident()
    assert hash_threads, "hash_password was never called"
    assert all(t != loop_thread for t in hash_threads)
