import asyncio
import logging
import threading

import pytest
from unittest.mock import AsyncMock

from app.services.system_config import system_config_store
from app.services.tts.providers.omnivoice_provider import OmniVoiceProvider
from app.services.tts.providers import omnivoice_provider as ov_mod


def test_omnivoice_timeout_is_not_absurdly_long():
    # Real-time conversation TTS; a 600s (10 min) timeout was functionally
    # indistinguishable from a permanent hang if the sidecar ever stalls on a
    # request. Cap it to something a user could plausibly tolerate.
    assert system_config_store.get().omnivoice.omnivoice_timeout_seconds <= 60


@pytest.mark.asyncio
async def test_server_synth_logs_and_reraises_on_cancellation(monkeypatch, caplog):
    provider = OmniVoiceProvider()
    monkeypatch.setattr(provider, "_ensure_server", AsyncMock(return_value=None))

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            await asyncio.sleep(10)

    monkeypatch.setattr(
        "app.services.tts.providers.omnivoice_provider.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(),
    )

    task = asyncio.create_task(provider._server_synth("hello", None, None, None, None))
    await asyncio.sleep(0.05)
    task.cancel()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            await task

    assert any("cancel" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_ensure_voice_ref_is_single_flight_under_concurrent_cold_start(monkeypatch, tmp_path):
    ov_mod._voice_ref.clear()
    build_calls = []

    async def fake_synth(self, text, instruct=None, ref_audio=None, ref_text=None, speed=None):
        build_calls.append(1)
        await asyncio.sleep(0.05)  # widen the race window
        return b"fake-wav-bytes"

    monkeypatch.setattr(ov_mod.OmniVoiceProvider, "_synth", fake_synth)
    monkeypatch.setattr(ov_mod.settings, "artifacts_dir", str(tmp_path))

    provider = ov_mod.OmniVoiceProvider()
    results = await asyncio.gather(*[provider._ensure_voice_ref() for _ in range(8)])

    assert len(build_calls) == 1, f"voice ref synthesized {len(build_calls)}x — not single-flight"
    assert all(r["path"] == results[0]["path"] for r in results)

    ov_mod._voice_ref.clear()


def test_spawn_sidecar_tracks_the_process_handle(monkeypatch, tmp_path):
    monkeypatch.setattr(ov_mod.settings, "artifacts_dir", str(tmp_path))
    fake_popen_calls = []

    class _FakePopen:
        def __init__(self, *a, **kw):
            fake_popen_calls.append((a, kw))
            self.pid = 12345
            self._killed = False

        def poll(self):
            return None if not self._killed else 0

        def kill(self):
            self._killed = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(ov_mod.subprocess, "Popen", _FakePopen)
    ov_mod._sidecar_process = None
    provider = ov_mod.OmniVoiceProvider()
    provider._spawn_sidecar()
    assert ov_mod._sidecar_process is not None
    assert ov_mod._sidecar_process.pid == 12345
    ov_mod._sidecar_process = None  # reset module state for other tests


def test_reset_voice_ref_and_respawn_clears_voice_ref_and_kills_old_sidecar(monkeypatch, tmp_path):
    """reset_voice_ref_and_respawn() no longer kills the old sidecar itself --
    _spawn_sidecar() owns the whole kill-then-spawn sequence (atomically, under
    _sidecar_lock) so mock subprocess.Popen and let the real _spawn_sidecar run,
    rather than mocking _spawn_sidecar itself (which would bypass the kill)."""
    monkeypatch.setattr(ov_mod.settings, "artifacts_dir", str(tmp_path))
    ov_mod._voice_ref.update({"path": "/tmp/fake.wav", "text": "old"})

    class _FakeProc:
        def __init__(self, *a, **kw):
            self.killed = False

        def poll(self):
            return None if not self.killed else 0

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    old_proc = _FakeProc()
    ov_mod._sidecar_process = old_proc

    popen_calls = []

    def _fake_popen(*a, **kw):
        popen_calls.append((a, kw))
        return _FakeProc()

    monkeypatch.setattr(ov_mod.subprocess, "Popen", _fake_popen)

    ov_mod.reset_voice_ref_and_respawn()

    assert ov_mod._voice_ref == {}
    assert old_proc.killed is True
    assert len(popen_calls) == 1
    assert ov_mod._sidecar_process is not old_proc  # replaced by the new spawn
    ov_mod._sidecar_process = None  # reset module state


def test_spawn_sidecar_serializes_concurrent_calls_via_lock(monkeypatch, tmp_path):
    """Two threads calling _spawn_sidecar() "simultaneously" (e.g. one session's
    background warm() on a thread-pool thread racing another) must not both
    slip past the kill-check and each spawn independently -- _sidecar_lock
    should make each thread's kill-then-spawn atomic, so whichever thread's
    process doesn't end up as the final _sidecar_process was actually killed
    (never silently leaked/orphaned)."""
    monkeypatch.setattr(ov_mod.settings, "artifacts_dir", str(tmp_path))
    ov_mod._sidecar_process = None

    created: list["_FakePopen"] = []
    created_lock = threading.Lock()

    class _FakePopen:
        def __init__(self, *a, **kw):
            self.killed = False
            self._alive = True
            with created_lock:
                created.append(self)

        def poll(self):
            return None if self._alive else 0

        def kill(self):
            self.killed = True
            self._alive = False

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(ov_mod.subprocess, "Popen", _FakePopen)

    provider = ov_mod.OmniVoiceProvider()
    barrier = threading.Barrier(2)

    def _spawn():
        barrier.wait()  # both threads enter _spawn_sidecar() at nearly the same instant
        provider._spawn_sidecar()

    t1 = threading.Thread(target=_spawn)
    t2 = threading.Thread(target=_spawn)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(created) == 2, "expected exactly one Popen per thread"
    survivor = ov_mod._sidecar_process
    victims = [p for p in created if p is not survivor]
    assert len(victims) == 1
    # Without the lock, a racing thread's kill-check can see _sidecar_process
    # as None (or as its own not-yet-overwritten value) and skip killing the
    # other thread's process entirely, leaking it -- this assertion is what
    # would fail if _sidecar_lock were removed.
    assert victims[0].killed is True
    assert survivor.killed is False

    ov_mod._sidecar_process = None  # reset module state
